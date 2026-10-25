"""Every change the app makes to a message goes through here: validated against a fixed
allow-list, logged with what is needed to reverse it, and undoable."""
import json
import sqlite3

# The only actions that may run. LLM output never names an action; code picks one from here.
ALLOWED_ACTIONS = {
    "move_section",  # the classifier moved it (confident enough)
    "user_move",     # you moved it; also becomes a same-sender example for the classifier
    "create_event",  # a Google Calendar event made from the email (undo deletes that event)
    "trash",         # you confirmed: the provider's Trash, never permanent delete (undo restores it)
    "archive",       # you clicked Archive: out of the inbox, kept in the account's archive
    "junk",          # you clicked Junk: Gmail Spam / the account's Junk folder
    "restore",       # you moved it back to the inbox from Trash, Archive or Junk
    "star", "unstar",  # Gmail star / Mail flag
    "send",          # you confirmed: a new email, reply or forward (can't be undone)
}

# Where each hidden state lives at the provider: (connector method to go there, method to come back)
HIDDEN = {"trashed": ("trash", "untrash"), "archived": ("archive", "unarchive"), "junk": ("mark_junk", "not_junk")}
STATE_OF = {"trash": "trashed", "archive": "archived", "junk": "junk"}
LABEL = {"trashed": "Trash", "archived": "Archive", "junk": "Junk", "new": "Inbox", "classified": "Inbox"}


class ActionError(ValueError):
    pass


def move_message(db: sqlite3.Connection, msg_id: int, section: str, action: str,
                 confidence: float = None) -> int:
    """Move a message to a section by name. Returns the actions_log id."""
    if action not in ALLOWED_ACTIONS:
        raise ActionError(f"Action '{action}' is not allowed")
    row = db.execute("SELECT id, name FROM sections WHERE lower(name)=lower(?) AND approved=1", (section,)).fetchone()
    if not row:
        raise ActionError(f"No approved section named '{section}'")
    sec_id, sec_name = row
    msg = db.execute("SELECT section_id, status, account_id FROM messages WHERE id=?", (msg_id,)).fetchone()
    if not msg:
        raise ActionError(f"No message with id {msg_id}")
    if db.execute("SELECT 1 FROM hidden_sections WHERE account_id=? AND section_id=?", (msg[2], sec_id)).fetchone():
        raise ActionError(f"{sec_name} is hidden for this account (python -m core.cli sections --show)")

    db.execute("UPDATE messages SET section_id=?, status='classified' WHERE id=?", (sec_id, msg_id))
    cur = db.execute(
        "INSERT INTO actions_log (message_id, action, params, confidence, reversible_info) VALUES (?, ?, ?, ?, ?)",
        (msg_id, action, json.dumps({"section_id": sec_id, "section": sec_name}), confidence,
         json.dumps({"section_id": msg[0], "status": msg[1]}))
    )
    db.commit()
    return cur.lastrowid


def create_event(db: sqlite3.Connection, msg_id: int, body: dict, confidence: float = None) -> int:
    """Add a calendar event made from an email. Returns the actions_log id."""
    from core.accounts import calendar  # here, so moving mail never needs Google libraries loaded
    if db.execute("SELECT 1 FROM events_created WHERE message_id=?", (msg_id,)).fetchone():
        raise ActionError(f"Message {msg_id} already has a calendar event")
    account_id = db.execute("SELECT account_id FROM messages WHERE id=?", (msg_id,)).fetchone()[0]
    event_id = calendar.create_event(db, account_id, body)
    db.execute("INSERT INTO events_created (message_id, calendar_event_id, confidence) VALUES (?, ?, ?)",
               (msg_id, event_id, confidence))
    cur = db.execute(
        "INSERT INTO actions_log (message_id, action, params, confidence, reversible_info) VALUES (?, ?, ?, ?, ?)",
        (msg_id, "create_event", json.dumps({"section": body.get("summary", ""), "event": body}), confidence,
         json.dumps({"calendar_event_id": event_id, "account_id": account_id})))
    db.commit()
    return cur.lastrowid


def _require_confirmation(confirmed: bool, what: str):
    # Only the CLI sets this, after a person typed "y" at a terminal. Nothing automatic does.
    if confirmed is not True:
        raise ActionError(f"{what} needs your explicit confirmation")


def _message(db, msg_id):
    row = db.execute("SELECT account_id, remote_id, status, section_id, starred FROM messages WHERE id=?",
                     (msg_id,)).fetchone()
    if not row:
        raise ActionError(f"No message with id {msg_id}")
    return row


def _set_status(db, msg_id: int, target: str):
    """Make the provider and the database agree on a new status: back to the inbox first if the
    email is in Trash/Archive/Junk, then to the target place if that's not the inbox."""
    from core.accounts import connector
    account_id, remote_id, status, _, _ = _message(db, msg_id)
    c = connector(db, account_id)
    if status in HIDDEN:
        getattr(c, HIDDEN[status][1])(remote_id)
    if target in HIDDEN:
        getattr(c, HIDDEN[target][0])(remote_id)
    db.execute("UPDATE messages SET status=? WHERE id=?", (target, msg_id))


def change_state(db: sqlite3.Connection, msg_id: int, op: str, confirmed: bool = False) -> int:
    """trash / archive / junk / restore one email. Logged and undoable. Returns the action id."""
    if op not in ("trash", "archive", "junk", "restore"):
        raise ActionError(f"Action '{op}' is not allowed")
    if op == "trash":
        _require_confirmation(confirmed, "Trashing")
    _, _, status, section_id, _ = _message(db, msg_id)
    if op == "restore":
        if status not in HIDDEN:
            raise ActionError(f"Message {msg_id} is already in the inbox")
        target = "classified" if section_id else "new"  # never-sorted mail gets sorted
    else:
        target = STATE_OF[op]
        if status == target:
            raise ActionError(f"Message {msg_id} is already in {LABEL[target]}")
    _set_status(db, msg_id, target)
    cur = db.execute("INSERT INTO actions_log (message_id, action, params, reversible_info) VALUES (?, ?, ?, ?)",
                     (msg_id, op, json.dumps({"section": LABEL[target]}), json.dumps({"status": status})))
    db.commit()
    return cur.lastrowid


def trash_message(db: sqlite3.Connection, msg_id: int, confirmed: bool = False) -> int:
    """Move a message to the provider's Trash (recoverable; undo restores it)."""
    return change_state(db, msg_id, "trash", confirmed)


def set_star(db: sqlite3.Connection, msg_id: int, on: bool) -> int:
    from core.accounts import connector
    account_id, remote_id, _, _, starred = _message(db, msg_id)
    connector(db, account_id).set_star(remote_id, on)
    db.execute("UPDATE messages SET starred=? WHERE id=?", (int(on), msg_id))
    cur = db.execute("INSERT INTO actions_log (message_id, action, params, reversible_info) VALUES (?, ?, ?, ?)",
                     (msg_id, "star" if on else "unstar", json.dumps({"section": "Starred" if on else "Unstarred"}),
                      json.dumps({"starred": starred or 0})))
    db.commit()
    return cur.lastrowid


def send_email(db: sqlite3.Connection, account_id: int, fields: dict, confirmed: bool = False) -> int:
    """Send a new email, reply or forward (fields: mode, ref, to, cc, subject, body). It can't be undone."""
    from core.accounts import connector
    _require_confirmation(confirmed, "Sending")
    sent_id = connector(db, account_id).send_fields(fields)
    cur = db.execute("INSERT INTO actions_log (message_id, action, params) VALUES (?, 'send', ?)",
                     (fields.get("ref"), json.dumps({"to": fields.get("to"), "subject": fields.get("subject"),
                                                     "mode": fields.get("mode", "new"),
                                                     "section": f"to {fields.get('to')}", "sent_id": sent_id})))
    db.commit()
    return cur.lastrowid


def undo(db: sqlite3.Connection, action_id: int = None) -> str:
    """Reverse one action (default: the most recent one not undone yet)."""
    if action_id is None:
        row = db.execute("SELECT id FROM actions_log WHERE undone_at IS NULL AND action != 'send' "
                         "ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            raise ActionError("Nothing to undo")
        action_id = row[0]
    row = db.execute("SELECT message_id, action, params, reversible_info, undone_at FROM actions_log WHERE id=?",
                     (action_id,)).fetchone()
    if not row:
        raise ActionError(f"No action with id {action_id}")
    msg_id, action, params, reversible, undone_at = row
    if undone_at:
        raise ActionError(f"Action {action_id} was already undone")
    if action == "send":
        raise ActionError("A sent email can't be unsent")
    # Only the newest live action on a message can be undone, or an older undo would
    # overwrite a later move with stale state
    # (moves/states, calendar events and stars don't affect each other: each kind is ordered on its own)
    newer = db.execute("SELECT id FROM actions_log WHERE message_id=? AND id>? AND undone_at IS NULL "
                       "AND action != 'send' AND " + KIND_SQL + " = ?", (msg_id, action_id, _kind(action))).fetchone()
    if newer:
        raise ActionError(f"Undo action {newer[0]} first: it changed message {msg_id} after this one")

    before = json.loads(reversible)
    if action == "create_event":
        from core.accounts import calendar
        calendar.delete_event(db, before["account_id"], before["calendar_event_id"])
        db.execute("DELETE FROM events_created WHERE message_id=?", (msg_id,))
        db.execute("UPDATE actions_log SET undone_at=CURRENT_TIMESTAMP WHERE id=?", (action_id,))
        db.commit()
        return f"Deleted calendar event '{json.loads(params)['section']}' made from message {msg_id}"
    if action in ("trash", "archive", "junk", "restore"):
        _set_status(db, msg_id, before["status"])
        db.execute("UPDATE actions_log SET undone_at=CURRENT_TIMESTAMP WHERE id=?", (action_id,))
        db.commit()
        return f"Moved message {msg_id} back to {LABEL[before['status']]}"
    if action in ("star", "unstar"):
        from core.accounts import connector
        account_id, remote_id, _, _, _ = _message(db, msg_id)
        connector(db, account_id).set_star(remote_id, bool(before["starred"]))
        db.execute("UPDATE messages SET starred=? WHERE id=?", (before["starred"], msg_id))
        db.execute("UPDATE actions_log SET undone_at=CURRENT_TIMESTAMP WHERE id=?", (action_id,))
        db.commit()
        return f"Undid {action} of message {msg_id}"
    db.execute("UPDATE messages SET section_id=?, status=? WHERE id=?", (before["section_id"], before["status"], msg_id))
    db.execute("UPDATE actions_log SET undone_at=CURRENT_TIMESTAMP WHERE id=?", (action_id,))
    db.commit()
    return f"Undid {action} of message {msg_id} to {json.loads(params)['section']}"


KIND_SQL = "CASE WHEN action='create_event' THEN 1 WHEN action IN ('star', 'unstar') THEN 2 ELSE 0 END"


def _kind(action: str) -> int:
    return 1 if action == "create_event" else 2 if action in ("star", "unstar") else 0


def history(db: sqlite3.Connection, n: int = 20) -> list:
    return db.execute("""
        SELECT a.id, a.action, json_extract(a.params, '$.section'), a.confidence, a.undone_at IS NOT NULL,
               m.id, m.subject
        FROM actions_log a LEFT JOIN messages m ON m.id = a.message_id
        ORDER BY a.id DESC LIMIT ?
    """, (n,)).fetchall()
