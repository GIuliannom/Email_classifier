"""Offline check for the size cap: oldest bodies go first, metadata stays, the file shrinks.
Run: python -m unittest tests.test_prune
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db.migrations import init_db
from core.db.prune import prune_if_large


class TestPrune(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "mail.db")
        with contextlib.redirect_stdout(io.StringIO()):
            self.db = init_db(self.path)
        self.db.execute("INSERT INTO accounts (provider, address, auth_ref_in_keychain) VALUES ('gmail', 'me@x.com', 'ref')")
        for i in range(200):  # ~2 MB of bodies; date order = id order
            self.db.execute("INSERT INTO messages (id, account_id, remote_id, subject, body_text, date) "
                            "VALUES (?, 1, ?, ?, ?, ?)", (i + 1, f"r{i}", f"S{i}", "é" * 5000,
                                                          f"2026-01-01T00:00:{i:03d}"))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_small_file_is_left_alone(self):
        self.assertEqual(prune_if_large(self.db), 0)  # default 5 GB limit
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages WHERE body_text IS NULL").fetchone()[0], 0)

    def test_prunes_oldest_until_under_target(self):
        size = os.path.getsize(self.path)
        target = size // 2
        n = prune_if_large(self.db, prune_at=size - 1, prune_to=target)
        self.assertGreater(n, 0)
        self.assertLessEqual(os.path.getsize(self.path), target)  # VACUUM really shrank it
        pruned = [i for (i,) in self.db.execute("SELECT id FROM messages WHERE body_text IS NULL ORDER BY id")]
        self.assertEqual(pruned, list(range(1, n + 1)))  # strictly the oldest
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages WHERE subject IS NOT NULL").fetchone()[0], 200)


if __name__ == "__main__":
    unittest.main()
