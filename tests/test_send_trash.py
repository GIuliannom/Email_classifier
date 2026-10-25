"""Offline check for trash / archive / junk / star / reply / forward / send: confirmation where
required, logged, undoable (Gmail faked).
Run: python -m unittest tests.test_send_trash
"""
import contextlib
import email
import email.policy
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import actions, cli, compose, mail
from core.accounts.gmail import GmailConnector
from core.db.migrations import init_db

ORIGINAL = (b"From: Alice <alice@x.com>\r\nReply-To: replies@x.com\r\nTo: me@x.com, bob@y.com\r\nCc: carol@z.com\r\n"
            b"Subject: Lunch?\r\nDate: Mon, 19 Oct 2026 12:00:00 +0000\r\nMessage-ID: <m1@x.com>\r\n"
            b"References: <m0@x.com>\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
            b"--B\r\nContent-Type: text/plain\r\n\r\nNoon at the cafe?\r\n"
            b"--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=menu.pdf\r\n\r\nPDFDATA\r\n--B--\r\n")


class TestSendTrash(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with contextlib.redirect_stdout(io.StringIO()):
            self.db = init_db(os.path.join(self.tmp.name, "mail.db"))
        self.db.execute("INSERT INTO accounts (provider, address, auth_ref_in_keychain) VALUES ('gmail', 'me@x.com', 'ref')")
        self.db.execute("INSERT INTO messages (id, account_id, remote_id, thread_id, sender, subject, status, section_id) "
                        "VALUES (1, 1, 'r1', 'T1', 'Alice <alice@x.com>', 'Lunch?', 'classified', "
                        "(SELECT id FROM sections WHERE name='Personal'))")
        self.db.commit()
        mail.store_raw(self.db, 1, ORIGINAL)  # the local copy prefill and forward read from
        self.gmail = {name: mock.patch.object(GmailConnector, name).start()
                      for name in ("trash", "untrash", "archive", "unarchive", "mark_junk", "not_junk",
                                   "set_star", "send")}
        self.gmail["send"].return_value = "sent1"
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def status(self):
        return self.db.execute("SELECT status FROM messages WHERE id=1").fetchone()[0]

    def sent(self):
        mime_bytes, thread_id = self.gmail["send"].call_args.args
        return email.message_from_bytes(mime_bytes, policy=email.policy.default), thread_id

    def cli(self, argv, answers=(), tty=True):
        out = io.StringIO()
        with mock.patch.object(cli, "init_db", return_value=self.db), \
             mock.patch.object(cli.sys.stdin, "isatty", return_value=tty), \
             mock.patch("builtins.input", side_effect=list(answers)), \
             mock.patch.object(cli, "edit_text", side_effect=lambda text: text + " (edited)") as edit, \
             contextlib.redirect_stdout(out):
            cli.main(argv)
        return out.getvalue(), edit

    def test_trash_and_send_refuse_without_confirmation(self):
        with self.assertRaises(actions.ActionError):
            actions.trash_message(self.db, 1)
        with self.assertRaises(actions.ActionError):
            actions.send_email(self.db, 1, {"to": "a@b.c"}, confirmed="yes")  # only True counts
        self.gmail["trash"].assert_not_called()
        self.gmail["send"].assert_not_called()

    def test_trash_archive_junk_restore_and_undo(self):
        actions.trash_message(self.db, 1, confirmed=True)
        self.gmail["trash"].assert_called_once_with("r1")
        self.assertEqual(self.status(), "trashed")
        actions.undo(self.db)
        self.gmail["untrash"].assert_called_once_with("r1")
        self.assertEqual(self.status(), "classified")

        actions.change_state(self.db, 1, "junk")
        self.assertEqual(self.status(), "junk")
        actions.change_state(self.db, 1, "archive")  # junk -> archive: out of Junk first, then archived
        self.gmail["not_junk"].assert_called_once_with("r1")
        self.gmail["archive"].assert_called_once_with("r1")
        actions.change_state(self.db, 1, "restore")
        self.gmail["unarchive"].assert_called_once_with("r1")
        self.assertEqual(self.status(), "classified")
        with self.assertRaises(actions.ActionError):
            actions.change_state(self.db, 1, "restore")  # already in the inbox

    def test_star_and_undo(self):
        actions.set_star(self.db, 1, True)
        self.gmail["set_star"].assert_called_with("r1", True)
        self.assertEqual(self.db.execute("SELECT starred FROM messages WHERE id=1").fetchone()[0], 1)
        actions.undo(self.db)
        self.gmail["set_star"].assert_called_with("r1", False)
        self.assertEqual(self.db.execute("SELECT starred FROM messages WHERE id=1").fetchone()[0], 0)

    def test_reply_all_is_threaded_and_cannot_be_undone(self):
        fields = compose.prefill(self.db, 1, "replyall")
        self.assertEqual((fields["to"], fields["cc"], fields["subject"]), ("replies@x.com", "bob@y.com, carol@z.com", "Re: Lunch?"))
        self.assertIn("> Noon at the cafe?", fields["body"])
        fields["body"] = "Sure!" + fields["body"]
        actions.send_email(self.db, 1, fields, confirmed=True)
        sent, thread = self.sent()
        self.assertEqual((sent["In-Reply-To"], sent["References"], thread), ("<m1@x.com>", "<m0@x.com> <m1@x.com>", "T1"))
        self.assertTrue(sent.get_content().startswith("Sure!"))
        sent_id = self.db.execute("SELECT id FROM actions_log WHERE action='send'").fetchone()[0]
        with self.assertRaisesRegex(actions.ActionError, "can't be unsent"):
            actions.undo(self.db, sent_id)

    def test_forward_carries_attachments(self):
        fields = compose.prefill(self.db, 1, "forward")
        self.assertEqual(fields["subject"], "Fwd: Lunch?")
        fields["to"] = "dave@w.com"
        actions.send_email(self.db, 1, fields, confirmed=True)
        sent, thread = self.sent()
        self.assertIsNone(thread)  # a forward starts its own conversation
        self.assertEqual([a.get_filename() for a in sent.iter_attachments()], ["menu.pdf"])

    def test_bad_recipient(self):
        with self.assertRaises(ValueError):
            compose.build_mime(self.db, 1, {"mode": "new", "to": "not an address", "subject": "Hi", "body": "x"})

    def test_cli_asks_and_honours_the_answer(self):
        out, _ = self.cli(["trash", "1"], answers=["n"])
        self.assertIn("Nothing changed", out)
        self.cli(["reply", "1", "--body", "Hi"], answers=[""])  # Enter = no
        self.gmail["trash"].assert_not_called()
        self.gmail["send"].assert_not_called()

        out, edit = self.cli(["reply", "1", "--body", "Hi"], answers=["e", "y"])
        edit.assert_called_once_with("Hi")
        sent, thread = self.sent()
        self.assertTrue(sent.get_content().startswith("Hi (edited)"))
        self.assertEqual(thread, "T1")

    def test_cli_refuses_without_a_terminal(self):
        out, _ = self.cli(["send", "--to", "a@b.com", "--subject", "x", "--body", "y"], tty=False)
        self.assertIn("Refusing", out)
        self.gmail["send"].assert_not_called()

    def test_trashed_mail_leaves_summaries(self):
        from core.ai import summarize
        self.db.execute("UPDATE messages SET date='2026-10-19T12:00:00', status='trashed' WHERE id=1")
        self.assertEqual(summarize.generate_summary(self.db, 1, "daily", ("2026-10-19", "2026-10-20")),
                         "No emails found for this period.")


if __name__ == "__main__":
    unittest.main()
