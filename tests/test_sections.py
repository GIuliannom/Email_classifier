"""Offline check for per-account sections (e.g. no Banks for UCF).
Run: python -m unittest tests.test_sections
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import actions, sections, server
from core.ai import classify, llm as llm_mod
from core.db.migrations import init_db


class TestSections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with contextlib.redirect_stdout(io.StringIO()):
            self.db = init_db(os.path.join(self.tmp.name, "mail.db"))
        for addr in ("me@gmail.com", "me@ucf.edu"):
            self.db.execute("INSERT INTO accounts (provider, address, auth_ref_in_keychain) VALUES ('gmail', ?, 'r')", (addr,))
        for msg_id, account in ((1, 1), (2, 2), (3, 2)):
            self.db.execute("INSERT INTO messages (id, account_id, remote_id, subject) VALUES (?, ?, ?, 'S')",
                            (msg_id, account, f"m{msg_id}"))
        self.db.commit()
        for msg_id in (1, 2, 3):
            actions.move_message(self.db, msg_id, "Banks", "move_section", 0.9)
        actions.move_message(self.db, 3, "Work", "user_move")
        actions.move_message(self.db, 3, "Banks", "user_move")  # you put it in Banks yourself, last
        self.offered = []

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def fake_model(self, prompt, schema, max_retries=1, json_schema=None):  # bound: no model object passed
        self.offered.append(json_schema["properties"]["section"]["enum"])
        return {"section": "Banks" if "Banks" in self.offered[-1] else "Work", "confidence": 0.95}

    def section(self, msg_id):
        return self.db.execute("SELECT s.name FROM messages m LEFT JOIN sections s ON s.id = m.section_id "
                               "WHERE m.id=?", (msg_id,)).fetchone()[0]

    def test_hide_resorts_only_that_account(self):
        self.assertEqual(sections.hide(self.db, 2, "banks"), 2)
        self.assertEqual(sections.hidden(self.db, 2), ["Banks"])
        self.assertEqual([self.section(i) for i in (1, 2, 3)], ["Banks", None, "Work"])  # 3 falls back to your Work
        with mock.patch.object(llm_mod.LocalLLM, "generate_json", self.fake_model), contextlib.redirect_stdout(io.StringIO()):
            classify.classify_all_new(self.db, account_id=2, labels_path="none.csv")
            classify.classify_message(self.db, 1, apply=False, labels_path="none.csv")
        self.assertNotIn("Banks", self.offered[0])  # never offered for the UCF account
        self.assertIn("Banks", self.offered[-1])    # still offered for Gmail
        self.assertEqual(self.section(2), "Work")

    def test_moves_and_window_respect_hidden(self):
        sections.hide(self.db, 2, "Banks")
        with self.assertRaisesRegex(actions.ActionError, "hidden"):
            actions.move_message(self.db, 2, "Banks", "user_move")
        boxes = {a["address"]: a["available"] for a in server.mailboxes(self.db)}
        self.assertNotIn("Banks", boxes["me@ucf.edu"])
        self.assertIn("Banks", boxes["me@gmail.com"])
        self.assertIn("Work", boxes["me@ucf.edu"])  # offered even before it has any mail
        sections.show(self.db, 2, "Banks")
        actions.move_message(self.db, 2, "Banks", "user_move")

    def test_bad_names(self):
        for name in ("Unsorted", "Nope"):
            with self.assertRaises(ValueError):
                sections.hide(self.db, 2, name)


if __name__ == "__main__":
    unittest.main()
