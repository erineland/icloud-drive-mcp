#!/usr/bin/env python3
"""
iCloud Drive MCP server — search, read and file documents in iCloud Drive.

Deliberately dependency-free: stdlib only, runs on the system python3.
No npm, no pip, nothing to pin or audit.

Why this exists rather than a generic filesystem server: an iCloud Drive used for
years of development can hold millions of files, mostly node_modules. Directory
walking is useless at that scale, so search is delegated to Spotlight (mdfind),
which already indexes file contents including PDFs and Pages documents.

Tools
  icloud_search  Spotlight-backed search with noise filtering
  icloud_read    text extraction from PDF / docx / rtf / pages / plain text
  icloud_write   writes a document, proposing a destination from the folder
                 taxonomy; refuses to write until confirm=true
"""

import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

ICLOUD_ROOT = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs"
)
HERE = os.path.dirname(os.path.abspath(__file__))
PDFTEXT_JS = os.path.join(HERE, "pdftext.js")

SERVER_NAME = "icloud"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

# Paths that are never useful in results. Matched as path substrings.
NOISE = (
    "/node_modules/",
    "/.git/",
    "/.venv/",
    "/venv/",
    "/__pycache__/",
    "/DerivedData/",
    "/.build/",
    "/Pods/",
    "/.cache/",
    "/site-packages/",
    "/bower_components/",
    "/.Trash/",
)

EXT_GROUPS = {
    "pdf": {".pdf"},
    "doc": {".doc", ".docx", ".rtf", ".pages", ".odt", ".txt", ".md"},
    "sheet": {".xls", ".xlsx", ".csv", ".numbers", ".tsv"},
    "slides": {".ppt", ".pptx", ".key"},
    "image": {".png", ".jpg", ".jpeg", ".heic", ".gif", ".tiff", ".tif", ".webp"},
    "text": {".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".csv", ".log"},
    "markdown": {".md", ".markdown"},
    "archive": {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar"},
}

# Extensions we can pull plain text out of directly.
PLAIN_TEXT_EXT = {
    ".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".csv", ".tsv",
    ".log", ".html", ".htm", ".xml", ".py", ".js", ".ts", ".sh", ".css",
}

# Destination taxonomy: (keywords, folder) rules, first match wins.
# Your own rules live in taxonomy.json next to this file (git-ignored, because a
# folder taxonomy says a lot about a life). taxonomy.example.json shows the format.
DEFAULT_TAXONOMY = [
    (("passport", "birth certificate", "identification"), "Documents/Important Documents"),
    (("insurance", "policy schedule"), "Documents/Insurance"),
    (("payslip", "p60"), "Documents/Finance/Payslips"),
    (("tax", "hmrc", "self assessment"), "Documents/Finance/Tax"),
    (("invoice",), "Documents/Finance/Invoices/Outstanding"),
    (("receipt", "order confirmation"), "Documents/Finance/Receipts"),
    (("statement",), "Documents/Finance/Statements"),
    (("contract", "offer letter", "employment"), "Documents/Work"),
    (("tenancy", "lease", "council tax"), "Documents/Home"),
    (("medical", "prescription", "nhs"), "Documents/Medical"),
    (("flight", "boarding pass", "itinerary"), "Documents/Travel"),
    (("ticket", "festival", "event"), "Documents/Tickets"),
]


def load_taxonomy():
    path = os.path.join(HERE, "taxonomy.json")
    if not os.path.exists(path):
        return DEFAULT_TAXONOMY
    with open(path, encoding="utf-8") as fh:
        return [(tuple(r["keywords"]), r["folder"]) for r in json.load(fh)]


TAXONOMY = load_taxonomy()

FALLBACK_DIR = "Documents/Miscellaneous"


# ---------------------------------------------------------------- helpers

def log(msg):
    """Diagnostics go to stderr; stdout is reserved for JSON-RPC."""
    print(f"[icloud-mcp] {msg}", file=sys.stderr, flush=True)


def is_noise(path):
    return any(n in path for n in NOISE)


def resolve(path):
    """Resolve a user-supplied path to an absolute path inside iCloud Drive.

    Accepts absolute paths or paths relative to the iCloud Drive root.
    Raises ValueError if the result escapes the root — this is the only thing
    standing between a bad path and the rest of the filesystem.
    """
    path = os.path.expanduser(path)
    candidate = path if os.path.isabs(path) else os.path.join(ICLOUD_ROOT, path)
    full = os.path.realpath(candidate)
    root = os.path.realpath(ICLOUD_ROOT)
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"path is outside iCloud Drive: {path}")
    return full


def rel(path):
    root = os.path.realpath(ICLOUD_ROOT)
    full = os.path.realpath(path)
    return full[len(root) + 1:] if full.startswith(root + os.sep) else full


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0


def run_cmd(args, timeout=60):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, "", str(e)


# ---------------------------------------------------------------- extraction

def pdf_text(path, pages=None):
    args = ["osascript", "-l", "JavaScript", PDFTEXT_JS, path]
    if pages:
        args += [str(pages[0]), str(pages[1])]
    code, out, err = run_cmd(args, timeout=120)
    if code != 0:
        return f"[could not read PDF: {err.strip() or 'osascript failed'}]"
    if out.startswith("ERROR:"):
        return f"[{out.strip()}]"
    return out


def docx_text(path):
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except Exception as e:
        return f"[could not read docx: {e}]"
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    return re.sub(r"<[^>]+>", "", xml)


def pages_text(path):
    """Apple Pages documents: try the embedded PDF preview, else give up cleanly."""
    if os.path.isdir(path):
        preview = os.path.join(path, "preview.pdf")
        if os.path.exists(preview):
            return pdf_text(preview)
        return "[Pages package with no preview.pdf — open in Pages and export]"
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith("preview.pdf"):
                    tmp = os.path.join("/tmp", f"_icloudmcp_preview_{os.getpid()}.pdf")
                    with open(tmp, "wb") as fh:
                        fh.write(z.read(name))
                    try:
                        return pdf_text(tmp)
                    finally:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
    except Exception as e:
        return f"[could not read Pages file: {e}]"
    return "[Pages file with no extractable preview — open in Pages and export]"


def textutil_text(path):
    code, out, err = run_cmd(["textutil", "-convert", "txt", "-stdout", path])
    return out if code == 0 else f"[could not convert: {err.strip()}]"


def extract_text(path, pages=None):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return pdf_text(path, pages)
    if ext == ".docx":
        return docx_text(path)
    if ext in (".pages", ".key", ".numbers"):
        return pages_text(path)
    if ext in (".doc", ".rtf", ".rtfd", ".odt", ".html", ".htm"):
        return textutil_text(path)
    if ext in PLAIN_TEXT_EXT or ext == "":
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except Exception as e:
            return f"[could not read: {e}]"
    return f"[no text extractor for '{ext}' files]"


def snippet_for(path, terms, width=110):
    """First matching line of context, for text-ish files only. Best effort."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in PLAIN_TEXT_EXT:
        return None
    try:
        if os.path.getsize(path) > 2_000_000:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            body = fh.read(400_000)
    except Exception:
        return None
    low = body.lower()
    for t in terms:
        i = low.find(t.lower())
        if i >= 0:
            start = max(0, i - width // 2)
            frag = body[start:start + width].replace("\n", " ").strip()
            return ("…" if start > 0 else "") + re.sub(r"\s+", " ", frag) + "…"
    return None


# ---------------------------------------------------------------- tools

def tool_search(args):
    query = (args.get("query") or "").strip()
    if not query:
        return "Provide a query."

    limit = int(args.get("limit") or 20)
    kind = (args.get("kind") or "any").lower()
    folder = args.get("folder")
    names_only = bool(args.get("names_only"))
    since = args.get("since")

    scope = resolve(folder) if folder else ICLOUD_ROOT
    if not os.path.isdir(scope):
        return f"No such folder: {folder}"

    cmd = ["mdfind", "-onlyin", scope]
    if names_only:
        cmd += ["-name", query]
    else:
        cmd += [query]

    code, out, err = run_cmd(cmd, timeout=90)
    if code != 0:
        return f"Spotlight search failed: {err.strip() or code}"

    since_ts = None
    if since:
        try:
            since_ts = datetime.strptime(since, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            return f"Bad 'since' date {since!r} — use YYYY-MM-DD."

    wanted_exts = EXT_GROUPS.get(kind) if kind != "any" else None

    rows, scanned, skipped_noise = [], 0, 0
    for line in out.splitlines():
        scanned += 1
        if scanned > 20000:
            break
        if not line or is_noise(line):
            skipped_noise += 1
            continue
        ext = os.path.splitext(line)[1].lower()
        if wanted_exts is not None and ext not in wanted_exts:
            continue
        try:
            st = os.stat(line)
        except OSError:
            continue
        if since_ts and st.st_mtime < since_ts:
            continue
        rows.append((st.st_mtime, st.st_size, line))

    rows.sort(reverse=True)  # newest first
    total = len(rows)
    rows = rows[:limit]

    if not rows:
        extra = f" ({skipped_noise} noise paths filtered)" if skipped_noise else ""
        return f"No matches for {query!r}{extra}."

    terms = [t for t in re.split(r"\s+", query) if len(t) > 2]
    lines = [f"{total} match(es) for {query!r}; showing {len(rows)}, newest first."]
    if skipped_noise:
        lines.append(f"({skipped_noise} node_modules/build paths filtered out)")
    lines.append("")
    for mtime, size, path in rows:
        when = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        lines.append(f"{rel(path)}")
        lines.append(f"    {when} · {human_size(size)}")
        snip = snippet_for(path, terms)
        if snip:
            lines.append(f"    {snip}")
    return "\n".join(lines)


def tool_read(args):
    path_arg = args.get("path")
    if not path_arg:
        return "Provide a path."
    path = resolve(path_arg)
    if not os.path.exists(path):
        return f"No such file: {path_arg}"
    if os.path.isdir(path) and not path.endswith((".pages", ".key", ".numbers")):
        entries = sorted(os.listdir(path))[:100]
        return f"{rel(path)} is a directory:\n" + "\n".join("  " + e for e in entries)

    pages = None
    if args.get("pages"):
        m = re.match(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$", str(args["pages"]))
        if not m:
            return "Bad 'pages' — use e.g. '1' or '2-6'."
        pages = (int(m.group(1)), int(m.group(2) or m.group(1)))

    text = extract_text(path, pages)
    max_chars = int(args.get("max_chars") or 40000)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]

    header = f"{rel(path)} · {human_size(os.path.getsize(path))}"
    if truncated:
        header += f" · TRUNCATED at {max_chars} chars (raise max_chars or use pages)"
    return header + "\n" + "-" * 60 + "\n" + text


def suggest_folder(filename, hint=None):
    """Rank destinations by keyword match against the hint and filename."""
    haystack = f"{hint or ''} {filename}".lower()
    matches = []
    for keywords, dest in TAXONOMY:
        for kw in keywords:
            if kw in haystack:
                matches.append((dest, kw))
                break
    return matches


def tool_write(args):
    filename = (args.get("filename") or "").strip()
    content = args.get("content")
    if not filename:
        return "Provide a filename."
    if content is None:
        return "Provide content."
    if "/" in filename or filename in (".", ".."):
        return "filename must be a bare name, not a path — use 'folder' to choose the location."

    explicit = args.get("folder")
    hint = args.get("hint")
    confirm = bool(args.get("confirm"))
    overwrite = bool(args.get("overwrite"))

    if explicit:
        dest_dir_rel = explicit
        why = "folder given explicitly"
        alternatives = []
    else:
        matches = suggest_folder(filename, hint)
        if matches:
            dest_dir_rel, kw = matches[0]
            why = f"matched {kw!r} in hint/filename"
            alternatives = [d for d, _ in matches[1:4]]
        else:
            dest_dir_rel, why = FALLBACK_DIR, "no taxonomy match — fallback"
            alternatives = []

    try:
        dest_dir = resolve(dest_dir_rel)
    except ValueError as e:
        return str(e)
    dest = os.path.join(dest_dir, filename)

    exists = os.path.exists(dest)
    dir_exists = os.path.isdir(dest_dir)

    if not confirm:
        out = [
            "PROPOSED (nothing written yet — call again with confirm=true)",
            "",
            f"  file:   {filename}",
            f"  folder: {dest_dir_rel}" + ("" if dir_exists else "   [will be created]"),
            f"  why:    {why}",
            f"  size:   {human_size(len(content.encode('utf-8')))}",
        ]
        if exists:
            out.append("  ⚠️  a file of this name already exists — pass overwrite=true to replace it")
        if alternatives:
            out.append("")
            out.append("  other candidates:")
            out += [f"    - {a}" for a in alternatives]
        return "\n".join(out)

    if exists and not overwrite:
        return f"Refusing to overwrite existing file: {rel(dest)}\nPass overwrite=true if that is intended."

    try:
        os.makedirs(dest_dir, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)
    except Exception as e:
        return f"Write failed: {e}"

    verb = "Overwrote" if exists else "Wrote"
    return f"{verb} {rel(dest)} ({human_size(os.path.getsize(dest))})"


# ---------------------------------------------------------------- MCP wiring

TOOLS = [
    {
        "name": "icloud_search",
        "description": (
            "Search iCloud Drive by content or filename using macOS Spotlight. "
            "Searches inside PDFs, Pages documents, Markdown and plain text. "
            "Automatically filters out node_modules and build-artefact noise. "
            "Results are newest-first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to search for."},
                "kind": {
                    "type": "string",
                    "enum": ["any", "pdf", "doc", "sheet", "slides", "image", "text", "markdown", "archive"],
                    "description": "Restrict to a file type. Default 'any'.",
                },
                "folder": {"type": "string", "description": "Restrict to a subfolder, relative to iCloud Drive root."},
                "since": {"type": "string", "description": "Only files modified on/after this date (YYYY-MM-DD)."},
                "names_only": {"type": "boolean", "description": "Match filenames only, not contents. Default false."},
                "limit": {"type": "integer", "description": "Max results. Default 20."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "icloud_read",
        "description": (
            "Read a document from iCloud Drive as text. Handles PDF (via macOS "
            "PDFKit), .docx, .rtf/.doc, Apple Pages, Markdown and plain text. "
            "Pass a directory path to list its contents."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path, or relative to iCloud Drive root."},
                "pages": {"type": "string", "description": "PDFs only, e.g. '1' or '2-6'."},
                "max_chars": {"type": "integer", "description": "Truncation limit. Default 40000."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "icloud_write",
        "description": (
            "Write a text document into iCloud Drive, choosing the destination "
            "from the existing folder taxonomy. Call it WITHOUT confirm first to "
            "see the proposed location, then again with confirm=true to write. "
            "Never overwrites unless overwrite=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Bare filename, no slashes."},
                "content": {"type": "string", "description": "Full text content of the file."},
                "hint": {"type": "string", "description": "What this document is, e.g. 'payslip', 'tenancy agreement'. Drives folder choice."},
                "folder": {"type": "string", "description": "Explicit destination folder relative to iCloud root. Overrides the suggestion."},
                "confirm": {"type": "boolean", "description": "Must be true to actually write. Default false (dry run)."},
                "overwrite": {"type": "boolean", "description": "Allow replacing an existing file. Default false."},
            },
            "required": ["filename", "content"],
        },
    },
]

HANDLERS = {
    "icloud_search": tool_search,
    "icloud_read": tool_read,
    "icloud_write": tool_write,
}


def respond(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(req):
    method = req.get("method")
    msg_id = req.get("id")

    # Notifications carry no id and must not be answered.
    if msg_id is None:
        return

    if method == "initialize":
        asked = (req.get("params") or {}).get("protocolVersion")
        version = asked if asked in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        respond(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    elif method == "ping":
        respond(msg_id, {})
    elif method == "tools/list":
        respond(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        fn = HANDLERS.get(name)
        if fn is None:
            respond(msg_id, error={"code": -32602, "message": f"Unknown tool: {name}"})
            return
        try:
            text = fn(params.get("arguments") or {})
            respond(msg_id, {"content": [{"type": "text", "text": text}]})
        except Exception as e:
            log(f"tool {name} failed: {e!r}")
            respond(msg_id, {
                "content": [{"type": "text", "text": f"Error in {name}: {e}"}],
                "isError": True,
            })
    else:
        respond(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main():
    if not os.path.isdir(ICLOUD_ROOT):
        log(f"FATAL: iCloud Drive not found at {ICLOUD_ROOT}")
        sys.exit(1)
    log(f"ready · root={ICLOUD_ROOT}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            log(f"bad JSON: {e}")
            continue
        try:
            handle(req)
        except Exception as e:
            log(f"handler crashed: {e!r}")


if __name__ == "__main__":
    main()
