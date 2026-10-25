-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- Accounts
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    address TEXT UNIQUE NOT NULL,
    auth_ref_in_keychain TEXT NOT NULL,
    last_sync TEXT
);

-- Messages
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    remote_id TEXT NOT NULL,
    thread_id TEXT,
    sender TEXT,
    subject TEXT,
    date TEXT,
    body_text TEXT,
    section_id INTEGER,
    status TEXT DEFAULT 'new',
    FOREIGN KEY(account_id) REFERENCES accounts(id),
    FOREIGN KEY(section_id) REFERENCES sections(id),
    UNIQUE(account_id, remote_id)
);

-- Sections
CREATE TABLE IF NOT EXISTS sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    created_by TEXT DEFAULT 'user',
    approved INTEGER DEFAULT 1
);

-- Section Proposals
CREATE TABLE IF NOT EXISTS section_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    reason TEXT,
    status TEXT DEFAULT 'pending'
);

-- Actions Log
CREATE TABLE IF NOT EXISTS actions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER,
    action TEXT NOT NULL,
    params TEXT,
    confidence REAL,
    reversible_info TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Summaries
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    range_start TEXT,
    range_end TEXT,
    text TEXT,
    FOREIGN KEY(account_id) REFERENCES accounts(id)
);

-- Events Created
CREATE TABLE IF NOT EXISTS events_created (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER,
    calendar_event_id TEXT,
    confidence REAL,
    FOREIGN KEY(message_id) REFERENCES messages(id)
);
