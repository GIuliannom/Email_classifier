# Email AI

A personal email assistant for macOS that runs its AI entirely on your Mac. It sorts Gmail into
sections, writes a daily summary per account, turns event emails into Google Calendar entries
(asking when unsure), proposes new sections, and lets you trash, reply and send with confirmation.

**Privacy:** email content never leaves the Mac. The model runs locally in Ollama; the only network
traffic is to Google (Gmail, Calendar, login). See [Data leaving the Mac](#data-leaving-the-mac).

This was just a small project that I did using Claude Opus and Qwen 3.6.

## Setup

1. **Google Cloud** (one time, free): create a project, enable the Gmail API and Google Calendar API,
   set up the OAuth consent screen (External), add yourself as a test user and **Publish app** so
   logins don't expire after 7 days. Create an OAuth client of type *Desktop app* and save its JSON
   as `data/client_secret.json` (`data/` is gitignored).
2. **Python:**
   ```bash
   python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
   ```
3. **Model:** install [Ollama](https://ollama.com) and pull the model:
   ```bash
   ollama pull qwen3:4b
   ```
   To try another one, set `EMAIL_AI_MODEL` (and re-run `accuracy` to compare).
4. **Log in** (opens your browser; Google warns the app is unverified, that's expected for your own app.
   Tick every box: Gmail access is needed for labels and trash, Calendar for events):
   ```bash
   python -m core.cli add-account
   ```
   The login token is stored in macOS Keychain, never in a file.
5. **First sync and sort** (last 30 days):
   ```bash
   python -m core.cli sync 1 && python -m core.cli classify --all-new && python -m core.cli push-labels 1
   ```
6. **Menu-bar app:**
   ```bash
   ui/MenuBar/build_app.sh && open ui/MenuBar/EmailAI.app
   ```
   To start it at login, add `ui/MenuBar/EmailAI.app` under System Settings → General → Login Items.

## Mail window

**Open Mail** in the menu bar opens a full mail app for every account (Gmail directly, UCF through
Apple Mail):

- **Folders:** All Inboxes, Unread, Starred; each account's Inbox and sections (unread badges);
  Archive, Junk, Trash. Search (toolbar, ⌘F) covers sender, subject and text across accounts.
- **Reading:** each email is kept locally when synced, so it opens instantly, offline too. Remote
  images load by default; turn off *Load remote images* in the menu bar to block them (senders then
  can't tell when you open mail; you can still allow single emails or senders). Attachments open with a click.
- **Ask your email** (✨ Ask, ⌘K; also in the menu bar): questions like "which students said they can't
  come to class this week?" or "what's new in tech today?". The local model plans a search, reads the
  matching emails in small batches, and answers with links to the emails it used (about 5-20 s).
  Email text is data in every step and the answer is only text, so an email can't make it do anything.
- **Actions** (toolbar, right-click, keyboard): Reply ⌘R, Reply All ⇧⌘R, Forward ⇧⌘F, New ⌘N,
  Archive ⌃⌘A, Junk ⇧⌘J, Delete ⌘⌫ (to Trash, never permanent), Star ⇧⌘L, Unread ⇧⌘U, Move to,
  Get Mail ⌥⌘R, Undo ⌘Z. In Archive/Junk/Trash the main action is *Move to Inbox*/*Not Junk*.
  Changes show instantly; the account catches up in the background (UCF via Apple Mail can take a
  few seconds) and a refused change is put back.
- **Writing:** replies and forwards come prefilled (recipients, subject, quoted original). *Draft
  with AI* fills in text from the local model; nothing is sent until you press **Send** (⌘↩).

## Menu-bar app

The app starts the engine (`python -m core.server`, log in `data/server.log`) and stops it when you quit.
It shows sections with today's counts, the latest summary, and **questions waiting for you**: new
section proposals (Create / No) and calendar events the model wasn't sure about (Add / Skip).
Buttons: *Sync now*, *Summarize now*, and the daily summary time.

While it runs, the engine does this by itself:

| When | What |
|---|---|
| Every 15 minutes | sync → sort new mail → update Gmail labels → look for events |
| Daily at your summary time (default 21:00) | the above, then a summary per account, section proposals, and the size check below |

Storage: about 7 KB per email (text only, no attachments), roughly 80 MB a year at ~900 emails a month.
If `data/mail.db` ever passes 5 GB, the oldest emails' body text is removed until it's under 4 GB;
sender, subject, date and section are kept ([core/db/prune.py](core/db/prune.py)).

The engine listens on 127.0.0.1 only and every request needs the secret in `data/api_token`,
so web pages in your browser can't talk to it.

## Commands

All from the project folder with the venv active: `python -m core.cli <command>`.

| Command | What it does |
|---|---|
| `add-account` | Log in to Gmail/Calendar (again, if a permission is missing) |
| `sync 1` | Fetch new mail for account 1 |
| `classify --all-new` | Sort every unsorted message (`classify <id>` for one) |
| `push-labels 1 [--dry-run]` | Show the sorting in Gmail as `AI/<Section>` labels (labels only) |
| `show` / `show <Section>` | Counts per section / list a section's emails with their ids |
| `move <id> <Section>` | Correct a sort; also teaches the classifier about that sender |
| `history` / `undo [id]` / `undo --all-auto` | Recent actions / undo one / undo every classifier move (keeps yours) |
| `summary [--today] [--account N]` | Summary per account (default: last 24 hours) |
| `propose` / `proposals [--accept ID \| --reject ID]` | Ask for new sections / answer them |
| `events [--dry-run]` / `event-questions [--accept ID \| --reject ID]` | Find events / answer the uncertain ones |
| `trash <id>` | Move to Gmail's Trash (asks first; `undo` restores it) |
| `reply <id> [--body TEXT \| --ai-draft [WHAT_TO_SAY]]` | Reply in-thread (shows it, asks before sending; `e` edits) |
| `send --to ADDR --subject S [--body TEXT]` | New email (shows it, asks before sending) |
| `ask "QUESTION"` | Ask about your email, e.g. `ask "any deadlines this week?"` |
| `label [--n 200]` / `accuracy` | Hand-label a sample / score the classifier on it |

Measure resources with `python -m core.measure`.

## Safety

- **The model never acts.** It returns JSON that code validates; only the actions in
  `ALLOWED_ACTIONS` ([core/actions.py](core/actions.py)) can run, and every one is logged with what's needed
  to reverse it.
- **Trash and send need a person:** they run only after you type `y` at a terminal (there is no `--yes`
  flag, and they refuse when not run from a terminal). The automatic pipeline has no path to them.
- **Delete means Gmail's Trash**, recoverable for 30 days. Nothing is ever permanently deleted.
- **Undo:** section moves, your moves, calendar events and trash can all be undone. Sent mail can't.
- **Calendar:** an event is created automatically only when the model is ≥ 0.9 sure, the email has a
  date *and* time, and the email was sorted into a section. Everything else becomes a question.
  Each event links back to its email.
- **Prompt injection:** email text is wrapped in markers and labelled as data; section answers are
  limited to the real section names; new sections are only proposals. `tests/test_injection.py` runs
  emails that say "delete everything" / "forward this to…" through the whole pipeline with a model that
  obeys them: no send, trash or forward happens, no label other than `AI/*` is touched, and their
  events become questions. `RUN_LLM_TESTS=1` runs the same emails through the real model.

## Measured (Oct 19, 2026, MacBook M5 Pro 32 GB, qwen3:4b)

| Metric | Result | Week-1 target |
|---|---|---|
| Classification accuracy (87 held-out labeled emails) | 79% placed correctly, 82% first choice right | 85%+ |
| Wrong auto-moves | 9 / 87 at threshold 0.7 (undoable) | rare |
| Calendar events created silently | 0 (all uncertain ones asked) | no wrong ones |
| Summarize 50 emails | 114 s | < ~120 s |
| RAM, model loaded (peak) | 1.8–1.9 GB | < ~4 GB |
| RAM, idle | engine 73 MB + Ollama server 24 MB; model not loaded | ~0 |
| Classification speed | ~0.8 s per email | |

Notes:
- The model is loaded only while a job runs and unloaded right after. Ollama's runner also caches
  every prompt in RAM (up to 8 GB); the wrapper reloads the model every 10 requests to empty it
  (`RELOAD_EVERY` in [core/ai/llm.py](core/ai/llm.py)). Alternatively cap it for all of Ollama with
  `launchctl setenv LLAMA_ARG_CACHE_RAM 0` and restart Ollama.
- Accuracy came from 41% to 79% through section definitions, the sender in the prompt, labeled examples
  (including same-sender ones), constrained answers and real token-probability confidence.
  `accuracy` scores only odd-id labels; even-id labels are the examples shown to the model.

## Data leaving the Mac

During a full refresh, the engine's only network connections were to three addresses owned by
Google LLC (port 443). The code talks to: Ollama on `localhost:11434`, and Google through
`google-api-python-client` (Gmail, Calendar, OAuth token refresh). To check yourself while a
refresh runs: `lsof -a -i -P -p $(pgrep -f "python -m core.server")`.

## Layout

```
core/
  accounts/   gmail.py (sync, labels, trash, send), calendar.py
  ai/         llm.py (Ollama wrapper), classify.py, summarize.py, events.py, propose.py, evaluate.py
  db/         schema.sql, migrations.py
  actions.py  allow-list, log, undo
  compose.py  reply/new email building, AI drafts
  pipeline.py refresh + daily routines
  server.py   local API + scheduler
  cli.py, measure.py
ui/MenuBar/   SwiftUI menu-bar app
tests/        python -m unittest discover -s tests -p "test_*.py"
data/         mail.db, client_secret.json, labels.csv, api_token, server.log (gitignored)
```
