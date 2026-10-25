import sqlite3
import os
from typing import List, Tuple

# Minimal migration system: numbered SQL steps
MIGRATIONS: List[Tuple[int, str]] = [
    (1, open(os.path.join(os.path.dirname(__file__), "schema.sql")).read()),
    # Starting sections with one-line definitions (used when labeling and by the classifier).
    # INSERT OR IGNORE: adds the new ones to existing DBs, seeds all of them on fresh ones.
    (2, """
        INSERT OR IGNORE INTO sections (name, created_by) VALUES
            ('News', 'user'), ('Banks', 'user'), ('Personal', 'user'), ('Social', 'user'),
            ('Unsorted', 'user'), ('Ads', 'user'), ('Updates', 'user'), ('Jobs', 'user');
        UPDATE sections SET description = CASE name
            WHEN 'News' THEN 'Newsletters, news digests and articles (world, tech, sports news).'
            WHEN 'Banks' THEN 'From banks, cards, lenders and payment apps: statements, bills, payments, loans, credit scores.'
            WHEN 'Personal' THEN 'Written by a real person you know (friends, family, classmates, professors). Never automated mail.'
            WHEN 'Social' THEN 'Notifications from social networks and community apps (LinkedIn, Instagram, Discord, Reddit, fan clubs).'
            WHEN 'Ads' THEN 'Promotions, sales, deals, coupons and marketing, even from companies you use.'
            WHEN 'Updates' THEN 'Automated mail about your own accounts: orders, deliveries, store receipts, verification codes, security alerts, sign-ups, policy changes.'
            WHEN 'Jobs' THEN 'Job listings, job alerts, applications and recruiters.'
            WHEN 'Unsorted' THEN 'Fits none of the other sections, or genuinely unclear.'
            ELSE description END;
    """),
    # Strip invisible preview padding (soft hyphen, CGJ, zero-width chars, BOM) from stored bodies
    (3, """
        UPDATE messages SET body_text = replace(replace(replace(replace(replace(replace(replace(
            body_text, char(173), ''), char(847), ''), char(8203), ''), char(8204), ''),
            char(8205), ''), char(8288), ''), char(65279), '');
    """),
    # Sharper lines between Ads, Banks and Updates: promotions from banks, stores and apps are Ads
    (4, """
        UPDATE sections SET description = CASE name
            WHEN 'Ads' THEN 'Promotions, sales, deals, coupons, rewards and points offers, wishlist sales, ticket and event promotions, new product launches. Ads even when sent by a bank, store, game or app you use.'
            WHEN 'Banks' THEN 'Your own money accounts: statements, bills due, payments sent or received, card and loan status, credit scores. Not their offers or promotions (those are Ads).'
            WHEN 'Updates' THEN 'Automated notices about your own accounts and activity: orders, deliveries, store receipts, refunds, verification codes, security alerts, sign-ups and invitations, terms and policy changes. Not sales or offers (those are Ads).'
            ELSE description END;
    """),
    # Undo marks an action instead of deleting it, so the log stays a full history
    (5, "ALTER TABLE actions_log ADD COLUMN undone_at TEXT;"),
    # Which AI/<Section> label Gmail currently has, so push-labels only sends changes
    (6, "ALTER TABLE messages ADD COLUMN remote_label TEXT;"),
    # Day 4-5: section proposals carry a definition and the emails that motivated them;
    # app settings; calendar events waiting for your answer; which emails were checked for events
    (7, """
        ALTER TABLE section_proposals ADD COLUMN description TEXT;
        ALTER TABLE section_proposals ADD COLUMN example_ids TEXT;
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        INSERT OR IGNORE INTO settings (key, value) VALUES ('summary_time', '21:00');
        CREATE TABLE IF NOT EXISTS pending_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER UNIQUE REFERENCES messages(id),
            title TEXT, start TEXT, end TEXT, location TEXT, confidence REAL, reason TEXT,
            status TEXT DEFAULT 'pending'
        );
        ALTER TABLE messages ADD COLUMN events_checked INTEGER DEFAULT 0;
    """),
    # Mail window: read/unread, mirrored from the provider (NULL = not known yet, filled on next sync)
    (8, "ALTER TABLE messages ADD COLUMN unread INTEGER;"),
    # UCF is school and work in one account: separate sections for each, and Personal narrowed
    # (it listed classmates and professors, which pulled school mail into it)
    (9, """
        INSERT OR IGNORE INTO sections (name, created_by) VALUES ('School', 'user'), ('Work', 'user');
        UPDATE sections SET description = CASE name
            WHEN 'School' THEN 'Your studies: classes, professors and classmates, exams, Canvas/Webcourses, registration, tuition and financial aid, advising, campus offices and student organizations. University-wide newsletters are News.'
            WHEN 'Work' THEN 'Your job, including at the university: supervisor and colleagues, research or lab group, teaching assistant duties, payroll, timesheets, HR and benefits (retirement plans, Workday).'
            WHEN 'Personal' THEN 'Written personally by friends or family. Never automated mail; school and work matters go to School or Work.'
            ELSE description END;
    """),
    # Sections an account never uses (e.g. no Banks at UCF): the classifier and window skip them there
    (10, """
        CREATE TABLE IF NOT EXISTS hidden_sections (
            account_id INTEGER REFERENCES accounts(id),
            section_id INTEGER REFERENCES sections(id),
            PRIMARY KEY (account_id, section_id)
        );
    """),
    # Mail app: stars, a local copy of each email as sent (instant, offline reading and
    # attachments), and full-text search over sender, subject and text
    (11, """
        ALTER TABLE messages ADD COLUMN starred INTEGER DEFAULT 0;
        CREATE TABLE IF NOT EXISTS message_raw (message_id INTEGER PRIMARY KEY REFERENCES messages(id), raw BLOB);
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            subject, sender, body_text, content='messages', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
        CREATE TRIGGER IF NOT EXISTS messages_fts_ins AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, subject, sender, body_text) VALUES (new.id, new.subject, new.sender, new.body_text);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_fts_del AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, subject, sender, body_text)
            VALUES ('delete', old.id, old.subject, old.sender, old.body_text);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_fts_upd AFTER UPDATE OF subject, sender, body_text ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, subject, sender, body_text)
            VALUES ('delete', old.id, old.subject, old.sender, old.body_text);
            INSERT INTO messages_fts(rowid, subject, sender, body_text) VALUES (new.id, new.subject, new.sender, new.body_text);
        END;
        INSERT INTO messages_fts(messages_fts) VALUES ('rebuild');
    """),
]

def init_db(db_path: str = "data/mail.db") -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    migrate(conn)
    return conn

def migrate(conn: sqlite3.Connection):
    # Ensure schema_version table exists before querying it on fresh DBs
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    current_version = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
    
    for version, sql in MIGRATIONS:
        if version > current_version:
            print(f"Applying migration {version}...")
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            conn.commit()
