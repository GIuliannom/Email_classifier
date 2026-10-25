"""Apple Mail bridge: for accounts whose organisation blocks our own login (UCF needs admin
approval), read them through the Mail app, which is already signed in.

Talks to Mail with AppleScript (Mail's official scripting interface). Mail does the work with its
own login, so read/unread, flags, trash, archive, junk and sending reach the server. Sections stay
local (Mail can't set Exchange categories).
The first run asks once: "EmailAI wants to control Mail" (System Settings > Privacy > Automation).
"""
import sqlite3
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import email
import email.policy

from core.accounts.gmail import extract_body

US, RS = "\x1f", "\x1e"   # unit / record separators: never appear in Mail's ids or flags
SYNC_DAYS = 30            # same window as Gmail's first sync
BACKFILL = 100            # older emails per sync that get their local copy filled in

# Each script gets the Mail account name as its first argument. Mail must be set up with the account.
LIST = '''
on run argv
    set cutoff to (current date) - ((item 2 of argv) as integer)
    tell application "Mail"
        -- asked directly on the search, so Mail answers each in one go (same order every time)
        set ids to message id of (messages of mailbox "Inbox" of account (item 1 of argv) whose date received > cutoff)
        set reads to read status of (messages of mailbox "Inbox" of account (item 1 of argv) whose date received > cutoff)
        set flags to flagged status of (messages of mailbox "Inbox" of account (item 1 of argv) whose date received > cutoff)
    end tell
    set out to {}
    repeat with i from 1 to count of ids
        set end of out to (item i of ids) & (character id 31) & ((item i of reads) as text) & (character id 31) & ((item i of flags) as text)
    end repeat
    set AppleScript's text item delimiters to (character id 30)
    return out as text
end run
'''
# argv: account, seconds back (same window as LIST), then position/message-id pairs.
# Fetching by position avoids a search of the whole Inbox per email (5,000+ at UCF);
# the id check makes sure a shifted list never stores the wrong email.
SOURCES = '''
on run argv
    set cutoff to (current date) - ((item 2 of argv) as integer)
    set out to {}
    tell application "Mail"
        set recent to (messages of mailbox "Inbox" of account (item 1 of argv) whose date received > cutoff)
        repeat with k from 3 to (count of argv) by 2
            try
                set m to item ((item k of argv) as integer) of recent
                if (message id of m) is (item (k + 1) of argv) then
                    set end of out to (item (k + 1) of argv) & (character id 31) & (source of m)
                end if
            end try
        end repeat
    end tell
    set AppleScript's text item delimiters to (character id 30)
    return out as text
end run
'''
# Shared by ACT and SEND. Folder names differ between account types, so each role has candidates.
HELPERS = '''
property inboxNames : {"Inbox", "INBOX"}
property archiveNames : {"Archive"}
property junkNames : {"Junk Email", "Junk", "Spam"}
property trashNames : {"Deleted Items", "Deleted Messages", "Trash"}

on findIn(acct, names, mid)
    tell application "Mail"
        repeat with mbx in (mailboxes of account acct)
            if (name of mbx) is in names then
                set found to (messages of mbx whose message id is mid)
                if found is not {} then return item 1 of found
            end if
        end repeat
    end tell
    error "Email not found in " & (item 1 of names) & ": " & mid
end findIn

on boxIn(acct, names)
    tell application "Mail"
        repeat with mbx in (mailboxes of account acct)
            if (name of mbx) is in names then return mbx
        end repeat
    end tell
    error "This account has no " & (item 1 of names) & " folder in Mail"
end boxIn

on splitList(s)
    if s is "" then return {}
    set AppleScript's text item delimiters to ","
    set parts to text items of s
    set AppleScript's text item delimiters to ""
    set out to {}
    repeat with p in parts
        set t to p as text
        repeat while t starts with " "
            set t to text 2 thru -1 of t
        end repeat
        if t is not "" then set end of out to t
    end repeat
    return out
end splitList
'''
# argv: account, verb, message id
ACT = HELPERS + '''
on run argv
    set {acct, verb, mid} to {item 1 of argv, item 2 of argv, item 3 of argv}
    set anywhere to inboxNames & archiveNames & junkNames & trashNames
    tell application "Mail"
        if verb is "source" then return source of my findIn(acct, anywhere, mid)
        if verb is "read" then set read status of my findIn(acct, anywhere, mid) to true
        if verb is "unread" then set read status of my findIn(acct, anywhere, mid) to false
        if verb is "flag" then set flagged status of my findIn(acct, anywhere, mid) to true
        if verb is "unflag" then set flagged status of my findIn(acct, anywhere, mid) to false
        if verb is "trash" then delete my findIn(acct, inboxNames & archiveNames & junkNames, mid)
        if verb is "untrash" then move my findIn(acct, trashNames, mid) to my boxIn(acct, inboxNames)
        if verb is "archive" then move my findIn(acct, inboxNames, mid) to my boxIn(acct, archiveNames)
        if verb is "unarchive" then move my findIn(acct, archiveNames, mid) to my boxIn(acct, inboxNames)
        if verb is "junk" then
            set m to my findIn(acct, inboxNames, mid)
            set junk mail status of m to true
            move m to my boxIn(acct, junkNames)
        end if
        if verb is "notjunk" then
            set m to my findIn(acct, junkNames, mid)
            set junk mail status of m to false
            move m to my boxIn(acct, inboxNames)
        end if
    end tell
    return "ok"
end run
'''
# argv: account, mode (new/reply/replyall/forward), original message id ("" for new), to, cc, subject, body.
# Replies and forwards start from Mail's own reply (threading, forwarded attachments), then take
# the recipients, subject and text you wrote.
SEND = HELPERS + '''
on run argv
    set {acct, mode, mid, toList, ccList, subj, body} to items 1 thru 7 of argv
    tell application "Mail"
        if mode is "new" then
            set msg to make new outgoing message with properties {subject:subj, content:body, visible:false}
        else
            set orig to my findIn(acct, inboxNames & archiveNames & junkNames & trashNames, mid)
            if mode is "forward" then
                set msg to forward orig without opening window
            else if mode is "replyall" then
                set msg to reply orig with reply to all without opening window
            else
                set msg to reply orig without opening window
            end if
            set subject of msg to subj
            set content of msg to body
            delete every to recipient of msg
            delete every cc recipient of msg
        end if
        set sender of msg to (item 1 of (get email addresses of account acct))
        tell msg
            repeat with a in my splitList(toList)
                make new to recipient at end of to recipients with properties {address:(a as text)}
            end repeat
            repeat with a in my splitList(ccList)
                make new cc recipient at end of cc recipients with properties {address:(a as text)}
            end repeat
        end tell
        send msg
    end tell
    return "ok"
end run
'''


def run_script(script: str, *args: str) -> str:
    proc = subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=180)  # a stuck Mail must not hold up Gmail
    if proc.returncode != 0:
        raise RuntimeError(f"Apple Mail: {proc.stderr.strip() or 'script failed'}")
    return proc.stdout.rstrip("\n")


def mail_accounts() -> list:
    """[(name, first email address)] of the accounts set up in Apple Mail."""
    out = run_script('''tell application "Mail"
    set out to {}
    repeat with a in accounts
        set addrs to (get email addresses of a)
        set addr to ""
        if addrs is not {} then set addr to (item 1 of addrs) as text
        set end of out to ((name of a) as text) & (character id 31) & addr
    end repeat
    set AppleScript's text item delimiters to (character id 30)
    return out as text
end tell''')
    return [tuple(r.split(US, 1)) for r in out.split(RS) if US in r]


def add_account(db: sqlite3.Connection, mail_account_name: str) -> int:
    """Register an Apple Mail account. auth_ref_in_keychain holds the Mail account name: no token
    is stored at all, Mail keeps its own login."""
    match = [addr for name, addr in mail_accounts() if name == mail_account_name]
    if not match:
        raise ValueError(f"No account named {mail_account_name!r} in Apple Mail")
    db.execute("INSERT OR IGNORE INTO accounts (provider, address, auth_ref_in_keychain) VALUES ('applemail', ?, ?)",
               (match[0], mail_account_name))
    db.commit()
    return db.execute("SELECT id FROM accounts WHERE address=?", (match[0],)).fetchone()[0]


class AppleMailConnector:
    def __init__(self, account_id: int, db: sqlite3.Connection):
        self.account_id, self.db = account_id, db
        self.mail_name = db.execute("SELECT auth_ref_in_keychain FROM accounts WHERE id=?", (account_id,)).fetchone()[0]

    def sync(self) -> int:
        listed, flagged, order = {}, {}, []
        started = time.time()
        out = run_script(LIST, self.mail_name, str(SYNC_DAYS * 86400))
        for rec in filter(None, out.split(RS)):
            mid, read, flag = (rec.split(US) + ["false"])[:3]
            listed[mid] = read.strip() == "true"
            flagged[mid] = flag.strip() == "true"
            order.append(mid)
        known = {rid: has_raw for rid, has_raw in self.db.execute(
            "SELECT m.remote_id, r.message_id IS NOT NULL FROM messages m "
            "LEFT JOIN message_raw r ON r.message_id = m.id WHERE m.account_id=?", (self.account_id,))}

        new = [(pos, mid) for pos, mid in enumerate(order, 1) if mid not in known]
        # Emails synced before copies were kept: fill those in a little at a time
        missing = [(pos, mid) for pos, mid in enumerate(order, 1) if mid in known and not known[mid]][:BACKFILL]
        new += missing
        stored = 0
        for i in range(0, len(new), 50):  # one osascript run per 50 messages, saved as it goes
            window = SYNC_DAYS * 86400 + round(time.time() - started)  # the same moment LIST used
            pairs = [str(x) for pos, mid in new[i:i + 50] for x in (pos, mid)]
            out = run_script(SOURCES, self.mail_name, str(window), *pairs)
            for rec in filter(None, out.split(RS)):
                mid, source = rec.split(US, 1)
                if mid in known:  # already stored: only the copy was missing
                    self.db.execute("INSERT OR REPLACE INTO message_raw (message_id, raw) SELECT id, ? FROM messages "
                                    "WHERE account_id=? AND remote_id=?",
                                    (source.encode('utf-8', 'surrogateescape'), self.account_id, mid))
                    continue
                self._store(mid, source, unread=not listed[mid])
                stored += 1
            self.db.commit()
            print(f"  {min(i + 50, len(new))}/{len(new)} checked, {stored} stored")
        # Read and flag state changed in Mail or Outlook since last time
        for mid, read in listed.items():
            self.db.execute("UPDATE messages SET unread=?, starred=? WHERE account_id=? AND remote_id=?",
                            (int(not read), int(flagged[mid]), self.account_id, mid))
        # ponytail: gone from the Inbox (deleted or moved elsewhere) = hidden here; folders aren't synced
        cutoff = (datetime.now(timezone.utc).timestamp() - SYNC_DAYS * 86400)
        since = datetime.fromtimestamp(cutoff, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        for (rid,) in self.db.execute("SELECT remote_id FROM messages WHERE account_id=? AND date >= ? "
                                      "AND status IN ('new', 'classified')", (self.account_id, since)).fetchall():
            if rid not in listed:
                self.db.execute("UPDATE messages SET status='trashed' WHERE account_id=? AND remote_id=?",
                                (self.account_id, rid))
        self.db.execute("UPDATE accounts SET last_sync=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(timespec='seconds'), self.account_id))
        self.db.commit()
        print(f"Synced {stored} new messages from Apple Mail ({self.mail_name}).")
        return stored

    def _store(self, mid: str, source: str, unread: bool):
        mail = email.message_from_string(source, policy=email.policy.default)
        try:
            date = parsedate_to_datetime(str(mail.get('Date'))).astimezone(timezone.utc)
        except (TypeError, ValueError):
            date = datetime.now(timezone.utc)
        cur = self.db.execute("""
            INSERT OR IGNORE INTO messages (account_id, remote_id, thread_id, sender, subject, date, body_text, status, unread)
            VALUES (?, ?, NULL, ?, ?, ?, ?, 'new', ?)""",
            (self.account_id, mid, str(mail.get('From', '')), str(mail.get('Subject', '')),
             date.strftime('%Y-%m-%dT%H:%M:%S'), extract_body(mail), int(unread)))
        if cur.rowcount:  # keep the email as sent: reading never has to wait for Mail
            self.db.execute("INSERT OR REPLACE INTO message_raw (message_id, raw) VALUES (?, ?)",
                            (cur.lastrowid, source.encode('utf-8', 'surrogateescape')))

    def fetch_raw(self, remote_id: str) -> bytes:
        return run_script(ACT, self.mail_name, "source", remote_id).encode('utf-8', 'surrogateescape')

    def mark_read(self, remote_id: str, read: bool = True):
        run_script(ACT, self.mail_name, "read" if read else "unread", remote_id)
        self.db.execute("UPDATE messages SET unread=? WHERE account_id=? AND remote_id=?",
                        (int(not read), self.account_id, remote_id))
        self.db.commit()

    # The mail-app actions below run only when you click them (via core.actions): never automatically
    def trash(self, remote_id: str):
        run_script(ACT, self.mail_name, "trash", remote_id)  # Mail moves it to the account's Deleted Items

    def untrash(self, remote_id: str):
        run_script(ACT, self.mail_name, "untrash", remote_id)

    def archive(self, remote_id: str):
        run_script(ACT, self.mail_name, "archive", remote_id)

    def unarchive(self, remote_id: str):
        run_script(ACT, self.mail_name, "unarchive", remote_id)

    def mark_junk(self, remote_id: str):
        run_script(ACT, self.mail_name, "junk", remote_id)

    def not_junk(self, remote_id: str):
        run_script(ACT, self.mail_name, "notjunk", remote_id)

    def set_star(self, remote_id: str, on: bool):
        run_script(ACT, self.mail_name, "flag" if on else "unflag", remote_id)

    def send_fields(self, fields: dict) -> str:
        from core.compose import valid_recipients
        ref = ""
        if fields.get("mode", "new") != "new":
            ref = self.db.execute("SELECT remote_id FROM messages WHERE id=?", (fields["ref"],)).fetchone()[0]
        run_script(SEND, self.mail_name, fields.get("mode", "new"), ref, valid_recipients(fields["to"]),
                   valid_recipients(fields["cc"]) if fields.get("cc") else "",
                   fields.get("subject", ""), fields.get("body", ""))
        return ""  # Mail doesn't report the sent message's id

    def push_labels(self, dry_run: bool = False) -> Counter:
        return Counter()  # sections stay local for Mail accounts

    def web_link(self, remote_id: str) -> str:
        return f"message://%3c{remote_id}%3e"  # opens the email in Apple Mail
