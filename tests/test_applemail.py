"""Offline check for the Apple Mail bridge, against a fake Mail that answers the three scripts.
Run: python -m unittest tests.test_applemail
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import actions, compose, pipeline
from core.accounts import applemail as am, connector
from core.ai import llm as llm_mod
from core.db.migrations import init_db


def source(subject, minutes_ago=5):
    date = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
    return f"From: Prof <prof@ucf.edu>\nSubject: {subject}\nDate: {date}\nContent-Type: text/plain\n\nHello class"


class FakeMail:
    """Inbox and Deleted Items of one Mail account, driven by the same arguments as osascript."""
    def __init__(self):
        self.inbox = {"a1@ucf.edu": [False, source("Exam moved")], "a2@ucf.edu": [True, source("Office hours")]}
        self.deleted = {}
        self.calls = []

    def __call__(self, script, *args):
        self.calls.append((script, args))
        if script is am.LIST:
            return am.RS.join(f"{mid}{am.US}{'true' if read else 'false'}" for mid, (read, _) in self.inbox.items())
        if script is am.SOURCES:  # account, seconds back, then position/id pairs (positions are 1-based)
            order = list(self.inbox)
            pairs = zip(args[2::2], args[3::2])
            return am.RS.join(f"{mid}{am.US}{self.inbox[mid][1]}" for pos, mid in pairs
                              if int(pos) <= len(order) and order[int(pos) - 1] == mid)
        if script is am.SEND:
            return "ok"
        acct, verb, mid = args
        if verb == "untrash":
            self.inbox[mid] = self.deleted.pop(mid)
        elif verb == "trash":
            self.deleted[mid] = self.inbox.pop(mid)
        elif verb in ("read", "unread"):
            self.inbox[mid][0] = verb == "read"
        elif verb == "source":
            return self.inbox[mid][1]
        return "ok"


class TestAppleMail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with contextlib.redirect_stdout(io.StringIO()):
            self.db = init_db(os.path.join(self.tmp.name, "mail.db"))
        self.mail = FakeMail()
        patcher = mock.patch.object(am, "run_script", self.mail)
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(am, "mail_accounts", return_value=[("UCF", "giu@ucf.edu"), ("iCloud", "g@icloud.com")]):
            self.account = am.add_account(self.db, "UCF")
        self.conn = connector(self.db, self.account)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def sync(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.conn.sync()

    def rows(self):
        return dict((r[0], r[1:]) for r in self.db.execute(
            "SELECT remote_id, subject, unread, status, body_text FROM messages ORDER BY remote_id"))

    def test_add_account(self):
        self.assertEqual(self.db.execute("SELECT provider, address, auth_ref_in_keychain FROM accounts").fetchone(),
                         ("applemail", "giu@ucf.edu", "UCF"))
        self.assertIsInstance(self.conn, am.AppleMailConnector)
        with self.assertRaises(ValueError), mock.patch.object(am, "mail_accounts", return_value=[]):
            am.add_account(self.db, "Nope")

    def test_sync_reads_changes_and_removals(self):
        self.assertEqual(self.sync(), 2)
        self.assertEqual(self.rows(), {"a1@ucf.edu": ("Exam moved", 1, "new", "Hello class"),
                                       "a2@ucf.edu": ("Office hours", 0, "new", "Hello class")})
        self.mail.inbox["a1@ucf.edu"][0] = True       # read in Outlook on the phone
        del self.mail.inbox["a2@ucf.edu"]             # deleted elsewhere
        self.assertEqual(self.sync(), 0)              # nothing new to download
        self.assertEqual(self.rows()["a1@ucf.edu"][1], 0)
        self.assertEqual(self.rows()["a2@ucf.edu"][2], "trashed")

    def test_shifted_positions_never_store_the_wrong_email(self):
        real = self.mail.__call__

        def arrives_mid_sync(script, *args):  # a new email lands on top between listing and download
            if script is am.SOURCES and "z0@ucf.edu" not in self.mail.inbox:
                self.mail.inbox = {"z0@ucf.edu": [False, source("Just arrived")], **self.mail.inbox}
            return real(script, *args)
        with mock.patch.object(am, "run_script", arrives_mid_sync):
            self.assertEqual(self.sync(), 0)  # every position shifted: nothing stored, nothing wrong
            self.assertEqual(self.sync(), 3)  # next sync sees the new order
        self.assertEqual(self.rows()["a1@ucf.edu"][0], "Exam moved")

    def test_missing_copies_are_filled_in(self):
        self.sync()
        self.db.execute("DELETE FROM message_raw")  # as if synced before copies were kept
        self.db.commit()
        self.assertEqual(self.sync(), 0)  # nothing new...
        self.assertEqual(self.db.execute("SELECT count(*) FROM message_raw").fetchone()[0], 2)  # ...copies back

    def test_read_trash_undo_go_through_mail(self):
        self.sync()
        mid = self.db.execute("SELECT id FROM messages WHERE remote_id='a1@ucf.edu'").fetchone()[0]
        self.conn.mark_read("a1@ucf.edu")
        self.assertTrue(self.mail.inbox["a1@ucf.edu"][0])
        actions.trash_message(self.db, mid, confirmed=True)
        self.assertIn("a1@ucf.edu", self.mail.deleted)
        actions.undo(self.db)
        self.assertIn("a1@ucf.edu", self.mail.inbox)
        from core import mail
        self.assertEqual(mail.content(self.conn.fetch_raw("a1@ucf.edu"))["Subject"], "Exam moved")
        # the copy kept at sync means reading never asks Mail
        before = len(self.mail.calls)
        self.assertEqual(mail.content(mail.raw_for(self.db, mid))["Subject"], "Exam moved")
        self.assertEqual(len(self.mail.calls), before)
        self.assertEqual(self.conn.web_link("a1@ucf.edu"), "message://%3ca1@ucf.edu%3e")

    def test_reply_sends_through_mail(self):
        self.sync()
        mid = self.db.execute("SELECT id FROM messages WHERE remote_id='a1@ucf.edu'").fetchone()[0]
        fields = compose.prefill(self.db, mid, "reply")  # from the local copy: no Mail call needed
        self.assertEqual((fields["to"], fields["subject"]), ("prof@ucf.edu", "Re: Exam moved"))
        fields["body"] = "Thanks!" + fields["body"]
        with mock.patch.object(self.mail, "__call__", wraps=self.mail.__call__):
            actions.send_email(self.db, self.account, fields, confirmed=True)
        script, args = self.mail.calls[-1]
        self.assertIs(script, am.SEND)
        self.assertEqual(args[:5], ("UCF", "reply", "a1@ucf.edu", "prof@ucf.edu", ""))
        self.assertTrue(args[6].startswith("Thanks!"))

    def test_pipeline_sorts_mail_accounts_too(self):
        with mock.patch.object(llm_mod.LocalLLM, "generate_json", return_value={"section": "Updates"}), \
             mock.patch.object(llm_mod.LocalLLM, "unload"), contextlib.redirect_stdout(io.StringIO()):
            errors = pipeline.refresh(self.db)
        self.assertEqual([e for e in errors if not e.startswith("events")], [])
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages WHERE status='classified'").fetchone()[0], 2)
        ok_scripts = {am.LIST, am.SOURCES}
        self.assertTrue(all(script in ok_scripts for script, _ in self.mail.calls))  # refresh never trashes or edits


if __name__ == "__main__":
    unittest.main()
