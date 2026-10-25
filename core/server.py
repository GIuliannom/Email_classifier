"""Local-only HTTP API for the menu-bar app, plus the scheduler.
Run: python -m core.server   (the menu-bar app starts it for you)

Listens on 127.0.0.1 only. Every request must carry the X-Token header from data/api_token:
a custom header means a web page in your browser can't call this API (CORS preflight fails)."""
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import actions, pipeline
from core.accounts import connector
from core.sections import allowed as allowed_sections
from urllib.parse import parse_qs, quote, urlparse
from email.utils import parseaddr
from core import compose, mail
from core.ai import ask, events, propose
from core.ai.llm import LocalLLM
from core.db.migrations import init_db

PORT = 8765
DB_PATH = os.path.join("data", "mail.db")
TOKEN_PATH = os.path.join("data", "api_token")
REFRESH_MINUTES = 15

# ponytail: one global lock, so a scheduled run and a button press never sync at the same time
JOB_LOCK = threading.Lock()
STATE = {"busy": None, "last_refresh": 0.0, "errors": []}


def load_token(path: str = TOKEN_PATH) -> str:
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # readable only by you
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(16))
    with open(path) as f:
        return f.read().strip()


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    # Plain tuples: the jobs' code sorts and unpacks rows (status() asks for sqlite3.Row itself)
    return sqlite3.connect(path, timeout=30)  # waits instead of failing while a job is writing


def get_setting(db, key: str) -> str:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(db, key: str, value: str):
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))
    db.commit()


def run_job(name: str, fn, db_path: str = DB_PATH) -> bool:
    """Run fn(db) in the background unless another job is running. Returns False if busy."""
    if not JOB_LOCK.acquire(blocking=False):
        return False
    STATE["busy"] = name

    def work():
        db = connect(db_path)
        try:
            STATE["errors"] = fn(db) or []
        except Exception as e:
            STATE["errors"] = [f"{name}: {e}"]
        finally:
            db.close()
            LocalLLM().unload()  # don't wait 5 idle minutes holding the model and its prompt cache
            STATE["busy"] = None
            JOB_LOCK.release()
    threading.Thread(target=work, daemon=True).start()
    return True


def refresh_job(db):
    errors = pipeline.refresh(db)
    STATE["last_refresh"] = time.time()
    return errors


def daily_job(db):
    errors = pipeline.refresh(db) + pipeline.daily(db)
    STATE["last_refresh"] = time.time()
    set_setting(db, "last_daily_summary", datetime.now().strftime("%Y-%m-%d"))
    return errors


def scheduler_tick(db_path: str = DB_PATH, now: datetime = None):
    """Called every minute: daily summary once a day at the set time, otherwise refresh every 15 min."""
    now = now or datetime.now()
    db = connect(db_path)
    try:
        due_daily = (now.strftime("%H:%M") >= (get_setting(db, "summary_time") or "21:00")
                     and get_setting(db, "last_daily_summary") != now.strftime("%Y-%m-%d"))
    finally:
        db.close()
    if due_daily:
        run_job("daily summary", daily_job, db_path)
    elif time.time() - STATE["last_refresh"] >= REFRESH_MINUTES * 60:
        run_job("refresh", refresh_job, db_path)


def status(db) -> dict:
    db.row_factory = sqlite3.Row  # this request's own connection: dict(row) below
    # messages.date is naive UTC: local midnight converted to UTC
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    today_utc = today.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    sections = [dict(r) for r in db.execute("""
        SELECT coalesce(s.name, 'Unsorted') AS name, count(*) AS total, sum(m.date >= ?) AS today
        FROM messages m LEFT JOIN sections s ON s.id = m.section_id WHERE m.status IN ('new', 'classified')
        GROUP BY 1 ORDER BY total DESC""", (today_utc,))]
    summaries = [dict(r) for r in db.execute("""
        SELECT a.address AS account, s.text, s.range_start, s.kind FROM summaries s
        JOIN accounts a ON a.id = s.account_id
        WHERE s.id IN (SELECT max(id) FROM summaries GROUP BY account_id) ORDER BY a.address""")]
    return {
        "sections": sections,
        "summaries": summaries,
        "proposals": [dict(zip(("id", "name", "description", "reason"), r)) for r in propose.pending_proposals(db)],
        "events": [dict(zip(("id", "title", "start", "end", "location", "reason", "subject"), r))
                   for r in events.pending_events(db)],
        "settings": {"summary_time": get_setting(db, "summary_time"),
                     "images_auto": (get_setting(db, "images_auto") or "1") == "1"},
        "busy": STATE["busy"],
        "errors": STATE["errors"],
        "last_refresh": STATE["last_refresh"],
    }


VISIBLE = "m.status IN ('new', 'classified')"
FOLDERS = {  # smart folders in the mail window; search covers everything but Trash and Junk
    "inbox": VISIBLE, "unread": VISIBLE + " AND m.unread = 1", "starred": "m.starred = 1 AND m.status != 'trashed'",
    "archive": "m.status = 'archived'", "junk": "m.status = 'junk'", "trash": "m.status = 'trashed'",
    "search": "m.status NOT IN ('trashed', 'junk')",
}


def mailboxes(db) -> list:
    """Accounts with their sections, totals and unread counts, plus folder counts, for the sidebar."""
    out = []
    for account_id, address, provider in db.execute("SELECT id, address, provider FROM accounts ORDER BY id").fetchall():
        rows = db.execute(f"""
            SELECT coalesce(s.name, 'Unsorted'), count(*), sum(coalesce(m.unread, 0))
            FROM messages m LEFT JOIN sections s ON s.id = m.section_id
            WHERE m.account_id=? AND {VISIBLE} GROUP BY 1
            -- fixed order (as the sections were defined), Unsorted last: rows never swap as counts change
            ORDER BY coalesce(s.name, 'Unsorted') = 'Unsorted', min(coalesce(s.id, 0))""", (account_id,)).fetchall()
        counts = {name: db.execute(f"SELECT count(*) FROM messages m WHERE m.account_id=? AND {cond}",
                                   (account_id,)).fetchone()[0]
                  for name, cond in FOLDERS.items() if name not in ("inbox", "search")}
        out.append({"id": account_id, "address": address, "provider": provider,
                    "sections": [{"name": n, "total": t, "unread": u} for n, t, u in rows],
                    "folders": counts,
                    # every section the account may use, for "Move to" (even ones with no mail yet)
                    "available": [n for _, n, _ in allowed_sections(db, account_id) if n != "Unsorted"]})
    return out


def fts_query(text: str):
    """Words typed in the search box -> an FTS5 query: every word must match, as a prefix."""
    terms = re.findall(r"[\w@.+-]+", text or "")
    return " ".join('"' + t.replace('"', "") + '"*' for t in terms) or None


def message_list(db, q: dict) -> list:
    """Newest first. folder (inbox/unread/starred/archive/junk/trash), q (search), account,
    section (Unsorted = not sorted), limit, offset."""
    folder = "search" if q.get("q") else q.get("folder", "inbox")
    if folder not in FOLDERS:
        raise ValueError(f"Unknown folder {folder!r}")
    where, args = [FOLDERS[folder]], []
    if folder == "search":
        match = fts_query(q["q"])
        if not match:
            return []
        where.append("m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
        args.append(match)
    if q.get("account"):
        where.append("m.account_id = ?")
        args.append(int(q["account"]))
    if q.get("section"):
        where.append("lower(coalesce(s.name, 'Unsorted')) = lower(?)")
        args.append(q["section"])
    limit, offset = min(int(q.get("limit", 100)), 500), int(q.get("offset", 0))
    rows = db.execute(f"""
        SELECT m.id, m.account_id, m.sender, m.subject, m.date, substr(coalesce(m.body_text, ''), 1, 600),
               coalesce(m.unread, 0), coalesce(s.name, 'Unsorted'), coalesce(m.starred, 0), m.status,
               EXISTS(SELECT 1 FROM message_raw r WHERE r.message_id = m.id AND r.raw LIKE '%Content-Disposition: attachment%')
        FROM messages m LEFT JOIN sections s ON s.id = m.section_id
        WHERE {' AND '.join(where)} ORDER BY m.date DESC LIMIT ? OFFSET ?""", (*args, limit, offset)).fetchall()
    keys = ("id", "account", "sender", "subject", "date", "snippet", "unread", "section", "starred", "status",
            "attachments")
    return [dict(zip(keys, r)) | {"unread": bool(r[6]), "starred": bool(r[8]), "attachments": bool(r[10]),
                                  "snippet": snippet(r[5])} for r in rows]


def snippet(text: str) -> str:
    """The first real words of an email for the list: no links, no "this email came from outside"
    banners that some organisations (UCF) put on top of every message."""
    text = html.unescape(text or "")
    banner = re.match(r"\s*(CAUTION|EXTERNAL|\[EXTERNAL\])\b.{0,300}?(recogni[sz]e|is safe|trust)[^.!]*[.!]", text, re.I | re.S)
    if banner:  # e.g. "CAUTION: This email originated from outside of UCF. Only visit links ... you recognize."
        text = text[banner.end():]
    text = re.sub(r"<?https?://\S+>?|\[\s*\]|\(\s*\)", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:140]


def _images_allowed(db) -> set:
    return set(json.loads(get_setting(db, "images_allowed") or "[]"))


def message_content(db, msg_id: int) -> dict:
    row = db.execute("""SELECT m.sender, m.subject, m.date, m.body_text, m.account_id, coalesce(m.starred, 0),
                               m.status, coalesce(s.name, 'Unsorted') FROM messages m
                        LEFT JOIN sections s ON s.id = m.section_id WHERE m.id=?""", (msg_id,)).fetchone()
    if not row:
        raise ValueError(f"No message with id {msg_id}")
    sender, subject, date, body, account_id, starred, status, section = row
    try:
        c = mail.content(mail.raw_for(db, msg_id))
    except Exception:  # offline or the provider is unreachable: show what we stored
        c = {"From": sender, "Subject": subject, "Date": date, "To": "", "Cc": "", "html": None, "text": body,
             "attachments": []}
    if not c.get("html") and not c.get("text"):
        c["text"] = body or "(This email's text was pruned to save space; it's still in your mailbox.)"
    address = (parseaddr(sender or "")[1] or "").lower()
    # Remote images load unless you turned that off (then only for senders you allowed)
    auto = (get_setting(db, "images_auto") or "1") == "1"
    return {"id": msg_id, **c, "account": account_id, "starred": bool(starred), "status": status,
            "section": section, "sender_address": address, "images_allowed": auto or address in _images_allowed(db)}


class Handler(BaseHTTPRequestHandler):
    token = None
    db_path = DB_PATH

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self._send_bytes(code, body, "application/json")

    def _send_bytes(self, code: int, body: bytes, content_type: str, filename: str = None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("X-Filename", quote(filename))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if secrets.compare_digest(self.headers.get("X-Token", ""), self.token):
            return True
        self._send(403, {"error": "missing or wrong X-Token"})
        return False

    def do_GET(self):
        if not self._authorized():
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        db = connect(self.db_path)
        try:
            if url.path == "/status":
                return self._send(200, status(db))
            if url.path == "/mailboxes":
                return self._send(200, {"accounts": mailboxes(db)})
            if url.path == "/messages":
                return self._send(200, {"messages": message_list(db, q)})
            m = re.fullmatch(r"/message/(\d+)", url.path)
            if m:
                return self._send(200, message_content(db, int(m.group(1))))
            m = re.fullmatch(r"/message/(\d+)/compose", url.path)
            if m:
                return self._send(200, compose.prefill(db, int(m.group(1)), q.get("mode", "reply")))
            m = re.fullmatch(r"/message/(\d+)/event", url.path)
            if m:  # Add to Calendar: what the model reads from the email, for you to check and edit
                ev = events.extract(db, int(m.group(1)))
                return self._send(200, {k: ev[k] for k in ("title", "start", "end", "location", "has_event")})
            m = re.fullmatch(r"/message/(\d+)/attachment/(\d+)", url.path)
            if m:
                name, ctype, data = mail.attachment(mail.raw_for(db, int(m.group(1))), int(m.group(2)))
                return self._send_bytes(200, data, ctype, name)
            return self._send(404, {"error": "not found"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})
        finally:
            db.close()

    def do_POST(self):
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "body must be JSON"})

        if self.path in ("/refresh", "/summarize"):
            name, fn = ("refresh", refresh_job) if self.path == "/refresh" else ("daily summary", daily_job)
            started = run_job(name, fn, self.db_path)
            return self._send(202 if started else 409, {"started": started, "busy": STATE["busy"]})

        db = connect(self.db_path)
        try:
            m = re.fullmatch(r"/(proposals|events)/(\d+)/(accept|reject)", self.path)
            if m:
                kind, item_id, verb = m.group(1), int(m.group(2)), m.group(3)
                if kind == "proposals":
                    fn = propose.accept_proposal if verb == "accept" else propose.reject_proposal
                    return self._send(200, {"message": fn(db, item_id)})
                return self._send(200, {"message": events.answer_pending(db, item_id, verb == "accept")})
            m = re.fullmatch(r"/message/(\d+)/(read|unread|move|trash|archive|junk|restore|star|unstar)", self.path)
            if m:
                msg_id, verb = int(m.group(1)), m.group(2)
                action_id = None
                if verb == "move":
                    action_id = actions.move_message(db, msg_id, str(data.get("section", "")), "user_move")
                elif verb in ("read", "unread"):
                    row = db.execute("SELECT account_id, remote_id FROM messages WHERE id=?", (msg_id,)).fetchone()
                    if not row:
                        raise ValueError(f"No message with id {msg_id}")
                    connector(db, row[0]).mark_read(row[1], read=(verb == "read"))
                elif verb in ("star", "unstar"):
                    action_id = actions.set_star(db, msg_id, verb == "star")
                else:
                    # A click in the window is your explicit confirmation; it's undoable (Trash, not delete)
                    action_id = actions.change_state(db, msg_id, verb, confirmed=True)
                return self._send(200, {"ok": True, "action_id": action_id})
            m = re.fullmatch(r"/message/(\d+)/event", self.path)
            if m:  # pressing Add in the form is your confirmation
                action_id = events.add_from_email(db, int(m.group(1)), str(data.get("title", "")), str(data.get("start", "")),
                                                  str(data.get("end", "")), str(data.get("location", "")))
                return self._send(200, {"action_id": action_id})
            if self.path == "/send":
                account = int(data.get("account") or 0)
                if not db.execute("SELECT 1 FROM accounts WHERE id=?", (account,)).fetchone():
                    raise ValueError("Choose an account to send from")
                fields = {k: data.get(k) for k in ("mode", "ref", "to", "cc", "subject", "body")}
                fields["mode"] = fields["mode"] or "new"
                # Pressing Send in the compose window is your confirmation
                return self._send(200, {"action_id": actions.send_email(db, account, fields, confirmed=True)})
            if self.path == "/draft":
                return self._send(200, {"text": compose.draft_reply(db, int(data["message_id"]),
                                                                     str(data.get("instructions", "")))})
            if self.path == "/ask":
                return self._send(200, ask.ask(db, str(data.get("question", ""))))
            if self.path == "/undo":
                return self._send(200, {"message": actions.undo(db, data.get("action_id"))})
            if self.path == "/images/allow":
                allowed = _images_allowed(db) | {str(data.get("sender", "")).lower()}
                set_setting(db, "images_allowed", json.dumps(sorted(a for a in allowed if "@" in a)))
                return self._send(200, {"ok": True})
            if self.path == "/settings":
                if "images_auto" in data:
                    set_setting(db, "images_auto", "1" if data["images_auto"] else "0")
                if "summary_time" in data:
                    t = str(data["summary_time"])
                    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                        return self._send(400, {"error": "summary_time must be HH:MM (24h)"})
                    set_setting(db, "summary_time", t)
                return self._send(200, {"settings": {"summary_time": get_setting(db, "summary_time"),
                                                     "images_auto": (get_setting(db, "images_auto") or "1") == "1"}})
            return self._send(404, {"error": "not found"})
        except ValueError as e:  # unknown id, bad recipient, nothing to undo, ...
            return self._send(400, {"error": str(e)})
        except Exception as e:  # e.g. Gmail or Mail unreachable: answer instead of dropping the connection
            return self._send(500, {"error": str(e)})
        finally:
            db.close()

    def log_message(self, *args):
        pass  # keep the terminal quiet; errors show up in /status


def main():
    init_db(DB_PATH).close()  # apply migrations once at start
    Handler.token = load_token()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    def scheduler():
        while True:
            try:
                scheduler_tick()
            except Exception as e:
                STATE["errors"] = [f"scheduler: {e}"]
            time.sleep(60)
    threading.Thread(target=scheduler, daemon=True).start()

    if os.environ.get("EMAIL_AI_EXIT_WITH_PARENT"):  # set by the menu-bar app that started us
        parent = os.getppid()

        def exit_with_parent():
            while os.getppid() == parent:  # macOS re-parents orphans, so a change means the app is gone
                time.sleep(30)
            os._exit(0)
        threading.Thread(target=exit_with_parent, daemon=True).start()
    print(f"Email AI engine on http://127.0.0.1:{PORT} (Ctrl-C to stop)")
    server.serve_forever()


if __name__ == "__main__":
    main()
