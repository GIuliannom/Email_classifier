"""Reading emails, the same for every account: a local copy of each email exactly as sent
(fetched once, then instant and offline), parsed into headers + HTML/text for the reader,
with embedded pictures shown inline and attachments listed."""
import base64
import email
import email.policy
import sqlite3

from core.accounts import connector

INLINE_LIMIT = 8 * 1024 * 1024  # embedded pictures turned into data: URIs, up to this much per email


def raw_for(db: sqlite3.Connection, msg_id: int) -> bytes:
    """The email as sent. Stored at sync (or on first open) so reading never waits on the server."""
    row = db.execute("SELECT r.raw, m.account_id, m.remote_id FROM messages m "
                     "LEFT JOIN message_raw r ON r.message_id = m.id WHERE m.id=?", (msg_id,)).fetchone()
    if not row:
        raise ValueError(f"No message with id {msg_id}")
    raw, account_id, remote_id = row
    if raw is None:
        raw = connector(db, account_id).fetch_raw(remote_id)
        store_raw(db, msg_id, raw)
    return raw


def store_raw(db: sqlite3.Connection, msg_id: int, raw: bytes):
    db.execute("INSERT OR REPLACE INTO message_raw (message_id, raw) VALUES (?, ?)", (msg_id, raw))
    db.commit()


def _parts(mail):
    """Every leaf part, in a fixed order: attachment numbers are positions in this list."""
    return [p for p in mail.walk() if not p.is_multipart()]


def content(raw: bytes) -> dict:
    mail = email.message_from_bytes(raw, policy=email.policy.default)
    html_part = mail.get_body(preferencelist=('html',))
    text_part = mail.get_body(preferencelist=('plain',))
    out = {h: str(mail.get(h, '')) for h in ('From', 'To', 'Cc', 'Date', 'Subject', 'Reply-To', 'Message-ID')}
    try:
        out['html'] = html_part.get_content() if html_part else None
        out['text'] = text_part.get_content() if text_part else None
    except Exception:  # odd encodings: the caller falls back to the stored text
        out['html'], out['text'] = None, None

    attachments, inline_bytes = [], 0
    for i, part in enumerate(_parts(mail)):
        if part is html_part or part is text_part:
            continue
        cid = (part.get('Content-ID') or '').strip('<> ')
        payload = part.get_payload(decode=True) or b''
        if cid and out['html'] and f'cid:{cid}' in out['html'] and part.get_content_maintype() == 'image' \
                and inline_bytes + len(payload) <= INLINE_LIMIT:
            # Pictures embedded in the email itself: show them (no remote loading involved)
            uri = f"data:{part.get_content_type()};base64,{base64.b64encode(payload).decode()}"
            out['html'] = out['html'].replace(f'cid:{cid}', uri)
            inline_bytes += len(payload)
        elif part.get_filename() or part.get_content_disposition() == 'attachment':
            attachments.append({'index': i, 'filename': part.get_filename() or f'attachment-{i}',
                                'type': part.get_content_type(), 'size': len(payload)})
    out['attachments'] = attachments
    return out


def attachment(raw: bytes, index: int) -> tuple:
    """(filename, content type, bytes) of one attachment, by its index from content()."""
    parts = _parts(email.message_from_bytes(raw, policy=email.policy.default))
    if not 0 <= index < len(parts):
        raise ValueError(f"No attachment {index}")
    part = parts[index]
    return part.get_filename() or f'attachment-{index}', part.get_content_type(), part.get_payload(decode=True) or b''
