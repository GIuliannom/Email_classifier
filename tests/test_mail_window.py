"""Offline check for the mail window's engine side: unread tracking, changes made elsewhere,
and the /mailboxes, /messages, /message endpoints (Gmail faked).
Run: python -m unittest tests.test_mail_window
"""
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import server
from core.accounts.gmail import GmailConnector
from core.db.migrations import init_db


def raw(mid, subject, labels=()):
    eml = (f"From: Alice <a@x.com>\r\nSubject: {subject}\r\nMIME-Version: 1.0\r\n"
           "Content-Type: multipart/alternative; boundary=B\r\n\r\n--B\r\nContent-Type: text/plain\r\n\r\nplain\r\n"
           "--B\r\nContent-Type: text/html\r\n\r\n<p>html</p>\r\n--B--\r\n").encode()
    return {"id": mid, "threadId": "t", "internalDate": str(int(time.time() * 1000)), "labelIds": list(labels),
            "raw": base64.urlsafe_b64encode(eml).decode()}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "mail.db")
        with contextlib.redirect_stdout(io.StringIO()):
            self.db = init_db(self.db_path)
        self.db.execute("INSERT INTO accounts (provider, address, auth_ref_in_keychain) VALUES ('gmail', 'me@x.com', 'ref')")
        self.db.commit()
        self.svc = mock.MagicMock()
        users = self.svc.users.return_value
        users.getProfile.return_value.execute.return_value = {"historyId": "1"}
        self.store = {"m1": raw("m1", "New one", ["UNREAD", "INBOX"]), "m2": raw("m2", "Read one", ["INBOX"])}
        users.messages.return_value.get.side_effect = lambda userId, id, format: mock.Mock(execute=lambda **_: self.store[id])
        self.lists = {"newer_than:30d": ["m1", "m2"], "is:unread": ["m1"]}
        users.messages.return_value.list.side_effect = lambda userId, q, **_: mock.Mock(
            execute=lambda **_: {"messages": [{"id": i} for i in self.lists[q]]})
        patcher = mock.patch.object(GmailConnector, "_service", return_value=self.svc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def sync(self):
        with contextlib.redirect_stdout(io.StringIO()):
            GmailConnector(1, self.db).sync()

    def row(self, rid):
        return self.db.execute("SELECT unread, status FROM messages WHERE remote_id=?", (rid,)).fetchone()


class TestUnreadTracking(Base):
    def test_unread_from_sync_and_changes_made_elsewhere(self):
        self.sync()
        self.assertEqual((self.row("m1")[0], self.row("m2")[0]), (1, 0))
        self.db.execute("UPDATE messages SET status='classified'")
        self.db.commit()
        # later: m1 read on the phone, m2 trashed on the web, then m2 restored
        self.svc.users.return_value.history.return_value.list.return_value.execute.return_value = {
            "historyId": "2", "history": [
                {"labelsRemoved": [{"message": {"id": "m1"}, "labelIds": ["UNREAD"]}]},
                {"labelsAdded": [{"message": {"id": "m2"}, "labelIds": ["TRASH"]}]}]}
        self.sync()
        self.assertEqual(self.row("m1"), (0, "classified"))
        self.assertEqual(self.row("m2"), (0, "trashed"))
        self.svc.users.return_value.history.return_value.list.return_value.execute.return_value = {
            "historyId": "3", "history": [{"labelsRemoved": [{"message": {"id": "m2"}, "labelIds": ["TRASH"]}]}]}
        self.sync()
        self.assertEqual(self.row("m2"), (0, "new"))  # back in the inbox and never sorted: it gets sorted

    def test_one_time_fill_for_older_messages(self):
        for rid in ("m1", "m2"):  # stored before unread was tracked
            self.db.execute("INSERT INTO messages (account_id, remote_id, status) VALUES (1, ?, 'classified')", (rid,))
        self.db.commit()
        self.sync()
        self.assertEqual((self.row("m1")[0], self.row("m2")[0]), (1, 0))


class TestEndpoints(Base):
    def setUp(self):
        super().setUp()
        self.sync()
        self.db.execute("INSERT INTO messages (account_id, remote_id, subject, status, unread) "
                        "VALUES (1, 'm3', 'Gone', 'trashed', 1)")
        self.db.commit()
        server.Handler.token, server.Handler.db_path = "secret", self.db_path
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, headers={"X-Token": "secret"},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_mailboxes_and_lists(self):
        (acct,) = self.call("/mailboxes")[1]["accounts"]
        self.assertEqual(acct["sections"], [{"name": "Unsorted", "total": 2, "unread": 1}])  # trashed left out
        msgs = self.call("/messages?account=1&section=unsorted")[1]["messages"]
        self.assertEqual([(m["subject"], m["unread"]) for m in msgs], [("New one", True), ("Read one", False)])
        self.assertEqual(self.call("/messages?section=Ads")[1]["messages"], [])

    def test_open_mark_read_and_move(self):
        mid = self.db.execute("SELECT id FROM messages WHERE remote_id='m1'").fetchone()[0]
        code, content = self.call(f"/message/{mid}")
        self.assertEqual((code, content["html"].strip(), content["Subject"]), (200, "<p>html</p>", "New one"))

        self.assertEqual(self.call(f"/message/{mid}/read", {})[0], 200)
        body = self.svc.users.return_value.messages.return_value.modify.call_args.kwargs["body"]
        self.assertEqual(body, {"removeLabelIds": ["UNREAD"]})
        self.assertEqual(self.row("m1")[0], 0)

        self.assertEqual(self.call(f"/message/{mid}/move", {"section": "Ads"})[0], 200)
        self.assertEqual(self.db.execute("SELECT action FROM actions_log").fetchone()[0], "user_move")
        self.assertEqual(self.call(f"/message/{mid}/move", {"section": "Nope"})[0], 400)
        self.assertEqual(self.call("/message/999")[0], 400)

    def test_offline_falls_back_to_stored_text(self):
        mid = self.db.execute("SELECT id FROM messages WHERE remote_id='m2'").fetchone()[0]
        from core import mail
        with mock.patch.object(mail, "raw_for", side_effect=OSError("offline")):
            content = self.call(f"/message/{mid}")[1]
        self.assertEqual((content["html"], content["text"]), (None, "plain"))


if __name__ == "__main__":
    unittest.main()
