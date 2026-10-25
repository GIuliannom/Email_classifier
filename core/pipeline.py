"""The routines the scheduler (and the menu-bar buttons) run. Each step is independent:
one failing (e.g. Ollama not running) doesn't stop the others."""
import sqlite3

from core.accounts import connector
from core.ai import events, propose
from core.ai.classify import classify_all_new
from core.ai.summarize import generate_summary
from core.db.prune import prune_if_large


def _step(errors: list, name: str, fn):
    try:
        return fn()
    except Exception as e:
        errors.append(f"{name}: {e}")


def refresh(db: sqlite3.Connection) -> list:
    """Sync every account, sort new mail, mirror labels to Gmail, look for events. Returns errors."""
    errors = []
    accounts = [a for (a,) in db.execute("SELECT id FROM accounts")]
    for a in accounts:
        _step(errors, f"sync {a}", lambda: connector(db, a).sync())
    _step(errors, "classify", lambda: classify_all_new(db))
    for a in accounts:
        _step(errors, f"labels {a}", lambda: connector(db, a).push_labels())
    _step(errors, "events", lambda: events.check_events(db))
    return errors


def daily(db: sqlite3.Connection) -> list:
    """End-of-day summary per account, then new-section proposals for what stayed unsorted."""
    errors = []
    for (a,) in db.execute("SELECT id FROM accounts").fetchall():
        _step(errors, f"summary {a}", lambda: generate_summary(db, a, "daily"))
    _step(errors, "proposals", lambda: propose.propose_sections(db))
    _step(errors, "prune", lambda: prune_if_large(db))  # oldest bodies go once the file passes 5 GB
    return errors
