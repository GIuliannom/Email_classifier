"""Offline check for the mail-app features: reading (embedded pictures, attachments), search,
folders, and the window's endpoints for compose/send/trash/undo/images (Gmail faked).
Run: python -m unittest tests.test_mail_app
"""
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import mail, server
from core.accounts.gmail import GmailConnector
from core.db.migrations import init_db

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
RAW = (b"From: Bob <bob@shop.com>\r\nTo: me@x.com\r\nSubject: Your receipt\r\nDate: Mon, 19 Oct 2026 12:00:00 +0000\r\n"
       b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=M\r\n\r\n"
       b"--M\r\nContent-Type: multipart/related; boundary=R\r\n\r\n"
       b"--R\r\nContent-Type: text/html\r\n\r\n<p>Thanks</p><img src=\"cid:logo1\">\r\n"
       b"--R\r\nContent-Type: image/png\r\nContent-ID: <logo1>\r\nContent-Transfer-Encoding: base64\r\n\r\n"
       + base64.b64encode(PNG) + b"\r\n--R--\r\n"
       b"--M\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=receipt.pdf\r\n\r\nPDF\r\n--M--\r\n")


class TestMailApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "mail.db")
        with contextlib.redirect_stdout(io.StringIO()):
            self.db = init_db(self.db_path)
        self.db.execute("INSERT INTO accounts (provider, address, auth_ref_in_keychain) VALUES ('gmail', 'me@x.com', 'ref')")
        rows = [(1, "Bob <bob@shop.com>", "Your receipt", "receipt for the lunch order", "classified", 0),
                (2, "Ann <ann@x.com>", "Dinner plans", "dinner at eight", "classified", 1),
                (3, "Spam <s@spam.com>", "You won", "lunch money prize", "junk", 0),
                (4, "Old <o@x.com>", "Archived lunch", "lunch notes", "archived", 0),
                (5, "Gone <g@x.com>", "Deleted lunch", "lunch", "trashed", 0)]
        for i, sender, subject, body, status, starred in rows:
            self.db.execute("INSERT INTO messages (id, account_id, remote_id, sender, subject, body_text, status, starred, date) "
                            "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)", (i, f"r{i}", sender, subject, body, status, starred,
                                                                    f"2026-10-1{i}T12:00:00"))
        self.db.commit()
        mail.store_raw(self.db, 1, RAW)
        server.Handler.token, server.Handler.db_path = "secret", self.db_path
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.gmail = {n: mock.patch.object(GmailConnector, n).start() for n in ("trash", "untrash", "send", "fetch_raw")}
        self.gmail["send"].return_value = "sent1"
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.db.close()
        self.tmp.cleanup()

    def call(self, path, body=None, raw=False):
        req = urllib.request.Request(self.base + path, headers={"X-Token": "secret"},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                data = r.read()
                return (r.status, data, r.headers) if raw else (r.status, json.loads(data))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def subjects(self, query):
        return [m["subject"] for m in self.call("/messages?" + query)[1]["messages"]]

    def test_reading_shows_embedded_pictures_and_lists_attachments(self):
        c = mail.content(RAW)
        self.assertIn('src="data:image/png;base64,', c["html"])
        self.assertNotIn("cid:", c["html"])
        (att,) = c["attachments"]
        self.assertEqual((att["filename"], att["type"]), ("receipt.pdf", "application/pdf"))
        code, data, headers = self.call(f"/message/1/attachment/{att['index']}", raw=True)
        self.assertEqual((code, data.strip(), headers["X-Filename"]), (200, b"PDF", "receipt.pdf"))
        self.gmail["fetch_raw"].assert_not_called()  # read from the local copy

    def test_search_and_folders(self):
        self.assertEqual(self.subjects("q=lunch"), ["Archived lunch", "Your receipt"])  # not Junk or Trash
        self.assertEqual(self.subjects("q=ann"), ["Dinner plans"])                     # sender too
        self.assertEqual(self.subjects("q=rece"), ["Your receipt"])                    # prefixes
        self.assertEqual(self.subjects("q=%22%29%28"), [])                             # junk input is safe
        self.assertEqual(self.subjects("folder=starred"), ["Dinner plans"])
        self.assertEqual(self.subjects("folder=junk"), ["You won"])
        self.assertEqual(self.subjects("folder=archive"), ["Archived lunch"])
        self.assertEqual(self.subjects("folder=trash"), ["Deleted lunch"])
        self.assertEqual(self.subjects("folder=inbox"), ["Dinner plans", "Your receipt"])
        self.db.execute("UPDATE messages SET body_text=NULL WHERE id=1")  # pruned text leaves the index
        self.db.commit()
        self.assertEqual(self.subjects("q=order"), [])
        (acct,) = self.call("/mailboxes")[1]["accounts"]
        self.assertEqual(acct["folders"], {"unread": 0, "starred": 1, "archive": 1, "junk": 1, "trash": 1})

    def test_snippets_skip_banners_and_links(self):
        self.assertEqual(server.snippet("CAUTION: This email originated from outside of UCF. Only visit links or open "
                                        "attachments from senders you recognize. &nbsp; Dear&nbsp;Ann, see "
                                        "https://x.y/z?a=1 for details"), "Dear Ann, see for details")

    def test_trash_undo_compose_send_images(self):
        code, body = self.call("/message/1/trash", {})
        self.assertEqual(code, 200)
        self.gmail["trash"].assert_called_once_with("r1")
        self.assertEqual(self.call("/undo", {"action_id": body["action_id"]})[0], 200)
        self.gmail["untrash"].assert_called_once_with("r1")

        fields = self.call("/message/1/compose?mode=forward")[1]
        self.assertEqual(fields["subject"], "Fwd: Your receipt")
        fields.update(to="dave@w.com", body="FYI" + fields["body"])
        self.assertEqual(self.call("/send", fields)[0], 200)
        self.gmail["send"].assert_called_once()
        self.assertEqual(self.call("/send", {"account": 1, "to": "nope", "subject": "x", "body": "y"})[0], 400)

        self.assertTrue(self.call("/message/1")[1]["images_allowed"])    # images load by default
        self.call("/settings", {"images_auto": False})                   # privacy switch on
        self.assertFalse(self.call("/message/1")[1]["images_allowed"])
        self.call("/images/allow", {"sender": "bob@shop.com"})           # ...except senders you allow
        self.assertTrue(self.call("/message/1")[1]["images_allowed"])


if __name__ == "__main__":
    unittest.main()
