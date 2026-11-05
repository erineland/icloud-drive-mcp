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
  icloud_tag     adds/removes Finder tags from a controlled vocabulary;
                 dry run until confirm=true, audit-logged, undoable
  icloud_tags    the tag vocabulary with counts, or the files carrying a tag

Personal configuration lives next to this file and is git-ignored:
  taxonomy.json   filing rules for icloud_write   (see taxonomy.example.json)
  tags.json       the tag vocabulary              (see tags.example.json)
  protected.json  extra never-touch path patterns (see protected.example.json)
"""

import ctypes
import ctypes.util
import json
import os
import plistlib
import secrets
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
# Where the personal config files live (default: next to this file).
CONFIG_DIR = os.environ.get("ICLOUD_MCP_CONFIG_DIR", HERE)
# Where the tag audit log lives. Outside the server folder on purpose, so a
# routine tag change doesn't look like the server itself was modified.
STATE_DIR = os.environ.get(
    "ICLOUD_MCP_STATE_DIR", os.path.expanduser("~/.local/state/icloud-mcp")
)

SERVER_NAME = "icloud"
SERVER_VERSION = "1.1.0"
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

# Destination taxonomy: (keywords, folder) rules, first match wins, so more
# specific rules sit higher. Your own rules live in taxonomy.json (git-ignored,
# because a filing taxonomy says a lot about a life). This is only the fallback.
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


def load_json_config(name):
    path = os.path.join(CONFIG_DIR, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_taxonomy():
    rules = load_json_config("taxonomy.json")
    if rules is None:
        return DEFAULT_TAXONOMY
    return [(tuple(r["keywords"]), r["folder"]) for r in rules]


TAXONOMY = load_taxonomy()

FALLBACK_DIR = "Documents/Miscellaneous"


# ---------------------------------------------------------------- helpers

def log(msg):
    """Diagnostics go to stderr; stdout is reserved for JSON-RPC."""
    print(f"[icloud-mcp] {msg}", file=sys.stderr, flush=True)


def is_noise(path):
    return any(n in path for n in NOISE)


# Files the server must never open, list, overwrite or tag: account-recovery
# material, wallet backups, credit reports and ID scans. The owner opens these
# themselves. Checked on the real path (symlinks resolved), case-insensitively.
# protected.json can add patterns and replace the refusal message.
DEFAULT_PROTECTED = (
    r"recovery[ _-]?code", r"recovery[ _-]?key", r"emergency[ _-]?kit", r"secret[ _-]?qr",
    r"security[ _-]?qr", r"(^|/)credit reports(/|$)", r"bitlocker",
    r"passport", r"driving[ _-]?licen[cs]e", r"drivers?[ _-]?licen[cs]e",
)
DEFAULT_PROTECTED_MSG = "protected file: open it yourself (it matches a protected pattern)."


def load_protected():
    cfg = load_json_config("protected.json") or {}
    patterns = list(DEFAULT_PROTECTED)
    for pat in cfg.get("patterns", []):
        if pat not in patterns:
            patterns.append(pat)
    return [re.compile(p, re.I) for p in patterns], cfg.get("message", DEFAULT_PROTECTED_MSG)


PROTECTED, PROTECTED_MSG = load_protected()


def is_protected(path):
    try:
        r = rel(os.path.realpath(path))
    except Exception:
        return True
    return any(p.search(r) for p in PROTECTED)


def masked_name(path):
    """A protected file's path for listings: long digit runs (e.g. PDF passwords) hidden."""
    return re.sub(r"\d{5,}", lambda m: "#" * len(m.group(0)), rel(path))


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
    tags = [t.strip() for t in as_list(args.get("tags")) if t and t.strip()]
    tag_mode = (args.get("tag_mode") or "all").lower()
    if not query and not tags:
        return "Provide a query, tags, or both."
    if tag_mode not in ("all", "any"):
        return "tag_mode must be 'all' or 'any'."

    limit = int(args.get("limit") or 20)
    kind = (args.get("kind") or "any").lower()
    folder = args.get("folder")
    names_only = bool(args.get("names_only"))
    since = args.get("since")

    scope = resolve(folder) if folder else ICLOUD_ROOT
    if not os.path.isdir(scope):
        return f"No such folder: {folder}"

    tagged = None
    if tags:
        try:
            vocab = load_vocab()
        except ValueError:
            vocab = {}
        tags = [canonical_tag(t, vocab) for t in tags]
        try:
            tagged = spotlight_tag_paths(tags, tag_mode, scope)
        except RuntimeError as e:
            return str(e)

    if query:
        cmd = ["mdfind", "-onlyin", scope]
        if names_only:
            cmd += ["-name", query]
        else:
            cmd += [query]
        code, out, err = run_cmd(cmd, timeout=90)
        if code != 0:
            return f"Spotlight search failed: {err.strip() or code}"
        if tagged is not None:
            keep = set(tagged)
            out = "\n".join(l for l in out.splitlines() if l in keep)
    else:
        out = "\n".join(tagged)

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

    what = repr(query) if query else ""
    if tags:
        joined = (" + " if tag_mode == "all" else " or ").join(tags)
        what = (what + " " if what else "") + f"tagged {joined}"

    if not rows:
        extra = f" ({skipped_noise} noise paths filtered)" if skipped_noise else ""
        return f"No matches for {what}{extra}."

    terms = [t for t in re.split(r"\s+", query) if len(t) > 2]
    lines = [f"{total} match(es) for {what}; showing {len(rows)}, newest first."]
    if skipped_noise:
        lines.append(f"({skipped_noise} node_modules/build paths filtered out)")
    lines.append("")
    for mtime, size, path in rows:
        when = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        if is_protected(path):
            lines.append(f"{masked_name(path)}")
            lines.append(f"    {when} · {human_size(size)} · protected (open it yourself)")
            continue
        lines.append(f"{rel(path)}")
        names = tag_names(path)
        lines.append(f"    {when} · {human_size(size)}" + (f" · tags: {', '.join(names)}" if names else ""))
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
    if is_protected(path):
        return PROTECTED_MSG
    if os.path.isdir(path) and not path.endswith((".pages", ".key", ".numbers")):
        entries = sorted(os.listdir(path))[:100]
        shown = [e + "  (protected)" if is_protected(os.path.join(path, e)) else e for e in entries]
        return f"{rel(path)} is a directory:\n" + "\n".join("  " + e for e in shown)

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
    if is_protected(dest) or is_protected(dest_dir):
        return PROTECTED_MSG

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


# ---------------------------------------------------------------- tags
#
# Finder tags live in the extended attribute com.apple.metadata:_kMDItemUserTags
# as a binary plist: a list of "name\ncolour" strings (colour 0-7). iCloud Drive
# syncs the attribute, Finder and the Files app show it, Spotlight indexes it as
# kMDItemUserTags.
#
# Why raw xattr rather than NSURL's NSURLTagNamesKey: setting tags through that
# API writes every tag back with colour 0, silently stripping the colours of tags
# that were already on the file (verified on macOS 27, Nov 2026). Writing the
# attribute ourselves keeps existing entries byte-for-byte. ctypes is stdlib, so
# this stays dependency-free.
#
# Colours: inside iCloud Drive the sync daemon rewrites every entry's colour
# digit to 1 within seconds (verified Nov 2026), so per-file colours don't
# survive there. The colour a device shows comes from its tag list (Finder >
# Settings > Tags, or the Files app), which syncs by tag NAME. tags.json's
# colours are still written (they hold outside iCloud) and are the reference
# for setting each tag's colour once in that list.

TAG_XATTR = b"com.apple.metadata:_kMDItemUserTags"
XATTR_NOFOLLOW = 0x0001
ENOATTR = 93
MAX_TAG_FILES = 50
PACKAGE_EXTS = (".pages", ".key", ".numbers", ".rtfd")

COLOURS = {"none": 0, "grey": 1, "gray": 1, "green": 2, "purple": 3,
           "blue": 4, "yellow": 5, "red": 6, "orange": 7}
COLOUR_NAMES = {0: "none", 1: "grey", 2: "green", 3: "purple",
                4: "blue", 5: "yellow", 6: "red", 7: "orange"}
# A tag name may not hold control characters, quotes, backslashes or wildcards:
# they'd break the plist entry format or the Spotlight query.
TAG_NAME_RE = re.compile(r'^[^\x00-\x1f"\\*]{1,40}$')

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                           ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
_libc.getxattr.restype = ctypes.c_ssize_t
_libc.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                           ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
_libc.setxattr.restype = ctypes.c_int
_libc.removexattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
_libc.removexattr.restype = ctypes.c_int


def read_tag_entries(path):
    """The raw tag entries on a file, e.g. ['Work\\n4', 'tax-2025-26\\n0']."""
    p = os.fsencode(path)
    size = _libc.getxattr(p, TAG_XATTR, None, 0, 0, XATTR_NOFOLLOW)
    if size < 0:
        err = ctypes.get_errno()
        if err == ENOATTR:
            return []
        raise OSError(err, os.strerror(err), path)
    buf = ctypes.create_string_buffer(size)
    got = _libc.getxattr(p, TAG_XATTR, buf, size, 0, XATTR_NOFOLLOW)
    if got < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path)
    data = plistlib.loads(buf.raw[:got])
    if not isinstance(data, list) or not all(isinstance(e, str) for e in data):
        raise ValueError(f"unexpected tag data on {rel(path)}")
    return data


def write_tag_entries(path, entries):
    """Replace a file's tag entries. An empty list removes the attribute, as Finder does."""
    p = os.fsencode(path)
    if not entries:
        if _libc.removexattr(p, TAG_XATTR, XATTR_NOFOLLOW) != 0:
            err = ctypes.get_errno()
            if err != ENOATTR:
                raise OSError(err, os.strerror(err), path)
        return
    data = plistlib.dumps(list(entries), fmt=plistlib.FMT_BINARY)
    if _libc.setxattr(p, TAG_XATTR, data, len(data), 0, XATTR_NOFOLLOW) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path)


def entry_name(entry):
    return entry.split("\n", 1)[0]


def names_of(entries):
    return [entry_name(e) for e in entries]


def tag_names(path):
    try:
        return [entry_name(e) for e in read_tag_entries(path)]
    except Exception:
        return []


def load_vocab():
    """tags.json → {lowercased name: {name, colour, facet, description}}.

    Re-read on every call so an edit to tags.json applies without a restart.
    """
    cfg = load_json_config("tags.json")
    if cfg is None:
        return {}
    vocab = {}
    for t in cfg.get("tags", []):
        name = t.get("name", "")
        if not TAG_NAME_RE.match(name) or name != name.strip():
            raise ValueError(f"tags.json: bad tag name {name!r}")
        colour = t.get("colour", "none")
        if colour not in COLOURS:
            raise ValueError(f"tags.json: tag {name!r} has unknown colour {colour!r}")
        entry = {
            "name": name,
            "colour": COLOURS[colour],
            "facet": t.get("facet", ""),
            "description": t.get("description", ""),
        }
        if name.lower() in vocab:
            raise ValueError(f"tags.json: tag {name!r} is listed twice")
        vocab[name.lower()] = entry
    # Aliases: a tag can be named without its leading emoji ("proof of address"
    # for "🏠 Proof of address"), so nobody has to type emoji. Never shadows a real name.
    for entry in list(vocab.values()):
        bare = tag_alias(entry["name"])
        if bare and bare not in vocab:
            vocab[bare] = entry
    return vocab


def tag_alias(name):
    """A tag name without leading emoji/symbols, lowercased ('' if nothing differs)."""
    bare = re.sub(r"^[^\w]+", "", name).strip().lower()
    return bare if bare and bare != name.lower() else ""


def vocab_tags(vocab):
    """Each vocabulary tag once (the dict also holds aliases)."""
    seen, out = set(), []
    for v in vocab.values():
        if v["name"] not in seen:
            seen.add(v["name"])
            out.append(v)
    return out


def canonical_tag(name, vocab):
    v = vocab.get(name.strip().lower())
    return v["name"] if v else name.strip()


def as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


def spotlight_quote(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("*", "\\*")


def spotlight_tag_paths(names, mode="all", scope=None):
    """Paths carrying the tags (mode 'all' = every tag, 'any' = at least one)."""
    joiner = " && " if mode == "all" else " || "
    query = joiner.join(f'(kMDItemUserTags == "{spotlight_quote(n)}"c)' for n in names)
    code, out, err = run_cmd(["mdfind", "-onlyin", scope or ICLOUD_ROOT, query], timeout=90)
    if code != 0:
        raise RuntimeError(f"Spotlight tag search failed: {err.strip() or code}")
    return [line for line in out.splitlines() if line and not is_noise(line)]


def audit_path():
    return os.path.join(STATE_DIR, "tag-audit.jsonl")


def new_batch_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def audit_append(record):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(audit_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def audit_records(batch):
    try:
        with open(audit_path(), encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []
    return [r for r in rows if r.get("batch") == batch]


def describe(entries):
    return ", ".join(entry_name(e) for e in entries) or "(none)"


def apply_changes(changes, op, undoes=None):
    """changes: [(abs_path, before, after)]. Applies, verifies and logs each one.

    A change whose audit line can't be written is rolled back, so the log never
    misses an applied change.
    """
    batch = new_batch_id()
    done, failed = [], []
    for path, before, after in changes:
        try:
            write_tag_entries(path, after)
            if read_tag_entries(path) != after:
                raise RuntimeError("tags read back differently after writing")
            record = {
                "batch": batch, "ts": datetime.now().isoformat(timespec="seconds"),
                "op": op, "path": rel(path), "before": before, "after": after,
            }
            if undoes:
                record["undoes"] = undoes
            try:
                audit_append(record)
            except Exception:
                write_tag_entries(path, before)
                raise
            done.append((path, before, after))
        except Exception as e:
            failed.append((path, str(e)))
    return batch, done, failed


def tag_target_problem(path):
    """Why a resolved path can't be tagged, or None if it can."""
    if is_noise(path):
        return "inside node_modules/build noise; never tagged"
    if not os.path.exists(path):
        return "no such file"
    if os.path.realpath(path) == os.path.realpath(ICLOUD_ROOT):
        return "that's the iCloud Drive root"
    if is_protected(path):
        return "protected file: tag it yourself in Finder"
    if os.path.isdir(path) and not path.lower().endswith(PACKAGE_EXTS):
        return "folders aren't tagged (the folder already says it); tag the files"
    return None


def tool_tag(args):
    if args.get("undo"):
        return undo_batch(str(args["undo"]), bool(args.get("confirm")))

    files = as_list(args.get("files"))
    add, remove = as_list(args.get("add")), as_list(args.get("remove"))
    confirm = bool(args.get("confirm"))
    if not files:
        return "Provide 'files' (one path or a list)."
    if not add and not remove:
        return "Provide tags to 'add' and/or 'remove'."
    if len(files) > MAX_TAG_FILES:
        return f"Refused: {len(files)} files is over the per-call cap of {MAX_TAG_FILES}. Split it into batches."

    vocab = load_vocab()
    unknown = [t for t in add + remove if t.lower() not in vocab]
    if unknown:
        hint = "No tags.json yet, so no tags are allowed." if not vocab else \
            "Allowed: " + ", ".join(v["name"] for v in vocab_tags(vocab))
        return ("Refused: not in the tag vocabulary: " + ", ".join(unknown) +
                ".\nNew tags are added by editing tags.json, not by this tool.\n" + hint)
    add_v = [vocab[t.lower()] for t in add]
    remove_keys = {vocab[t.lower()]["name"].lower() for t in remove}
    clash = {v["name"].lower() for v in add_v} & remove_keys
    if clash:
        return "Refused: the same tag is in both add and remove: " + ", ".join(sorted(clash))

    resolved, seen = [], set()
    for f in files:
        try:
            full = resolve(f)
        except ValueError as e:
            return f"Refused: {e}"
        if is_noise(full):
            return f"Refused: {f} is inside node_modules/build noise; never tagged."
        if full not in seen:
            seen.add(full)
            resolved.append((f, full))

    changes, lines, skipped = [], [], []
    for given, full in resolved:
        problem = tag_target_problem(full)
        if problem:
            skipped.append(f"  ✗ {given}: {problem}")
            continue
        try:
            before = read_tag_entries(full)
        except Exception as e:
            skipped.append(f"  ✗ {given}: can't read its tags ({e})")
            continue
        after = [e for e in before if entry_name(e).lower() not in remove_keys]
        have = {entry_name(e).lower() for e in after}
        for v in add_v:
            if v["name"].lower() not in have:
                after.append(f"{v['name']}\n{v['colour']}")
                have.add(v["name"].lower())
        if after == before:
            lines.append(f"  = {rel(full)}\n      no change ({describe(before)})")
            continue
        changes.append((full, before, after))
        lines.append(f"  ~ {rel(full)}\n      {describe(before)}  →  {describe(after)}")

    if not confirm:
        out = [f"PROPOSED (nothing changed yet; call again with confirm=true): "
               f"{len(changes)} file(s) would change."]
        return "\n".join(out + [""] + lines + skipped)

    if not changes:
        return "Nothing to change.\n" + "\n".join(lines + skipped)
    batch, done, failed = apply_changes(changes, "tag")
    out = [f"Tagged {len(done)} file(s). Batch {batch}."]
    if done:
        out.append(f"Undo: icloud_tag with undo=\"{batch}\" "
                   f"(or: /usr/bin/python3 icloud_mcp.py --undo {batch} --confirm)")
    out.append("")
    out += lines + skipped
    out += [f"  ✗ {rel(p)}: FAILED, left as it was ({e})" for p, e in failed]
    return "\n".join(out)


def undo_batch(batch, confirm):
    records = [r for r in audit_records(batch)]
    if not records:
        return f"No batch {batch!r} in the audit log ({audit_path()})."
    changes, lines = [], []
    for r in records:
        try:
            full = resolve(r["path"])
        except ValueError as e:
            lines.append(f"  ✗ {r['path']}: {e}")
            continue
        if not os.path.exists(full):
            lines.append(f"  ✗ {r['path']}: no longer there (moved or renamed?); skipped")
            continue
        current = read_tag_entries(full)
        # Compare names, not colours: iCloud Drive resets every entry's colour
        # digit to 1 seconds after syncing, so "after" never matches byte-for-byte.
        if names_of(current) == names_of(r["before"]):
            lines.append(f"  = {r['path']}: already back to {describe(r['before'])}")
        elif names_of(current) != names_of(r["after"]):
            lines.append(f"  ✗ {r['path']}: tags changed since that batch "
                         f"(now {describe(current)}); skipped, nothing overwritten")
        else:
            changes.append((full, current, r["before"]))
            lines.append(f"  ~ {r['path']}\n      {describe(current)}  →  {describe(r['before'])}")
    if not confirm:
        return "\n".join([f"PROPOSED UNDO of {batch} (nothing changed yet; call again with "
                          f"confirm=true): {len(changes)} file(s) would be restored.", ""] + lines)
    if not changes:
        return "Nothing to undo.\n" + "\n".join(lines)
    new, done, failed = apply_changes(changes, "undo", undoes=batch)
    out = [f"Restored {len(done)} file(s). Undo batch {new} (itself undoable)."]
    out += [""] + lines
    out += [f"  ✗ {rel(p)}: FAILED, left as it was ({e})" for p, e in failed]
    return "\n".join(out)


def tool_tags(args):
    tag = (args.get("tag") or "").strip()
    vocab = load_vocab()
    if tag:
        tag = canonical_tag(tag, vocab)
        limit = int(args.get("limit") or 50)
        rows = []
        for p in spotlight_tag_paths([tag]):
            try:
                rows.append((os.stat(p).st_mtime, p))
            except OSError:
                continue
        rows.sort(reverse=True)
        note = "" if tag.lower() in vocab else "  (not in the vocabulary)"
        out = [f"{len(rows)} file(s) tagged {tag!r}{note}; showing {min(limit, len(rows))}, newest first.", ""]
        for mtime, p in rows[:limit]:
            when = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
            name = masked_name(p) + "  (protected)" if is_protected(p) else rel(p)
            out.append(f"{name}\n    {when} · tags: {', '.join(tag_names(p)) or '(none)'}")
        return "\n".join(out)

    if not vocab:
        return "No tag vocabulary yet: create tags.json next to the server (see tags.example.json)."
    out = ["Tag vocabulary (files tagged in iCloud Drive):", ""]
    by_facet = {}
    for v in vocab_tags(vocab):
        by_facet.setdefault(v["facet"] or "Other", []).append(v)
    for facet, tags in by_facet.items():
        out.append(f"{facet}")
        for v in tags:
            n = len(spotlight_tag_paths([v["name"]]))
            out.append(f"  {v['name']}  [{COLOUR_NAMES[v['colour']]}]  {n} file(s)"
                       + (f"  · {v['description']}" if v["description"] else ""))
        out.append("")
    others = {}
    code, raw, _ = run_cmd(["mdfind", "-onlyin", ICLOUD_ROOT, 'kMDItemUserTags == "*"'], timeout=90)
    for p in [l for l in raw.splitlines() if l and not is_noise(l)][:5000]:
        for name in tag_names(p):
            if name.lower() not in vocab:
                others[name] = others.get(name, 0) + 1
    if others:
        out.append("Other tags already on files (not in the vocabulary; left alone):")
        out += [f"  {n}  {c} file(s)" for n, c in sorted(others.items(), key=lambda x: (-x[1], x[0]))]
    return "\n".join(out)


# ---------------------------------------------------------------- MCP wiring

TOOLS = [
    {
        "name": "icloud_search",
        "description": (
            "Search iCloud Drive by content or filename using macOS Spotlight. "
            "Searches inside PDFs, Pages documents, Markdown and plain text. "
            "Automatically filters out node_modules and build-artefact noise. "
            "Optional 'tags' filter (Finder tags), alone or combined with the query. "
            "Results are newest-first and show each file's tags."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to search for. Optional when 'tags' is given."},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "Only files carrying these Finder tags (case-insensitive)."},
                "tag_mode": {"type": "string", "enum": ["all", "any"],
                             "description": "'all' (default): every tag must be present. 'any': at least one."},
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

TOOLS += [
    {
        "name": "icloud_tag",
        "description": (
            "Add or remove Finder tags on iCloud Drive files. Tags must come from "
            "the controlled vocabulary (tags.json); unknown tags are refused. Call "
            "WITHOUT confirm first to see the before -> after for every file, then "
            f"again with confirm=true to apply. At most {MAX_TAG_FILES} files per call. "
            "Only tags change: files are never moved, renamed, edited or deleted. "
            "Every applied change is audit-logged with a batch id; pass undo=<batch id> "
            "(dry run, then confirm=true) to restore exactly what was there before."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "Paths, absolute or relative to iCloud Drive root."},
                "add": {"type": "array", "items": {"type": "string"}, "description": "Tags to add."},
                "remove": {"type": "array", "items": {"type": "string"}, "description": "Tags to remove."},
                "confirm": {"type": "boolean", "description": "Must be true to apply. Default false (dry run)."},
                "undo": {"type": "string", "description": "A batch id to undo instead (dry run unless confirm=true)."},
            },
        },
    },
    {
        "name": "icloud_tags",
        "description": (
            "Without 'tag': the tag vocabulary grouped by facet, with colour and how "
            "many files carry each, plus any other tags already on files. With 'tag': "
            "the files carrying it, newest first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "List the files carrying this tag."},
                "limit": {"type": "integer", "description": "Max files when listing a tag. Default 50."},
            },
        },
    },
]

HANDLERS = {
    "icloud_search": tool_search,
    "icloud_read": tool_read,
    "icloud_write": tool_write,
    "icloud_tag": tool_tag,
    "icloud_tags": tool_tags,
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
    # CLI undo, so a tag batch can be reversed without Claude:
    #   /usr/bin/python3 icloud_mcp.py --undo <batch id> [--confirm]
    if len(sys.argv) >= 3 and sys.argv[1] == "--undo":
        print(undo_batch(sys.argv[2], "--confirm" in sys.argv[3:]))
        return
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
