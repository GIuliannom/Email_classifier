import html
import os
import re
import json
import base64
import email
import email.policy
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from typing import List, Tuple

import keyring
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# gmail.modify = read + labels/trash (no permanent delete). Same Keychain entry as the .scratch scripts.
GMAIL_MODIFY = 'https://www.googleapis.com/auth/gmail.modify'
SCOPES = [
    GMAIL_MODIFY,
    'https://www.googleapis.com/auth/calendar.events'
]
LABEL_PREFIX = 'AI'  # labels show in Gmail as AI/Ads, AI/Banks, ... under one parent
KEYCHAIN_SERVICE = 'local-ai-email-client'
CLIENT_SECRETS_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'client_secret.json')
# Gmail limits units per minute per user; on 429/rate-limit 403 the client retries with
# exponential backoff (~2^n s). 7 retries covers waiting out a full minute window.
RETRIES = 7


def add_account(db: sqlite3.Connection, keychain_ref: str = 'gmail_token') -> int:
    """Log in (reusing a saved Keychain token if there is one) and register the account."""
    token_json = keyring.get_password(KEYCHAIN_SERVICE, keychain_ref)
    if token_json and set(SCOPES) <= set(json.loads(token_json).get('scopes') or []):
        creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    else:
        # No token yet, or it predates a permission the app now needs (e.g. labels): log in again
        print("Opening your browser to grant Gmail and Calendar access...")
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_PATH, SCOPES)
        creds = flow.run_local_server(port=8080)
        keyring.set_password(KEYCHAIN_SERVICE, keychain_ref, creds.to_json())
    address = build('gmail', 'v1', credentials=creds).users().getProfile(userId='me').execute(num_retries=RETRIES)['emailAddress']
    db.execute(
        "INSERT OR IGNORE INTO accounts (provider, address, auth_ref_in_keychain) VALUES ('gmail', ?, ?)",
        (address, keychain_ref)
    )
    db.commit()
    return db.execute("SELECT id FROM accounts WHERE address=?", (address,)).fetchone()[0]


def credentials(db: sqlite3.Connection, account_id: int, need_scope: str = None) -> Credentials:
    """The account's saved Google login from Keychain (shared by Gmail and Calendar)."""
    row = db.execute("SELECT auth_ref_in_keychain FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise RuntimeError(f"No account with id {account_id}. Run: python -m core.cli add-account")
    token_json = keyring.get_password(KEYCHAIN_SERVICE, row[0])
    if not token_json:
        raise RuntimeError(f"No Keychain token '{row[0]}'. Run: python -m core.cli add-account")
    # Use the scopes this token was actually granted: asking for more on refresh fails, and
    # an older read-only token should still sync. googleapiclient refreshes it hourly.
    creds = Credentials.from_authorized_user_info(json.loads(token_json))
    if need_scope and need_scope not in (creds.scopes or []):
        raise RuntimeError(f"Permission {need_scope.rsplit('/', 1)[-1]} missing. "
                           "Run: python -m core.cli add-account (logs in once more)")
    return creds


class GmailConnector:
    def __init__(self, account_id: int, db_conn: sqlite3.Connection):
        self.account_id = account_id
        self.db = db_conn

    def _service(self, need_modify: bool = False):
        return build('gmail', 'v1', credentials=credentials(self.db, self.account_id,
                                                            GMAIL_MODIFY if need_modify else None))

    def sync(self) -> int:
        svc = self._service()
        last_hist = self.db.execute(
            "SELECT last_sync FROM accounts WHERE id=?", (self.account_id,)
        ).fetchone()[0]

        ids = None
        if last_hist:
            try:
                ids, new_hist = self._history_ids(svc, last_hist)
            except HttpError as e:
                # Gmail forgets history after about a week: fall back to a full sync
                if e.resp.status != 404:
                    raise
        if ids is None:
            # Read historyId before listing so nothing arriving mid-sync is missed
            new_hist = svc.users().getProfile(userId='me').execute(num_retries=RETRIES)['historyId']
            ids = self._list_ids(svc, 'newer_than:30d')

        stored = 0
        for msg_id in ids:
            if self.db.execute(
                "SELECT 1 FROM messages WHERE account_id=? AND remote_id=?", (self.account_id, msg_id)
            ).fetchone():
                continue
            try:
                raw = svc.users().messages().get(userId='me', id=msg_id, format='raw').execute(num_retries=RETRIES)
            except HttpError as e:
                if e.resp.status == 404:  # deleted between listing and fetching
                    continue
                raise
            self._store_message(raw)
            stored += 1
            if stored % 100 == 0:
                print(f"  {stored}/{len(ids)} fetched...")

        if self.db.execute("SELECT 1 FROM messages WHERE account_id=? AND unread IS NULL LIMIT 1",
                           (self.account_id,)).fetchone():
            # Messages stored before unread was tracked: one search fills them all in
            unread_ids = set(self._list_ids(svc, 'is:unread'))
            for (rid,) in self.db.execute("SELECT remote_id FROM messages WHERE account_id=? AND unread IS NULL",
                                          (self.account_id,)).fetchall():
                self.db.execute("UPDATE messages SET unread=? WHERE account_id=? AND remote_id=?",
                                (int(rid in unread_ids), self.account_id, rid))

        self.db.execute("UPDATE accounts SET last_sync=? WHERE id=?", (str(new_hist), self.account_id))
        self.db.commit()
        print(f"Synced {stored} new messages. historyId: {new_hist}")
        return stored

    def _list_ids(self, svc, q: str) -> List[str]:
        ids, page = [], None
        while True:
            data = svc.users().messages().list(userId='me', q=q, maxResults=500, pageToken=page).execute(num_retries=RETRIES)
            ids += [m['id'] for m in data.get('messages', [])]
            page = data.get('nextPageToken')
            if not page:
                return ids

    def _history_ids(self, svc, history_id: str) -> Tuple[List[str], str]:
        """New message ids since history_id. Also applies label changes made elsewhere
        (read in Gmail on the phone, trashed on the web) to the stored messages."""
        ids, page, latest = [], None, history_id
        while True:
            data = svc.users().history().list(
                userId='me', startHistoryId=history_id, pageToken=page,
                historyTypes=['messageAdded', 'labelAdded', 'labelRemoved']
            ).execute(num_retries=RETRIES)
            latest = data.get('historyId', latest)
            for entry in data.get('history', []):
                for change in entry.get('messagesAdded', []):
                    if change['message']['id'] not in ids:
                        ids.append(change['message']['id'])
                for key, added in (('labelsAdded', True), ('labelsRemoved', False)):
                    for change in entry.get(key, []):
                        self._apply_labels(change['message']['id'], change.get('labelIds', []), added)
            page = data.get('nextPageToken')
            if not page:
                return ids, latest

    def _apply_labels(self, remote_id: str, label_ids: list, added: bool):
        """Mirror changes made in Gmail itself (phone, web) onto the stored message."""
        where = "account_id=? AND remote_id=?"
        key = (self.account_id, remote_id)
        # Back in the inbox: sorted mail shows again, never-sorted mail gets sorted
        back = "status = CASE WHEN section_id IS NULL THEN 'new' ELSE 'classified' END"
        if 'UNREAD' in label_ids:
            self.db.execute(f"UPDATE messages SET unread=? WHERE {where}", (int(added), *key))
        if 'STARRED' in label_ids:
            self.db.execute(f"UPDATE messages SET starred=? WHERE {where}", (int(added), *key))
        for label, status in (('TRASH', 'trashed'), ('SPAM', 'junk')):
            if label in label_ids:
                if added:
                    self.db.execute(f"UPDATE messages SET status='{status}' WHERE {where}", key)
                else:
                    self.db.execute(f"UPDATE messages SET {back} WHERE {where} AND status='{status}'", key)
        if 'INBOX' in label_ids:  # archived / moved back to the inbox
            if added:
                self.db.execute(f"UPDATE messages SET {back} WHERE {where} AND status='archived'", key)
            else:
                self.db.execute(f"UPDATE messages SET status='archived' WHERE {where} "
                                "AND status IN ('new', 'classified')", key)

    # Trash and send are only ever called from core.actions after you typed "y" (never by the
    # automatic pipeline; tests/test_injection.py checks that). Trash is Gmail's 30-day Trash,
    # never permanent delete.
    def trash(self, remote_id: str):
        self._service(need_modify=True).users().messages().trash(userId='me', id=remote_id).execute(num_retries=RETRIES)

    def untrash(self, remote_id: str):
        self._service(need_modify=True).users().messages().untrash(userId='me', id=remote_id).execute(num_retries=RETRIES)

    def send(self, mime_bytes: bytes, thread_id: str = None) -> str:
        body = {'raw': base64.urlsafe_b64encode(mime_bytes).decode()}
        if thread_id:
            body['threadId'] = thread_id
        # No num_retries on purpose: if Gmail got the first attempt but the reply was lost, a retry sends it twice
        return self._service(need_modify=True).users().messages().send(userId='me', body=body).execute()['id']

    def fetch_raw(self, remote_id: str) -> bytes:
        """The email exactly as sent (core.mail keeps a local copy)."""
        raw = self._service().users().messages().get(userId='me', id=remote_id, format='raw').execute(num_retries=RETRIES)
        return base64.urlsafe_b64decode(raw['raw'])

    def _modify(self, remote_id: str, add=(), remove=()):
        body = {k: list(v) for k, v in (('addLabelIds', add), ('removeLabelIds', remove)) if v}
        self._service(need_modify=True).users().messages().modify(userId='me', id=remote_id, body=body).execute(num_retries=RETRIES)

    # The mail-app actions below run only when you click them (via core.actions): never automatically
    def archive(self, remote_id: str):
        self._modify(remote_id, remove=['INBOX'])

    def unarchive(self, remote_id: str):
        self._modify(remote_id, add=['INBOX'])

    def mark_junk(self, remote_id: str):
        self._modify(remote_id, add=['SPAM'], remove=['INBOX'])  # Gmail's Spam: also trains Gmail's filter

    def not_junk(self, remote_id: str):
        self._modify(remote_id, add=['INBOX'], remove=['SPAM'])

    def set_star(self, remote_id: str, on: bool):
        self._modify(remote_id, add=['STARRED'] if on else (), remove=() if on else ['STARRED'])

    def send_fields(self, fields: dict) -> str:
        from core import compose  # built here; Apple Mail accounts let Mail build their own
        mime, thread_id = compose.build_mime(self.db, self.account_id, fields)
        return self.send(mime.as_bytes(), thread_id)

    def web_link(self, remote_id: str) -> str:
        return f"https://mail.google.com/mail/u/0/#all/{remote_id}"

    def mark_read(self, remote_id: str, read: bool = True):
        body = {'removeLabelIds': ['UNREAD']} if read else {'addLabelIds': ['UNREAD']}
        self._service(need_modify=True).users().messages().modify(userId='me', id=remote_id, body=body).execute(num_retries=RETRIES)
        self.db.execute("UPDATE messages SET unread=? WHERE account_id=? AND remote_id=?",
                        (int(not read), self.account_id, remote_id))
        self.db.commit()

    def push_labels(self, dry_run: bool = False) -> Counter:
        """Make Gmail match the local sorting: one AI/<Section> label per message (none for
        Unsorted). Only labels change; nothing is archived, moved or deleted. Returns a count
        of changes as {(old, new): n}."""
        rows = self.db.execute("""
            SELECT m.id, m.remote_id, m.remote_label, s.name FROM messages m
            LEFT JOIN sections s ON s.id = m.section_id WHERE m.account_id=?""", (self.account_id,)).fetchall()
        groups = {}
        for msg_id, remote_id, have, section in rows:
            want = section if section and section != 'Unsorted' else None
            if want != have:
                groups.setdefault((have, want), []).append((msg_id, remote_id))
        counts = Counter({k: len(v) for k, v in groups.items()})
        if dry_run or not groups:
            return counts

        svc = self._service(need_modify=True)
        label_ids = self._ensure_labels(svc, {want for _, want in groups if want})
        for (have, want), msgs in groups.items():
            for i in range(0, len(msgs), 1000):  # batchModify takes up to 1000 ids
                chunk = msgs[i:i + 1000]
                body = {'ids': [rid for _, rid in chunk]}
                if want:
                    body['addLabelIds'] = [label_ids[want]]
                if have and have in label_ids:  # skip if you deleted that label in Gmail
                    body['removeLabelIds'] = [label_ids[have]]
                svc.users().messages().batchModify(userId='me', body=body).execute(num_retries=RETRIES)
                self.db.executemany("UPDATE messages SET remote_label=? WHERE id=?",
                                    [(want, msg_id) for msg_id, _ in chunk])
                self.db.commit()  # per chunk, so an interrupted push resumes cleanly
        return counts

    def _ensure_labels(self, svc, sections: set) -> dict:
        """{section: Gmail label id} for every existing AI/* label, creating the needed ones."""
        existing = {l['name']: l['id'] for l in
                    svc.users().labels().list(userId='me').execute(num_retries=RETRIES).get('labels', [])}
        for name in [LABEL_PREFIX] + sorted(f"{LABEL_PREFIX}/{s}" for s in sections):
            if name not in existing:  # the parent label makes Gmail nest AI/<Section> under it
                created = svc.users().labels().create(userId='me', body={
                    'name': name, 'labelListVisibility': 'labelShow', 'messageListVisibility': 'show'
                }).execute(num_retries=RETRIES)
                existing[name] = created['id']
        prefix = LABEL_PREFIX + '/'
        return {n[len(prefix):]: i for n, i in existing.items() if n.startswith(prefix)}

    def _store_message(self, raw: dict):
        raw_bytes = base64.urlsafe_b64decode(raw['raw'])
        mail = email.message_from_bytes(raw_bytes, policy=email.policy.default)
        # Stored as naive UTC ISO so date ranges compare correctly as strings
        date = datetime.fromtimestamp(int(raw['internalDate']) / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        labels = raw.get('labelIds', [])
        # Only inbox mail is sorted and shown in the inbox views (sent and archived mail isn't)
        status = ('trashed' if 'TRASH' in labels else 'junk' if 'SPAM' in labels
                  else 'new' if 'INBOX' in labels or not labels else 'archived')
        cur = self.db.execute("""
            INSERT OR IGNORE INTO messages (account_id, remote_id, thread_id, sender, subject, date, body_text, status, unread, starred)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            self.account_id, raw['id'], raw.get('threadId'),
            str(mail.get('From', '')), str(mail.get('Subject', '')), date, extract_body(mail),
            status, int('UNREAD' in labels), int('STARRED' in labels)
        ))
        if cur.rowcount:  # keep the email as sent: reading it later is instant and offline
            self.db.execute("INSERT OR REPLACE INTO message_raw (message_id, raw) VALUES (?, ?)",
                            (cur.lastrowid, raw_bytes))
        self.db.commit()  # per message, so an interrupted first sync keeps its progress


def extract_body(mail) -> str:
    part = mail.get_body(preferencelist=('plain', 'html'))
    if part is None:
        return ''
    try:
        text = part.get_content()
    except Exception:
        return ''
    if part.get_content_type() == 'text/html':
        # ponytail: regex tag strip leaves some CSS/JS text behind; use html.parser if it hurts classification
        text = html.unescape(re.sub(r'(?s)<(style|script).*?</\1>|<[^>]+>', ' ', text))  # &nbsp; &amp; ...
    # Newsletters pad the preview with invisible characters; they waste model context
    text = re.sub('[\u00ad\u034f\u200b-\u200d\u2060\ufeff]', '', text)
    return re.sub(r'\s+', ' ', text).strip()
