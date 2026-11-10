"""Terminal-safety tests: replies must not carry terminal control characters.

    /usr/bin/python3 -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
os.environ.setdefault("ICLOUD_MCP_CONFIG_DIR", tempfile.mkdtemp(prefix="icloud-mcp-config-"))
os.environ.setdefault("ICLOUD_MCP_STATE_DIR", tempfile.mkdtemp(prefix="icloud-mcp-state-"))

# Loaded under its own name so this file doesn't share (and pre-configure) the
# `icloud_mcp` module that the other test files import with their own config.
_spec = importlib.util.spec_from_file_location("icloud_mcp_clean_test", os.path.join(REPO, "icloud_mcp.py"))
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

ENGLAND_FLAG = "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F"


class CleanTextTests(unittest.TestCase):
    def test_csi_colour_sequence(self):
        self.assertEqual(m.clean_text("a\x1b[31mred\x1b[0m"), "a␛[31mred␛[0m")

    def test_osc52_with_bel(self):
        self.assertEqual(m.clean_text("x\x1b]52;c;cHduZWQ=\x07"), "x␛]52;c;cHduZWQ=␇")

    def test_crlf_and_bare_cr(self):
        self.assertEqual(m.clean_text("line1\r\nline2\rX"), "line1\nline2␍X")

    def test_tab_and_newline_kept(self):
        self.assertEqual(m.clean_text("tab\tand\nnewline"), "tab\tand\nnewline")

    def test_c1_range(self):
        self.assertEqual(m.clean_text("\x9b31m\x85"), "�31m�")

    def test_del_and_nul(self):
        self.assertEqual(m.clean_text("del\x7f nul\x00"), "del␡ nul␀")

    def test_bidi_controls_dropped(self):
        self.assertEqual(m.clean_text("‮evil‬ ⁦iso⁩"), "evil iso")

    def test_zwj_emoji_kept(self):
        s = "\U0001F469‍\U0001F4BB"
        self.assertEqual(m.clean_text(s), s)

    def test_emoji_tag_sequence_kept(self):
        self.assertEqual(m.clean_text(ENGLAND_FLAG), ENGLAND_FLAG)

    def test_zwnj_kept(self):
        s = "می‌خواهم"
        self.assertEqual(m.clean_text(s), s)

    def test_non_string_passes_through(self):
        self.assertIsNone(m.clean_text(None))
        self.assertEqual(m.clean_text(5), 5)

    def test_clean_reply_cleans_text_and_error(self):
        result = {"content": [{"type": "text", "text": "\x1b[2Jhi"}]}
        error = {"code": -32602, "message": "Unknown tool: \x1b]0;t\x07"}
        m._clean_reply(result, error)
        self.assertEqual(result["content"][0]["text"], "␛[2Jhi")
        self.assertNotIn("\x1b", error["message"])
        self.assertNotIn("\x07", error["message"])


class StdioEndToEndTests(unittest.TestCase):
    def test_hostile_tool_name_is_cleaned_on_the_wire(self):
        reqs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "x\u001b]0;pwned\u0007", "arguments": {}}},
        ]
        stdin = "".join(json.dumps(r) + "\n" for r in reqs)
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO, "icloud_mcp.py")],
            input=stdin, capture_output=True, text=True, timeout=30, env=os.environ.copy(),
        )
        replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        reply = next(r for r in replies if r.get("id") == 2)
        decoded = json.dumps(reply, ensure_ascii=False)
        self.assertNotIn("\x1b", decoded)
        self.assertNotIn("\x07", decoded)
        self.assertIn("␛", decoded)


if __name__ == "__main__":
    unittest.main()
