# iCloud Drive MCP server

Lets Claude (or any MCP client) **find, read and file documents in iCloud Drive** on macOS.

- **Runtime:** `/usr/bin/python3`, standard library only. No npm, no pip, nothing to audit or pin.
- **Search is Spotlight-backed** (`mdfind`). An iCloud Drive used for years of development can hold millions of files, mostly `node_modules`; walking directories is useless at that scale. Spotlight already indexes file *contents*, including PDFs and Pages documents, so search is instant. Build-artefact noise is filtered out.
- **PDF text via macOS PDFKit** (JXA in `pdftext.js`), so no poppler or other native dependency.

## Tools

| Tool | What it does |
|---|---|
| `icloud_search` | Search by content or filename, newest first. Filter by kind (`pdf`, `doc`, `sheet`, `image`…), folder or date. |
| `icloud_read` | Text from PDF, `.docx`, `.rtf`/`.doc`, Pages, Markdown, plain text. Pass a folder to list it. |
| `icloud_write` | Writes a text document, **proposing** a destination folder from your taxonomy. |
| `icloud_tag` | Adds/removes **Finder tags** from your controlled vocabulary. Dry run first, audit-logged, undoable. |
| `icloud_tags` | The vocabulary with colours and counts, or the files carrying a tag. |

`icloud_search` also takes `tags` (with `tag_mode` `all` or `any`), alone or with a text query, and shows each result's tags.

## Safety

- **Writes are two-step.** The first call is a dry run that shows the proposed file and folder; nothing is written until you call again with `confirm: true`. Existing files are never overwritten without `overwrite: true`.
- **Path guard.** Every path is `realpath`-resolved and must stay inside iCloud Drive (`../` traversal and symlinks out are refused).
- **Protected files.** Paths matching protected patterns (recovery codes, ID scans, credit reports, plus your own in `protected.json`) are never opened, listed in full, overwritten or tagged.

## Tags

Folders say *where a document lives*; a file can only be in one. Tags carry the facets that cut across folders: a council-tax letter lives in `Residential Documents` but is also *proof of address*, belongs to a *tax year* and might be *waiting on a reply*. They're native macOS Finder tags, so they sync through iCloud Drive and show in Finder, the iPhone/iPad Files app and Spotlight (`mdfind 'kMDItemUserTags == "…"'`).

- **Controlled vocabulary.** Copy `tags.example.json` to `tags.json` (git-ignored). The tools refuse any tag that isn't in it; new tags are added by editing the file, never by the tool. Give each *facet* one colour, so the dot alone says what kind of tag it is. Emoji are fine, and a tag can be referred to without its leading emoji (`proof of address` → `🏠 Proof of address`). Avoid emoji that carry a hidden variation selector (`✍️`, `⚖️`, `↩️`): they can look identical but not match.
- **Two-step.** `icloud_tag` without `confirm` shows *before → after* for every file; `confirm: true` applies it. At most 50 files per call. Only tags change: files are never moved, renamed or edited, and their modified date is untouched. Plain folders and `node_modules`/build noise are never tagged (packages such as `.pages` are files and can be).
- **Existing tags are kept**, and tags outside the vocabulary are left alone.
- **Colours live in your tag list, not on the file.** Inside iCloud Drive the sync daemon resets every tag's per-file colour within seconds, so the colour a device shows comes from its tag list, which syncs by name. Set each tag's colour once: Finder → Settings → Tags (or right-click the tag in the sidebar), or the Files app → Browse → Tags. The `colour` in `tags.json` is the reference for that, and it is honoured outside iCloud Drive.
- **Audit log and undo.** Every applied change is appended to `~/.local/state/icloud-mcp/tag-audit.jsonl` (time, file, tags before → after) under a batch id. Undo a batch with `icloud_tag` `undo: "<batch id>"` (dry run, then `confirm: true`), or without Claude: `/usr/bin/python3 icloud_mcp.py --undo <batch id> --confirm`. Undo restores the tags that were there (compared by name, since iCloud rewrites colours) and skips any file whose tags changed since.

**Why raw xattr, not `NSURLTagNamesKey`:** setting tags through Apple's `NSURL` API writes every tag back with colour 0, so it silently strips the colours of tags already on a file. The server reads and writes the `com.apple.metadata:_kMDItemUserTags` attribute (a binary plist of `"name\ncolour"` strings, the format Finder itself uses) through `ctypes`, which keeps existing entries intact.

## Your filing taxonomy

`icloud_write` matches keywords in the filename and hint against an ordered list of rules; first match wins.
Copy `taxonomy.example.json` to `taxonomy.json` and describe your own folders. `taxonomy.json` is git-ignored on purpose: a filing taxonomy says a lot about a life. The same goes for `tags.json` and `protected.json`. Set `ICLOUD_MCP_CONFIG_DIR` to keep them somewhere other than next to the server.

## Install

```bash
claude mcp add icloud --scope user -- /usr/bin/python3 /path/to/icloud_mcp.py
```

Use `/usr/bin/python3` explicitly. Homebrew Pythons are often missing from the restricted `PATH` that desktop apps and launchd jobs run with.

## Test

```bash
/usr/bin/python3 -m unittest discover -s tests -v   # tag tests: use a temp folder inside iCloud Drive, removed afterwards

printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | /usr/bin/python3 icloud_mcp.py
```
