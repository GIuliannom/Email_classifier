"""Google Calendar: create and delete the events the app adds (same login as Gmail)."""
import sqlite3
from datetime import datetime, timedelta

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from core.accounts.gmail import credentials, RETRIES

CALENDAR_SCOPE = 'https://www.googleapis.com/auth/calendar.events'


def _service(db: sqlite3.Connection, account_id: int):
    return build('calendar', 'v3', credentials=credentials(db, account_id, CALENDAR_SCOPE))


def event_body(title: str, start: str, end: str, location: str, description: str) -> dict:
    """start/end: 'YYYY-MM-DD' makes an all-day event, 'YYYY-MM-DDTHH:MM' a timed one in local time."""
    if len(start) == 10:
        day = datetime.fromisoformat(start).date()
        times = {'start': {'date': day.isoformat()}, 'end': {'date': (day + timedelta(days=1)).isoformat()}}
    else:
        s = datetime.fromisoformat(start).astimezone()  # naive -> this Mac's time zone
        e = datetime.fromisoformat(end).astimezone() if end and len(end) > 10 else None
        if e is None or e <= s:
            e = s + timedelta(hours=1)
        times = {'start': {'dateTime': s.isoformat()}, 'end': {'dateTime': e.isoformat()}}
    return {'summary': title, 'location': location or '', 'description': description, **times}


def create_event(db: sqlite3.Connection, account_id: int, body: dict) -> str:
    created = _service(db, account_id).events().insert(calendarId='primary', body=body).execute(num_retries=RETRIES)
    return created['id']


def delete_event(db: sqlite3.Connection, account_id: int, event_id: str):
    try:
        _service(db, account_id).events().delete(calendarId='primary', eventId=event_id).execute(num_retries=RETRIES)
    except HttpError as e:
        if e.resp.status not in (404, 410):  # already gone (you deleted it in Calendar): fine
            raise
