"""Tag tests. Runs against a temp folder INSIDE iCloud Drive (tags behave
differently elsewhere) and removes only that folder afterwards.

    /usr/bin/python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CONFIG = tempfile.mkdtemp(prefix="icloud-mcp-config-")
STATE = tempfile.mkdtemp(prefix="icloud-mcp-state-")
os.environ["ICLOUD_MCP_CONFIG_DIR"] = CONFIG
os.environ["ICLOUD_MCP_STATE_DIR"] = STATE
sys.path.insert(0, REPO)

VOCAB = {"tags": [
    {"name": "🏠 Proof of address", "colour": "green", "facet": "Proof of"},
    {"name": "📊 Tax 2025-26", "colour": "blue", "facet": "Tax year"},
    {"name": "waiting", "colour": "orange", "facet": "Waiting"},
]}
with open(os.path.join(CONFIG, "tags.json"), "w", encoding="utf-8") as fh:
    json.dump(VOCAB, fh, ensure_ascii=False)
with open(os.path.join(CONFIG, "protected.json"), "w", encoding="utf-8") as fh:
    json.dump({"patterns": ["top[ _-]?secret"]}, fh)

import icloud_mcp as m  # noqa: E402

TEST_DIR_NAME = f"_icloud-mcp-test-{os.getpid()}"
TEST_DIR = os.path.join(m.ICLOUD_ROOT, TEST_DIR_NAME)


def make(name, tags=None):
    path = os.path.join(TEST_DIR, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("test\n")
    if tags is not None:
        m.write_tag_entries(path, tags)
    return path


def names(path):
    """Tag names only: iCloud resets colour digits seconds after a write."""
    return [e.split("\n")[0] for e in m.read_tag_entries(path)]


def audit_lines():
    try:
        with open(m.audit_path(), encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]
    except FileNotFoundError:
        return []


def batch_of(result):
    for word in result.split():
        if word.startswith("20") and word.count("-") == 2:
            return word.rstrip(".")
    raise AssertionError(f"no batch id in: {result}")


class TagTests(unittest.TestCase):
    def setUp(self):
        os.makedirs(TEST_DIR, exist_ok=False)
        if os.path.exists(m.audit_path()):
            os.unlink(m.audit_path())

    def tearDown(self):
        # Remove only our own temp folder, and only if it is what we think it is.
        assert os.path.basename(TEST_DIR).startswith("_icloud-mcp-test-")
        assert os.path.dirname(TEST_DIR) == m.ICLOUD_ROOT
        shutil.rmtree(TEST_DIR)

    def rel(self, path):
        return os.path.relpath(path, m.ICLOUD_ROOT)

    def test_dry_run_changes_nothing(self):
        f = make("a.txt", ["Work\n4"])
        out = m.tool_tag({"files": [self.rel(f)], "add": ["waiting"]})
        self.assertIn("PROPOSED", out)
        self.assertEqual(names(f), ["Work"])
        self.assertEqual(audit_lines(), [])

    def test_confirm_applies_and_keeps_existing_colours(self):
        f = make("a.txt", ["Work\n4", "Red\n6"])
        mtime = os.stat(f).st_mtime
        out = m.tool_tag({"files": [self.rel(f)], "add": ["waiting", "Tax 2025-26"], "confirm": True})
        self.assertIn("Tagged 1 file", out)
        self.assertEqual(names(f), ["Work", "Red", "waiting", "📊 Tax 2025-26"])
        self.assertEqual(os.stat(f).st_mtime, mtime, "tagging must not change the modified date")
        log = audit_lines()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["before"], ["Work\n4", "Red\n6"])

    def test_remove_by_alias_and_leave_others(self):
        f = make("a.txt", ["Work\n4", "🏠 Proof of address\n2"])
        m.tool_tag({"files": [self.rel(f)], "remove": ["proof of address"], "confirm": True})
        self.assertEqual(names(f), ["Work"])

    def test_removing_last_tag_removes_attribute(self):
        f = make("a.txt", ["waiting\n7"])
        m.tool_tag({"files": [self.rel(f)], "remove": ["waiting"], "confirm": True})
        out = subprocess.run(["/usr/bin/xattr", f], capture_output=True, text=True).stdout
        self.assertNotIn("_kMDItemUserTags", out)

    def test_cap_enforced(self):
        files = [f"x{i}.txt" for i in range(m.MAX_TAG_FILES + 1)]
        out = m.tool_tag({"files": files, "add": ["waiting"], "confirm": True})
        self.assertIn("cap", out)
        self.assertEqual(audit_lines(), [])

    def test_path_traversal_refused(self):
        for bad in ("../../../../etc/passwd", "/etc/passwd", "~/.ssh/config"):
            out = m.tool_tag({"files": [bad], "add": ["waiting"], "confirm": True})
            self.assertTrue(out.startswith("Refused"), out)

    def test_symlink_out_refused(self):
        link = os.path.join(TEST_DIR, "escape")
        os.symlink("/etc/hosts", link)
        out = m.tool_tag({"files": [self.rel(link)], "add": ["waiting"], "confirm": True})
        self.assertTrue(out.startswith("Refused"), out)

    def test_unknown_tag_refused(self):
        f = make("a.txt")
        out = m.tool_tag({"files": [self.rel(f)], "add": ["made-up"], "confirm": True})
        self.assertTrue(out.startswith("Refused"), out)
        self.assertEqual(m.read_tag_entries(f), [])

    def test_noise_refused(self):
        f = make("proj/node_modules/x/index.js")
        out = m.tool_tag({"files": [self.rel(f)], "add": ["waiting"], "confirm": True})
        self.assertTrue(out.startswith("Refused"), out)
        self.assertEqual(m.read_tag_entries(f), [])

    def test_protected_and_folders_skipped(self):
        p = make("top secret plan.txt")
        d = os.path.join(TEST_DIR, "sub")
        os.makedirs(d)
        out = m.tool_tag({"files": [self.rel(p), self.rel(d)], "add": ["waiting"], "confirm": True})
        self.assertIn("protected", out)
        self.assertIn("folders aren't tagged", out)
        self.assertEqual(m.read_tag_entries(p), [])
        self.assertEqual(m.read_tag_entries(d), [])

    def test_awkward_filenames(self):
        f = make("it's a 🧪 \"test\" file (1).pdf")
        m.tool_tag({"files": [self.rel(f)], "add": ["🏠 Proof of address"], "confirm": True})
        self.assertEqual(names(f), ["🏠 Proof of address"])

    def test_undo_restores_exactly(self):
        f1 = make("a.txt", ["Work\n4"])
        f2 = make("b.txt")
        out = m.tool_tag({"files": [self.rel(f1), self.rel(f2)], "add": ["waiting"], "confirm": True})
        batch = batch_of(out)
        dry = m.tool_tag({"undo": batch})
        self.assertIn("PROPOSED UNDO", dry)
        self.assertEqual(names(f1), ["Work", "waiting"])
        m.tool_tag({"undo": batch, "confirm": True})
        self.assertEqual(names(f1), ["Work"])
        self.assertEqual(m.read_tag_entries(f2), [])
        out = subprocess.run(["/usr/bin/xattr", f2], capture_output=True, text=True).stdout
        self.assertNotIn("_kMDItemUserTags", out)

    def test_undo_survives_icloud_resetting_colours(self):
        f = make("a.txt", ["Work\n4"])
        batch = batch_of(m.tool_tag({"files": [self.rel(f)], "add": ["waiting"], "confirm": True}))
        m.write_tag_entries(f, ["Work\n1", "waiting\n1"])  # what iCloud sync does
        out = m.tool_tag({"undo": batch, "confirm": True})
        self.assertIn("Restored 1", out)
        self.assertEqual(names(f), ["Work"])

    def test_undo_skips_files_changed_since(self):
        f = make("a.txt")
        batch = batch_of(m.tool_tag({"files": [self.rel(f)], "add": ["waiting"], "confirm": True}))
        m.write_tag_entries(f, ["waiting\n7", "Mine\n0"])
        out = m.tool_tag({"undo": batch, "confirm": True})
        self.assertIn("changed since", out)
        self.assertEqual(names(f), ["waiting", "Mine"])

    def test_cli_undo(self):
        f = make("a.txt")
        batch = batch_of(m.tool_tag({"files": [self.rel(f)], "add": ["waiting"], "confirm": True}))
        env = dict(os.environ)
        r = subprocess.run(["/usr/bin/python3", os.path.join(REPO, "icloud_mcp.py"), "--undo", batch, "--confirm"],
                           capture_output=True, text=True, env=env)
        self.assertIn("Restored 1", r.stdout, r.stderr)
        self.assertEqual(m.read_tag_entries(f), [])

    def test_search_by_tag(self):
        f = make("searchable-zebra-note.txt")
        g = make("other-zebra-note.txt")
        m.tool_tag({"files": [self.rel(f)], "add": ["📊 Tax 2025-26"], "confirm": True})
        out = ""
        for _ in range(30):  # Spotlight indexes asynchronously
            out = m.tool_search({"tags": ["tax 2025-26"], "folder": TEST_DIR_NAME})
            if "searchable-zebra-note" in out:
                break
            time.sleep(1)
        self.assertIn("searchable-zebra-note", out)
        self.assertNotIn("other-zebra-note", out)
        both = m.tool_search({"query": "zebra", "names_only": True, "tags": ["📊 Tax 2025-26"],
                              "folder": TEST_DIR_NAME})
        self.assertIn("searchable-zebra-note", both)
        self.assertNotIn("other-zebra-note", both)


if __name__ == "__main__":
    unittest.main()
