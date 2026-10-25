"""Offline checks for section proposals, calendar events and the local API (model, Google faked).
Run: python -m unittest tests.test_day45
"""
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
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import actions, server
from core.accounts import calendar
from core.ai import events, llm as llm_mod, propose
from core.db.migrations import init_db


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "mail.db")
        with contextlib.redirect_stdout(io.StringIO()):
            self.db = init_db(self.db_path)
        self.db.execute("INSERT INTO accounts (provider, address, auth_ref_in_keychain) VALUES ('gmail', 'me@x.com', 'ref')")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add(self, msg_id, subject, section=None, days_ago=0, status="classified"):
        date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")
        sec = self.db.execute("SELECT id FROM sections WHERE name=?", (section,)).fetchone() if section else None
        self.db.execute("INSERT INTO messages (id, account_id, remote_id, sender, subject, body_text, date, section_id, status) "
                        "VALUES (?, 1, ?, 'x@y.com', ?, 'body', ?, ?, ?)",
                        (msg_id, f"r{msg_id}", subject, date, sec[0] if sec else None, status))
        self.db.commit()


class TestProposals(Base):
    def test_only_valid_proposals_are_stored_and_accept_creates_section(self):
        for i in range(1, 7):
            self.add(i, f"Rays game {i}")
        answer = {"proposals": [
            {"name": "sports", "description": "Teams and games", "email_ids": [1, 2, 3, 99]},  # 99 never shown
            {"name": "Ads", "description": "dup", "email_ids": [1, 2, 3]},                    # already a section
            {"name": "Tickets", "description": "too few", "email_ids": [4, 5]},               # under MIN_EMAILS
            {"name": "Delete all mail", "description": "3 words", "email_ids": [1, 2, 3]},    # max two words
            {"name": "Rm-rf", "description": "not letters", "email_ids": [1, 2, 3]},
        ]}
        with mock.patch.object(llm_mod.LocalLLM, "generate_json", return_value=answer):
            new = propose.propose_sections(self.db)
            self.assertEqual(len(new), 1)
            self.assertEqual(propose.propose_sections(self.db), [])  # Sports isn't proposed twice
        (pid, name, desc, reason), = propose.pending_proposals(self.db)
        self.assertEqual((name, desc), ("Sports", "Teams and games"))
        self.assertIn("Fits 3 unsorted emails", reason)

        self.assertEqual(propose.accept_proposal(self.db, pid), "Sports")
        self.assertTrue(self.db.execute("SELECT 1 FROM sections WHERE name='Sports' AND created_by='ai'").fetchone())
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages WHERE status='new'").fetchone()[0], 6)
        with self.assertRaises(ValueError):
            propose.reject_proposal(self.db, pid)  # no longer pending


class TestEvents(Base):
    NOW = datetime(2026, 10, 19, 12, 0)

    def ev(self, start, conf=0.95, has=True, title="Dentist"):
        return {"has_event": has, "title": title, "start": start, "end": "", "location": "", "confidence": conf}

    def test_decide_rules(self):
        cases = [  # (event, section, expected)
            (self.ev("", has=False), "Personal", "skip"),
            (self.ev("2026-10-18T09:00"), "Personal", "skip"),         # past
            (self.ev("2026-10-25"), "Personal", "ask"),               # no time
            (self.ev("2026-10-25T09:00", conf=0.6), "Personal", "skip"),  # too unsure to even ask
            (self.ev("2026-10-25T09:00", conf=0.8), "Personal", "ask"),
            (self.ev("next Friday"), "Personal", "ask"),               # unreadable
            (self.ev("2026-10-25T09:00"), "Personal", "create"),       # a note from someone you know
            (self.ev("2026-10-25T09:00", conf=1.0), "Work", "ask"),    # e.g. an advisor "meeting" promo
            (self.ev("2026-10-25T09:00", conf=1.0), "Unsorted", "ask"),
            (self.ev("2026-10-25"), "Unsorted", "skip"),               # date-only promo from a stranger
        ]
        for ev, section, want in cases:
            self.assertEqual(events.decide(ev, self.NOW, section)[0], want, (ev, section))

    def test_add_from_email(self):
        self.add(1, "Lunch", "Personal")
        with mock.patch.object(calendar, "create_event", return_value="evt9") as create:
            events.add_from_email(self.db, 1, "Lunch with Ann", "2026-10-25T12:30", "", "Cafe")
        body = create.call_args.args[2]
        self.assertEqual((body["summary"], body["location"]), ("Lunch with Ann", "Cafe"))
        for title, start in (("", "2026-10-25T12:30"), ("Lunch", "next week")):
            with self.assertRaises(ValueError):
                events.add_from_email(self.db, 1, title, start)

    def test_event_body(self):
        all_day = calendar.event_body("Exam", "2026-10-25", "", "", "src")
        self.assertEqual(all_day["start"], {"date": "2026-10-25"})
        self.assertEqual(all_day["end"], {"date": "2026-10-26"})
        timed = calendar.event_body("Call", "2026-10-25T09:00", "", "Zoom", "src")
        start = datetime.fromisoformat(timed["start"]["dateTime"])
        self.assertIsNotNone(start.tzinfo)  # local time zone attached
        self.assertEqual(datetime.fromisoformat(timed["end"]["dateTime"]) - start, timedelta(hours=1))

    def test_check_create_ask_and_undo(self):
        future = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%dT10:00")
        self.add(1, "Appointment confirmed", "Personal")
        self.add(2, "Maybe a meetup", "Social")
        self.add(3, "Sale ends Friday", "Ads")            # skipped section: never sent to the model
        self.add(4, "Old appointment", "Updates", days_ago=30)  # too old
        answers = {1: self.ev(future), 2: self.ev(future, conf=0.8, title="Meetup")}
        with mock.patch.object(events, "extract", side_effect=lambda db, i: answers[i]) as extract, \
             mock.patch.object(calendar, "create_event", return_value="evt1") as create, \
             mock.patch.object(calendar, "delete_event") as delete:
            dry = events.check_events(self.db, apply=False)
            self.assertEqual([r[2] for r in dry], ["create", "ask"])
            create.assert_not_called()
            self.assertEqual(self.db.execute("SELECT count(*) FROM pending_events").fetchone()[0], 0)

            events.check_events(self.db)
            self.assertEqual(sorted(c.args[1] for c in extract.call_args_list), [1, 1, 2, 2])
            create.assert_called_once()
            body = create.call_args.args[2]
            self.assertEqual(body["summary"], "Dentist")
            self.assertIn("mail.google.com/mail/u/0/#all/r1", body["description"])  # links the source email
            self.assertEqual(events.check_events(self.db), [])  # each email checked once

            (pid, title, *_), = events.pending_events(self.db)
            self.assertEqual(title, "Meetup")
            events.answer_pending(self.db, pid, accept=True)
            self.assertEqual(create.call_count, 2)

            print_ = actions.undo(self.db)  # newest action: the accepted Meetup event
            self.assertIn("Deleted calendar event 'Meetup'", print_)
            delete.assert_called_once_with(self.db, 1, "evt1")
            with self.assertRaises(actions.ActionError):  # one event per email
                actions.create_event(self.db, 1, body)


class TestServer(Base):
    def setUp(self):
        super().setUp()
        self.add(1, "Hello", "News")
        self.db.execute("INSERT INTO section_proposals (name, description, reason) VALUES ('Sports', 'd', 'r')")
        self.db.commit()
        server.Handler.token, server.Handler.db_path = "secret", self.db_path
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, path, body=None, token="secret"):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Token": token} if token else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_token_required(self):
        self.assertEqual(self.call("/status", token=None)[0], 403)
        self.assertEqual(self.call("/status", token="wrong")[0], 403)
        self.assertEqual(self.call("/refresh", {}, token=None)[0], 403)

    def test_status_settings_and_answers(self):
        code, st = self.call("/status")
        self.assertEqual(code, 200)
        self.assertEqual(st["sections"], [{"name": "News", "total": 1, "today": 1}])
        self.assertEqual(st["proposals"][0]["name"], "Sports")
        self.assertEqual(st["settings"]["summary_time"], "21:00")

        self.assertEqual(self.call("/settings", {"summary_time": "25:00"})[0], 400)
        self.assertEqual(self.call("/settings", {"summary_time": "07:30"})[0], 200)
        self.assertEqual(self.call("/status")[1]["settings"]["summary_time"], "07:30")

        self.assertEqual(self.call("/proposals/1/accept", {})[0], 200)
        self.assertEqual(self.call("/proposals/1/accept", {})[0], 400)  # already answered
        self.assertEqual(self.call("/events/99/reject", {})[0], 400)
        self.assertEqual(self.call("/nope", {})[0], 404)

    def test_refresh_runs_in_background_once_at_a_time(self):
        gate = threading.Event()
        with mock.patch.object(server.pipeline, "refresh", side_effect=lambda db: gate.wait(5) and []), \
             mock.patch.object(server.LocalLLM, "unload") as unload:
            self.assertEqual(self.call("/refresh", {})[0], 202)
            code, body = self.call("/summarize", {})
            self.assertEqual((code, body["busy"]), (409, "refresh"))
            gate.set()
            for _ in range(50):
                if not server.STATE["busy"]:
                    break
                time.sleep(0.05)
            unload.assert_called_once()  # model freed as soon as the job ends
        self.assertIsNone(self.call("/status")[1]["busy"])

    def test_jobs_get_plain_rows(self):
        # Regression: the server's connection broke the classifier ('<' between sqlite3.Row)
        from core.ai.classify import classify_all_new
        labels = os.path.join(self.tmp.name, "labels.csv")
        with open(labels, "w") as f:
            f.write("message_id,label\n2,News\n4,News\n")
        self.add(2, "Example A", "News")
        self.add(4, "Example B", "News")
        self.add(5, "Fresh mail", status="new")
        db = server.connect(self.db_path)
        try:
            with mock.patch.object(llm_mod.LocalLLM, "generate_json", return_value={"section": "News", "confidence": 0.9}):
                self.assertEqual(sum(classify_all_new(db, labels_path=labels).values()), 1)
        finally:
            db.close()

    def test_scheduler_daily_once_then_refresh(self):
        server.STATE["last_refresh"] = 0
        with mock.patch.object(server, "run_job") as run:
            server.scheduler_tick(self.db_path, now=datetime(2026, 10, 19, 20, 59))
            self.assertEqual(run.call_args.args[0], "refresh")  # before 21:00
            server.scheduler_tick(self.db_path, now=datetime(2026, 10, 19, 21, 0))
            self.assertEqual(run.call_args.args[0], "daily summary")
        self.db.execute("INSERT INTO settings (key, value) VALUES ('last_daily_summary', '2026-10-19')")
        self.db.commit()
        server.STATE["last_refresh"] = time.time()
        with mock.patch.object(server, "run_job") as run:
            server.scheduler_tick(self.db_path, now=datetime(2026, 10, 19, 22, 0))
            run.assert_not_called()  # already done today, refreshed recently


if __name__ == "__main__":
    unittest.main()
