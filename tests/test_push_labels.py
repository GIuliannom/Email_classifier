"""Offline check for push-labels and the label-permission login (Gmail is faked).
Run: python -m unittest tests.test_push_labels
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import actions
from core.accounts import gmail
from core.accounts.gmail import GmailConnector
from core.db.migrations import init_db

READONLY = 'https://www.googleapis.com/auth/gmail.readonly'


def token(scopes):
    return json.dumps({"refresh_token": "r", "client_id": "c", "client_secret": "s",
                       "token_uri": "https://oauth2.googleapis.com/token", "scopes": scopes})


def fake_gmail():
    svc = mock.MagicMock()
    labels = svc.users.return_value.labels.return_value
    labels.list.return_value.execute.return_value = {"labels": [
        {"name": "INBOX", "id": "INBOX"}, {"name": "AI", "id": "L0"}, {"name": "AI/Ads", "id": "L1"}]}
    labels.create.return_value.execute.return_value = {"id": "L2"}
    return svc


class TestPushLabels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with contextlib.redirect_stdout(io.StringIO()):
            self.db = init_db(os.path.join(self.tmp.name, "mail.db"))
        self.db.execute("INSERT INTO accounts (provider, address, auth_ref_in_keychain) VALUES ('gmail', 'me@x.com', 'ref')")
        for i in (1, 2, 3, 4):
            self.db.execute("INSERT INTO messages (id, account_id, remote_id) VALUES (?, 1, ?)", (i, f"r{i}"))
        self.db.commit()
        for msg_id, section in ((1, "Ads"), (2, "Banks"), (4, "Ads")):  # 3 stays Unsorted
            actions.move_message(self.db, msg_id, section, "move_section", 0.9)
        self.svc = fake_gmail()
        patcher = mock.patch.object(GmailConnector, "_service", return_value=self.svc)
        self.service = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def batches(self):
        return [c.kwargs["body"] for c in self.svc.users.return_value.messages.return_value.batchModify.call_args_list]

    def test_dry_run_touches_nothing(self):
        counts = GmailConnector(1, self.db).push_labels(dry_run=True)
        self.assertEqual(counts, {(None, "Ads"): 2, (None, "Banks"): 1})
        self.service.assert_not_called()

    def test_push_then_corrections(self):
        conn = GmailConnector(1, self.db)
        conn.push_labels()
        # only the missing label is created; AI and AI/Ads already exist
        created = [c.kwargs["body"]["name"] for c in self.svc.users.return_value.labels.return_value.create.call_args_list]
        self.assertEqual(created, ["AI/Banks"])
        self.assertCountEqual(self.batches(), [{"ids": ["r1", "r4"], "addLabelIds": ["L1"]},
                                               {"ids": ["r2"], "addLabelIds": ["L2"]}])
        self.assertEqual(conn.push_labels(), {})  # Gmail already matches

        # your move and an undo both carry through on the next push
        self.svc.users.return_value.labels.return_value.list.return_value.execute.return_value["labels"].append(
            {"name": "AI/Banks", "id": "L2"})
        actions.move_message(self.db, 1, "Banks", "user_move")
        actions.undo(self.db, self.db.execute("SELECT id FROM actions_log WHERE message_id=2").fetchone()[0])
        self.svc.reset_mock()
        conn.push_labels()
        self.assertCountEqual(self.batches(), [{"ids": ["r1"], "addLabelIds": ["L2"], "removeLabelIds": ["L1"]},
                                               {"ids": ["r2"], "removeLabelIds": ["L2"]}])
        self.assertEqual(self.db.execute("SELECT remote_label FROM messages WHERE id=2").fetchone()[0], None)


class TestLabelPermission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with contextlib.redirect_stdout(io.StringIO()):
            self.db = init_db(os.path.join(self.tmp.name, "mail.db"))
        self.db.execute("INSERT INTO accounts (provider, address, auth_ref_in_keychain) VALUES ('gmail', 'me@x.com', 'ref')")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_read_only_token_still_syncs_but_cannot_label(self):
        with mock.patch.object(gmail.keyring, "get_password", return_value=token([READONLY])), \
             mock.patch.object(gmail, "build") as build:
            GmailConnector(1, self.db)._service()  # sync path works
            with self.assertRaisesRegex(RuntimeError, "add-account"):
                GmailConnector(1, self.db)._service(need_modify=True)
        self.assertEqual(build.call_count, 1)

    def test_add_account_logs_in_again_when_a_permission_is_missing(self):
        flow = mock.Mock()
        flow.run_local_server.return_value.to_json.return_value = token(gmail.SCOPES)
        with mock.patch.object(gmail.keyring, "get_password", return_value=token([READONLY])), \
             mock.patch.object(gmail.keyring, "set_password") as saved, \
             mock.patch.object(gmail.InstalledAppFlow, "from_client_secrets_file", return_value=flow), \
             mock.patch.object(gmail, "build") as build, contextlib.redirect_stdout(io.StringIO()):
            build.return_value.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": "me@x.com"}
            self.assertEqual(gmail.add_account(self.db), 1)  # same account row, no duplicate
        flow.run_local_server.assert_called_once()
        saved.assert_called_once()


if __name__ == "__main__":
    unittest.main()
