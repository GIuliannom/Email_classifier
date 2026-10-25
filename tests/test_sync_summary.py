"""Offline check that sync, summary, classify and the LLM wrapper work end to end.
Gmail and Ollama are faked, so this needs no network, token or model.
Run: python -m unittest tests.test_sync_summary
"""
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from googleapiclient.errors import HttpError

from core import cli
from core.accounts.gmail import GmailConnector
from core.ai import llm as llm_mod
from core.ai.classify import classify_message
from core.ai.summarize import generate_summary
from core.db.migrations import init_db


def raw_msg(mid, subject, ts, body="hello", ctype="text/plain"):
    eml = f"From: Alice <a@x.com>\r\nSubject: {subject}\r\nContent-Type: {ctype}; charset=utf-8\r\n\r\n{body}\r\n".encode()
    return {"id": mid, "threadId": "t" + mid, "internalDate": str(int(ts * 1000)),
            "raw": base64.urlsafe_b64encode(eml).decode()}


def fake_gmail(store, list_ids, history=None, history_error=None):
    svc = mock.MagicMock()
    users = svc.users.return_value
    users.getProfile.return_value.execute.return_value = {"historyId": "100"}
    users.messages.return_value.list.return_value.execute.return_value = {"messages": [{"id": i} for i in list_ids]}
    users.messages.return_value.get.side_effect = lambda userId, id, format: mock.Mock(execute=lambda **_: store[id])
    hist = users.history.return_value.list.return_value.execute
    if history_error:
        hist.side_effect = history_error
    else:
        hist.return_value = history or {}
    return svc


def echo_llm(self, prompt, schema, max_retries=1):
    return {"summary": prompt}


class TestSyncSummary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = init_db(os.path.join(self.tmp.name, "mail.db"))
        for addr in ("me@gmail.com", "other@gmail.com"):
            self.db.execute("INSERT INTO accounts (provider, address, auth_ref_in_keychain) VALUES ('gmail', ?, 'ref')", (addr,))
        self.db.commit()
        now = time.time()
        self.store = {
            "m1": raw_msg("m1", "Today plain", now, "plain\u200c\u200c body"),  # zero-width padding gets stripped
            "m2": raw_msg("m2", "Old html", now - 3 * 86400, "<style>p{color:red}</style><p>html body</p>", "text/html"),
            "m3": raw_msg("m3", "Arrived later", now, "new body"),
        }

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def sync(self, svc):
        with mock.patch.object(GmailConnector, "_service", return_value=svc), contextlib.redirect_stdout(io.StringIO()):
            return GmailConnector(1, self.db).sync()

    def test_first_then_incremental_sync(self):
        svc = fake_gmail(self.store, ["m1", "m2"],
                         history={"historyId": "105", "history": [{"messagesAdded": [{"message": {"id": "m3"}}, {"message": {"id": "m1"}}]}]})
        self.assertEqual(self.sync(svc), 2)
        sender, subject, date, body = self.db.execute(
            "SELECT sender, subject, date, body_text FROM messages WHERE remote_id='m1'").fetchone()
        self.assertEqual((sender, subject, body), ("Alice <a@x.com>", "Today plain", "plain body"))
        self.assertRegex(date, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        html = self.db.execute("SELECT body_text FROM messages WHERE remote_id='m2'").fetchone()[0]
        self.assertEqual(html, "html body")
        self.assertEqual(self.db.execute("SELECT last_sync FROM accounts WHERE id=1").fetchone()[0], "100")
        self.assertIn("newer_than:30d", str(svc.users.return_value.messages.return_value.list.call_args))

        # second run uses history: m3 is new, m1 is already stored
        self.assertEqual(self.sync(svc), 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 3)
        self.assertEqual(self.db.execute("SELECT last_sync FROM accounts WHERE id=1").fetchone()[0], "105")

    def test_expired_history_falls_back_to_full_sync(self):
        self.db.execute("UPDATE accounts SET last_sync='1' WHERE id=1")
        svc = fake_gmail(self.store, ["m1"], history_error=HttpError(mock.Mock(status=404), b"gone"))
        self.assertEqual(self.sync(svc), 1)

    def test_daily_summary_is_local_today_and_per_account(self):
        self.sync(fake_gmail(self.store, ["m1", "m2"]))
        self.db.execute("INSERT INTO messages (account_id, remote_id, subject, body_text, date) "
                        "SELECT 2, 'x', 'Other account', 'secret', date FROM messages WHERE remote_id='m1'")
        with mock.patch.object(llm_mod.LocalLLM, "generate_json", echo_llm):
            text = generate_summary(self.db, 1, "daily")
            ondemand = generate_summary(self.db, 1, "ondemand")
        self.assertIn("Today plain", text)
        self.assertNotIn("Old html", text)
        self.assertNotIn("Other account", text)
        self.assertIn("Today plain", ondemand)

    def busy_day(self, combine_result):
        """12 long emails -> several chunks; partials are 'partN', the merge returns combine_result."""
        for i in range(12):
            self.db.execute("INSERT INTO messages (account_id, remote_id, subject, body_text, date) "
                            "VALUES (1, ?, ?, ?, '2026-10-19T12:00:00')", (f"b{i}", f"Mail {i}", "x" * 900))
        prompts = []

        def fake(_self, prompt, schema, max_retries=1):
            prompts.append(prompt)
            if "partial summaries" in prompt:
                return combine_result()
            return {"summary": f"part{len(prompts)}"}

        with mock.patch.object(llm_mod.LocalLLM, "generate_json", fake):
            text = generate_summary(self.db, 1, "daily", ("2026-10-19", "2026-10-20"))
        return text, prompts

    def test_busy_day_partials_are_combined_into_one(self):
        text, prompts = self.busy_day(lambda: {"summary": "FINAL"})
        merges = [p for p in prompts if "partial summaries" in p]
        self.assertGreater(len(prompts) - len(merges), 1)  # several chunks
        self.assertEqual(len(merges), 1)
        self.assertIn("part1", merges[0])
        self.assertEqual(text, "FINAL")

    def test_failed_combine_keeps_partials(self):
        def boom():
            raise RuntimeError("model down")
        text, _ = self.busy_day(boom)
        self.assertIn("part1", text)
        self.assertIn("part2", text)

    def test_cli_summary_commands(self):
        self.sync(fake_gmail(self.store, ["m1"]))
        with mock.patch.object(cli, "init_db", return_value=self.db), \
             mock.patch.object(llm_mod.LocalLLM, "generate_json", echo_llm):
            for argv in (["summary", "--today"], ["summary"], ["summary", "--account", "1", "--today"]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    cli.main(argv)
                self.assertIn("Today plain", out.getvalue(), argv)

    def test_classifier_validates_section_and_logs_move(self):
        self.sync(fake_gmail(self.store, ["m1"]))
        with mock.patch.object(llm_mod.LocalLLM, "generate_json", return_value={"section": "Shopping", "confidence": 0.95}):
            self.assertEqual(classify_message(self.db, 1)["section"], "Unsorted")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM actions_log").fetchone()[0], 0)

        with mock.patch.object(llm_mod.LocalLLM, "generate_json", return_value={"section": "news", "confidence": 0.9}):
            self.assertEqual(classify_message(self.db, 1)["section"], "News")
        action, undo = self.db.execute("SELECT action, reversible_info FROM actions_log").fetchone()
        self.assertEqual(action, "move_section")
        self.assertEqual(json.loads(undo), {"section_id": None, "status": "classified"})  # first call marked it processed

    def test_rate_limit_is_retried(self):
        # Real googleapiclient service over a canned HTTP sequence: the message fetch
        # hits Gmail's per-minute quota once, then succeeds after backoff.
        from googleapiclient.discovery import build
        from googleapiclient.http import HttpMockSequence
        rate_limited = json.dumps({"error": {"code": 403, "errors": [{"reason": "rateLimitExceeded"}]}})
        svc = build("gmail", "v1", http=HttpMockSequence([
            ({"status": "200"}, json.dumps({"historyId": "100"})),
            ({"status": "200"}, json.dumps({"messages": [{"id": "m1"}]})),
            ({"status": "403"}, rate_limited),
            ({"status": "200"}, json.dumps(self.store["m1"])),
        ]))
        with mock.patch("googleapiclient.http.time.sleep") as sleep:
            self.assertEqual(self.sync(svc), 1)
        self.assertEqual(sleep.call_count, 1)

    def test_llm_reloads_every_n_requests(self):
        # Ollama's prompt cache grows per request; the wrapper empties it by reloading every N
        llm_mod._requests_since_load = 0
        ok = mock.Mock()
        ok.json.return_value = {"response": '{"summary": "s"}'}
        with mock.patch.object(llm_mod.requests, "post", return_value=ok) as post:
            for _ in range(llm_mod.RELOAD_EVERY + 1):
                llm_mod.LocalLLM().generate_json("p", {"summary": "string"})
        unloads = [i for i, c in enumerate(post.call_args_list) if c.kwargs["json"].get("keep_alive") == 0]
        self.assertEqual(unloads, [llm_mod.RELOAD_EVERY])  # exactly one, right before request N+1

    def test_llm_retries_wrong_type(self):
        llm_mod._requests_since_load = 0  # no reload call mixed into the count below
        bad, good = mock.Mock(), mock.Mock()
        bad.json.return_value = {"response": '{"section": 5, "confidence": 0.9}'}
        good.json.return_value = {"response": '{"section": "News", "confidence": 0.9}'}
        with mock.patch.object(llm_mod.requests, "post", side_effect=[bad, good]) as post:
            out = llm_mod.LocalLLM().generate_json("p", {"section": "string|null", "confidence": "float"})
        self.assertEqual(out["section"], "News")
        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
