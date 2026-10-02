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

## Safety

- **Writes are two-step.** The first call is a dry run that shows the proposed file and folder; nothing is written until you call again with `confirm: true`. Existing files are never overwritten without `overwrite: true`.
- **Path guard.** Every path is `realpath`-resolved and must stay inside iCloud Drive (`../` traversal and symlinks out are refused).

## Your filing taxonomy

`icloud_write` matches keywords in the filename and hint against an ordered list of rules; first match wins.
Copy `taxonomy.example.json` to `taxonomy.json` and describe your own folders. `taxonomy.json` is git-ignored on purpose: a filing taxonomy says a lot about a life.

## Install

```bash
claude mcp add icloud --scope user -- /usr/bin/python3 /path/to/icloud_mcp.py
```

Use `/usr/bin/python3` explicitly. Homebrew Pythons are often missing from the restricted `PATH` that desktop apps and launchd jobs run with.

## Test

```bash
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | /usr/bin/python3 icloud_mcp.py
```
