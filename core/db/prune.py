"""Keep the database from growing without limit: past PRUNE_AT, drop the stored copy and body text
of the oldest emails until the file is under PRUNE_TO. Sender, subject, date and section stay, so
sorting, undo and history still work; the emails themselves are still in your mailbox."""
import os
import sqlite3

GB = 1024 ** 3
PRUNE_AT = 5 * GB   # check daily; prune once the file passes this
PRUNE_TO = 4 * GB   # ...down to this, so it doesn't prune again the next day


def prune_if_large(db: sqlite3.Connection, prune_at: int = PRUNE_AT, prune_to: int = PRUNE_TO) -> int:
    """Returns how many emails had their body removed (0 when the file is small enough)."""
    path = db.execute("PRAGMA database_list").fetchone()[2]
    size = os.path.getsize(path)
    if size <= prune_at:
        return 0
    to_free, freed, ids = size - prune_to, 0, []
    # The stored copy of each email (with attachments) is most of the size: it goes with the text
    for msg_id, nbytes in db.execute("""
            SELECT m.id, coalesce(length(CAST(m.body_text AS BLOB)), 0) + coalesce(length(r.raw), 0)
            FROM messages m LEFT JOIN message_raw r ON r.message_id = m.id
            WHERE m.body_text IS NOT NULL OR r.raw IS NOT NULL ORDER BY m.date"""):
        ids.append(msg_id)
        freed += nbytes or 0
        if freed >= to_free:
            break
    db.executemany("UPDATE messages SET body_text=NULL WHERE id=?", [(i,) for i in ids])
    db.executemany("DELETE FROM message_raw WHERE message_id=?", [(i,) for i in ids])
    db.commit()
    db.execute("VACUUM")  # actually shrinks the file; SQLite otherwise keeps the freed pages
    return len(ids)
