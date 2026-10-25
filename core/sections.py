"""Which sections each account uses. All sections apply to every account unless hidden for it
(e.g. Banks hidden for UCF): the classifier never picks a hidden one and the window doesn't offer it."""
import sqlite3

from core import actions


def allowed(db: sqlite3.Connection, account_id: int) -> list:
    """[(id, name, description)] the account may use, Unsorted last."""
    return db.execute("""
        SELECT id, name, description FROM sections
        WHERE approved=1 AND id NOT IN (SELECT section_id FROM hidden_sections WHERE account_id=?)
        ORDER BY name='Unsorted', id""", (account_id,)).fetchall()


def hidden(db: sqlite3.Connection, account_id: int) -> list:
    return [n for (n,) in db.execute("""SELECT s.name FROM hidden_sections h JOIN sections s ON s.id = h.section_id
                                        WHERE h.account_id=? ORDER BY s.name""", (account_id,))]


def _section_id(db, name: str) -> int:
    row = db.execute("SELECT id FROM sections WHERE lower(name)=lower(?) AND approved=1", (name,)).fetchone()
    if not row:
        raise ValueError(f"No section named {name!r}")
    if name.lower() == "unsorted":
        raise ValueError("Unsorted can't be hidden: it's where uncertain mail waits")
    return row[0]


def hide(db: sqlite3.Connection, account_id: int, name: str) -> int:
    """Stop using a section for one account. Its mail there goes back to be sorted again
    (through undo, so the action log stays consistent). Returns how many emails need re-sorting."""
    sec_id = _section_id(db, name)
    db.execute("INSERT OR IGNORE INTO hidden_sections (account_id, section_id) VALUES (?, ?)", (account_id, sec_id))
    ids = [i for (i,) in db.execute("SELECT id FROM messages WHERE account_id=? AND section_id=?", (account_id, sec_id))]
    for msg_id in ids:
        last = db.execute("SELECT id FROM actions_log WHERE message_id=? AND undone_at IS NULL "
                          "AND action IN ('move_section', 'user_move') ORDER BY id DESC LIMIT 1", (msg_id,)).fetchone()
        if last:
            actions.undo(db, last[0])
        # If what's left is still the hidden section (or nothing), it gets sorted again
        db.execute("UPDATE messages SET section_id=NULL, status='new' WHERE id=? AND "
                   "(section_id IS NULL OR section_id=?)", (msg_id, sec_id))
    db.commit()
    return len(ids)


def show(db: sqlite3.Connection, account_id: int, name: str):
    db.execute("DELETE FROM hidden_sections WHERE account_id=? AND section_id=?", (account_id, _section_id(db, name)))
    db.commit()
