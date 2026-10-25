"""Offline check that moves are validated, logged and undoable.
Run: python -m unittest tests.test_actions
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import actions, cli
from core.db.migrations import init_db


class TestActions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with contextlib.redirect_stdout(io.StringIO()):
            self.db = init_db(os.path.join(self.tmp.name, "mail.db"))
        self.db.execute("INSERT INTO accounts (provider, address, auth_ref_in_keychain) VALUES ('gmail', 'me@x.com', 'ref')")
        for i in (1, 2):
            self.db.execute("INSERT INTO messages (id, account_id, remote_id, subject) VALUES (?, 1, ?, ?)", (i, f"m{i}", f"S{i}"))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def state(self, msg_id):
        return self.db.execute("SELECT s.name, m.status FROM messages m LEFT JOIN sections s ON s.id = m.section_id "
                               "WHERE m.id=?", (msg_id,)).fetchone()

    def count_log(self):
        return self.db.execute("SELECT COUNT(*) FROM actions_log").fetchone()[0]

    def test_validator_rejects_and_writes_nothing(self):
        for args in [(1, "Ads", "delete_all"), (1, "Shopping", "user_move"), (99, "Ads", "user_move")]:
            with self.assertRaises(actions.ActionError):
                actions.move_message(self.db, *args)
        self.assertEqual(self.count_log(), 0)
        self.assertEqual(self.state(1), (None, "new"))

    def test_move_and_undo_restore_state(self):
        actions.move_message(self.db, 1, "ads", "move_section", 0.9)  # section name is case-insensitive
        self.assertEqual(self.state(1), ("Ads", "classified"))
        self.assertIn("Undid move_section of message 1 to Ads", actions.undo(self.db))
        self.assertEqual(self.state(1), (None, "new"))
        with self.assertRaises(actions.ActionError):
            actions.undo(self.db, 1)  # already undone
        with self.assertRaises(actions.ActionError):
            actions.undo(self.db)     # nothing left

    def test_only_newest_action_on_a_message_can_be_undone(self):
        auto = actions.move_message(self.db, 1, "Banks", "move_section", 0.8)
        actions.move_message(self.db, 1, "Ads", "user_move")
        with self.assertRaises(actions.ActionError):
            actions.undo(self.db, auto)
        actions.undo(self.db)  # your move first...
        self.assertEqual(self.state(1), ("Banks", "classified"))
        actions.undo(self.db, auto)  # ...then the classifier's
        self.assertEqual(self.state(1), (None, "new"))

    def test_undo_all_auto_keeps_your_moves(self):
        actions.move_message(self.db, 1, "Banks", "move_section", 0.8)
        actions.move_message(self.db, 2, "Banks", "move_section", 0.8)
        actions.move_message(self.db, 2, "Ads", "user_move")
        out = io.StringIO()
        with mock.patch.object(cli, "init_db", return_value=self.db), contextlib.redirect_stdout(out):
            cli.main(["undo", "--all-auto"])
        self.assertIn("Undid 1 classifier moves; kept 1", out.getvalue())
        self.assertEqual(self.state(1), (None, "new"))
        self.assertEqual(self.state(2), ("Ads", "classified"))


class TestResort(TestActions):
    def test_resort_one_account_keeps_your_moves(self):
        from core.ai import classify, llm as llm_mod
        self.db.execute("INSERT INTO accounts (provider, address, auth_ref_in_keychain) VALUES ('applemail', 'u@ucf.edu', 'Exchange')")
        for i in (3, 4):
            self.db.execute("INSERT INTO messages (id, account_id, remote_id, subject) VALUES (?, 2, ?, ?)", (i, f"m{i}", f"S{i}"))
        self.db.commit()
        actions.move_message(self.db, 1, "Personal", "move_section", 0.9)   # other account: untouched
        actions.move_message(self.db, 3, "Personal", "move_section", 0.9)   # classifier: redone
        actions.move_message(self.db, 4, "Personal", "move_section", 0.9)
        actions.move_message(self.db, 4, "Banks", "user_move")               # yours: kept
        with mock.patch.object(llm_mod.LocalLLM, "generate_json", return_value={"section": "School", "confidence": 0.95}), \
             contextlib.redirect_stdout(io.StringIO()):
            placed = classify.resort(self.db, 2, labels_path=os.path.join(self.tmp.name, "none.csv"))
        self.assertEqual(placed, {"School": 1})
        self.assertEqual([self.state(i)[0] for i in (1, 3, 4)], ["Personal", "School", "Banks"])


if __name__ == "__main__":
    unittest.main()
