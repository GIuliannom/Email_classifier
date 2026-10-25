import sqlite3


def connector(db: sqlite3.Connection, account_id: int):
    """The right mail connector for an account: Gmail directly, or an account read through Apple Mail."""
    row = db.execute("SELECT provider FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise ValueError(f"No account with id {account_id}")
    if row[0] == "gmail":
        from core.accounts.gmail import GmailConnector
        return GmailConnector(account_id, db)
    if row[0] == "applemail":
        from core.accounts.applemail import AppleMailConnector
        return AppleMailConnector(account_id, db)
    raise ValueError(f"Provider {row[0]!r} isn't supported yet")
