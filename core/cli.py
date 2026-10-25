import argparse
import sys
from core.db.migrations import init_db
from core.accounts.gmail import add_account
from core.ai.classify import classify_message, classify_all_new
from core.ai import classify
from core.ai import evaluate
from core.ai.summarize import generate_summary
from core import actions
from core.ai.llm import LocalLLM
from core.ai import propose, events
from core import compose
import os
import subprocess

def main(argv=None):
    parser = argparse.ArgumentParser(description="Local AI Email Client Core")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # add-account
    subparsers.add_parser("add-account", help="Log in to Gmail and register the account")
    am_parser = subparsers.add_parser("add-applemail", help="Add an account through Apple Mail (e.g. UCF)")
    am_parser.add_argument("name", nargs="?", help="The account's name in Apple Mail (omit to list them)")

    # sync
    sync_parser = subparsers.add_parser("sync", help="Sync Gmail account")
    sync_parser.add_argument("account_id", type=int, help="ID of the account in DB")

    # push-labels
    push_parser = subparsers.add_parser("push-labels", help="Show the sorting in Gmail as AI/<Section> labels")
    push_parser.add_argument("account_id", type=int)
    push_parser.add_argument("--dry-run", action="store_true", help="Only show what would change")

    # classify
    classify_parser = subparsers.add_parser("classify", help="Classify one message, or all new ones")
    classify_parser.add_argument("message_id", type=int, nargs="?", help="ID of the message to classify")
    classify_parser.add_argument("--all-new", action="store_true", help="Classify every message not classified yet")
    classify_parser.add_argument("--limit", type=int, help="With --all-new: only the N newest")

    sec_parser = subparsers.add_parser("sections", help="Sections per account; hide ones an account never uses")
    sec_parser.add_argument("--account", type=int, help="Account id (default: list every account)")
    sec_group = sec_parser.add_mutually_exclusive_group()
    sec_group.add_argument("--hide", metavar="SECTION", help="Stop using this section for --account")
    sec_group.add_argument("--show", metavar="SECTION", help="Use this section again for --account")
    ask_parser = subparsers.add_parser("ask", help='Ask about your email, e.g. "which students missed class today?"')
    ask_parser.add_argument("question", nargs="+")
    resort_parser = subparsers.add_parser("resort", help="Sort one account again from scratch (keeps your moves)")
    resort_parser.add_argument("account_id", type=int)

    # accuracy check
    label_parser = subparsers.add_parser("label", help="Hand-label a sample of your mail for the accuracy check")
    label_parser.add_argument("--n", type=int, default=100, help="Sample size (default 100)")
    subparsers.add_parser("accuracy", help="Score the classifier against your labels (moves nothing)")

    # section proposals
    subparsers.add_parser("propose", help="Let the model suggest new sections for unsorted mail")
    prop_parser = subparsers.add_parser("proposals", help="List pending section proposals, or accept/reject one")
    prop_group = prop_parser.add_mutually_exclusive_group()
    prop_group.add_argument("--accept", type=int, metavar="ID")
    prop_group.add_argument("--reject", type=int, metavar="ID")

    # calendar events
    ev_parser = subparsers.add_parser("events", help="Find events in recent mail: create confident ones, ask about the rest")
    ev_parser.add_argument("--dry-run", action="store_true", help="Only show what would happen")
    ev_parser.add_argument("--limit", type=int)
    evq_parser = subparsers.add_parser("event-questions", help="Events waiting for your answer; accept or reject one")
    evq_group = evq_parser.add_mutually_exclusive_group()
    evq_group.add_argument("--accept", type=int, metavar="ID")
    evq_group.add_argument("--reject", type=int, metavar="ID")

    # trash / reply / send: always ask you first; nothing automatic can reach these
    trash_parser = subparsers.add_parser("trash", help="Move an email to Gmail's Trash (asks first; undoable)")
    trash_parser.add_argument("message_id", type=int)
    reply_parser = subparsers.add_parser("reply", help="Reply to an email (shows it and asks before sending)")
    reply_parser.add_argument("message_id", type=int)
    reply_src = reply_parser.add_mutually_exclusive_group()
    reply_src.add_argument("--body", help="Reply text (otherwise an editor opens)")
    reply_src.add_argument("--ai-draft", nargs="?", const="", metavar="WHAT_TO_SAY",
                           help="Let the local model draft it, optionally saying what to answer")
    send_parser = subparsers.add_parser("send", help="Send a new email (shows it and asks before sending)")
    send_parser.add_argument("--account", type=int, default=1)
    send_parser.add_argument("--to", required=True)
    send_parser.add_argument("--subject", required=True)
    send_parser.add_argument("--body", help="Text (otherwise an editor opens)")

    # review and correct the sorting
    show_parser = subparsers.add_parser("show", help="Count per section, or list one section's messages")
    show_parser.add_argument("section", nargs="?", help="Section name (Unsorted = not moved anywhere)")
    show_parser.add_argument("--n", type=int, default=30, help="How many to list (default 30)")
    move_parser = subparsers.add_parser("move", help="Move a message to a section yourself (undoable)")
    move_parser.add_argument("message_id", type=int)
    move_parser.add_argument("section")
    history_parser = subparsers.add_parser("history", help="Recent actions with their ids")
    history_parser.add_argument("--n", type=int, default=20)
    undo_parser = subparsers.add_parser("undo", help="Undo the last action, a given action id, or every auto-move")
    undo_parser.add_argument("action_id", type=int, nargs="?")
    undo_parser.add_argument("--all-auto", action="store_true", help="Undo every classifier move (not yours)")

    # summary
    summary_parser = subparsers.add_parser("summary", help="Summarize mail, one summary per account")
    summary_parser.add_argument("--today", action="store_true", help="Today (local time); default is the last 24 hours")
    summary_parser.add_argument("--account", type=int, help="Only this account ID; default is every account")

    args = parser.parse_args(argv)

    db = init_db()

    if args.command == "add-account":
        account_id = add_account(db)
        print(f"Account added with id {account_id}. Next: python -m core.cli sync {account_id}")

    elif args.command == "add-applemail":
        from core.accounts import applemail
        if not args.name:
            accounts = applemail.mail_accounts()
            for name, addr in accounts:
                print(f"  {name!r:<30} {addr}")
            print("Add one: python -m core.cli add-applemail \"<name>\"" if accounts else
                  "No accounts in Apple Mail yet: System Settings > Internet Accounts > add your account, Mail on.")
        else:
            try:
                account_id = applemail.add_account(db, args.name)
                print(f"Added with id {account_id}. Next: python -m core.cli sync {account_id}")
            except ValueError as e:
                parser.error(str(e))

    elif args.command == "sync":
        print(f"Syncing account {args.account_id}...")
        from core.accounts import connector
        connector(db, args.account_id).sync()
        print("Sync complete.")

    elif args.command == "push-labels":
        from core.accounts import connector
        changes = connector(db, args.account_id).push_labels(dry_run=args.dry_run)
        for (have, want), n in sorted(changes.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>5}  {have or '(no label)'} -> {want or '(no label)'}")
        total = sum(changes.values())
        if args.dry_run:
            print(f"{total} emails would change. Run without --dry-run to apply.")
        else:
            print(f"Updated labels on {total} emails." if total else "Gmail already matches.")

    elif args.command == "classify":
        if args.all_new:
            placed = classify_all_new(db, args.limit)
            print("Placed:", ", ".join(f"{k} {v}" for k, v in placed.most_common()) or "nothing new")
        elif args.message_id is not None:
            result = classify_message(db, args.message_id)
            print(f"Message {args.message_id}: Section={result['section']}, Confidence={result['confidence']}")
        else:
            parser.error("classify needs a message_id or --all-new")

    elif args.command == "sections":
        from core import sections
        try:
            if args.hide or args.show:
                if not args.account:
                    parser.error("--hide/--show need --account")
                if args.hide:
                    n = sections.hide(db, args.account, args.hide)
                    print(f"{args.hide} hidden for account {args.account}; sorting its {n} email(s) again...")
                    placed = classify_all_new(db, account_id=args.account)
                    print("Placed:", ", ".join(f"{k} {v}" for k, v in placed.most_common()) or "nothing")
                else:
                    sections.show(db, args.account, args.show)
                    print(f"{args.show} is used again for account {args.account}.")
            for account_id, address in db.execute("SELECT id, address FROM accounts WHERE ? IS NULL OR id=?",
                                                  (args.account, args.account)).fetchall():
                used = [n for _, n, _ in sections.allowed(db, account_id)]
                print(f"  [{account_id}] {address}\n      uses:   {', '.join(used)}\n"
                      f"      hidden: {', '.join(sections.hidden(db, account_id)) or '-'}")
        except ValueError as e:
            parser.error(str(e))

    elif args.command == "ask":
        from core.ai import ask
        try:
            result = ask.ask(db, " ".join(args.question))
        except ValueError as e:
            parser.error(str(e))
        print(result["answer"])
        for e in result["emails"]:
            print(f"  #{e['id']}  {(e['sender'] or '')[:30]:<30} {(e['subject'] or '')[:60]}")
        print(f"({result['checked']} emails checked)")

    elif args.command == "resort":
        placed = classify.resort(db, args.account_id)
        print("Placed:", ", ".join(f"{k} {v}" for k, v in placed.most_common()) or "nothing")

    elif args.command == "label":
        evaluate.label(db, args.n)

    elif args.command == "accuracy":
        evaluate.accuracy(db)

    elif args.command == "propose":
        new_ids = propose.propose_sections(db)
        print(f"{len(new_ids)} new proposal(s)." + (" See: python -m core.cli proposals" if new_ids else ""))

    elif args.command == "proposals":
        try:
            if args.accept:
                name = propose.accept_proposal(db, args.accept)
                print(f"Created section {name}. Unsorted mail will be re-checked: python -m core.cli classify --all-new")
            elif args.reject:
                print(f"Rejected {propose.reject_proposal(db, args.reject)}; it won't be proposed again.")
            else:
                rows = propose.pending_proposals(db)
                for pid, name, desc, reason in rows:
                    print(f"  [{pid}] {name}: {desc}\n        {reason}")
                print("Accept: proposals --accept ID   Reject: proposals --reject ID" if rows else "No pending proposals.")
        except ValueError as e:
            parser.error(str(e))

    elif args.command == "events":
        results = events.check_events(db, apply=not args.dry_run, limit=args.limit)
        for msg_id, subject, decision, reason, ev in results:
            if decision != "skip":
                print(f"  {decision.upper():<6} {ev['title'][:40]:<40} {ev['start']:<16} #{msg_id} ({reason})")
        n = {d: sum(r[2] == d for r in results) for d in ("create", "ask", "skip")}
        verb = "would be" if args.dry_run else "were"
        print(f"Checked {len(results)} emails: {n['create']} events {verb} created, {n['ask']} questions, {n['skip']} no event.")

    elif args.command == "event-questions":
        try:
            if args.accept or args.reject:
                print(events.answer_pending(db, args.accept or args.reject, accept=bool(args.accept)))
            else:
                rows = events.pending_events(db)
                for pid, title, start, end, loc, reason, subject in rows:
                    print(f"  [{pid}] {title} | {start}{' - ' + end if end else ''}{' @ ' + loc if loc else ''}\n"
                          f"        why asking: {reason}; email: {(subject or '')[:50]}")
                print("Add: event-questions --accept ID   Skip: event-questions --reject ID" if rows else "No questions.")
        except (ValueError, actions.ActionError) as e:
            parser.error(str(e))

    elif args.command == "trash":
        row = db.execute("SELECT sender, subject FROM messages WHERE id=?", (args.message_id,)).fetchone()
        if not row:
            parser.error(f"No message with id {args.message_id}")
        print(f"From:    {row[0]}\nSubject: {row[1]}")
        if confirm("Move this email to Gmail's Trash?"):
            try:
                actions.trash_message(db, args.message_id, confirmed=True)
                print("In the Trash (Gmail keeps it 30 days). Undo: python -m core.cli undo")
            except actions.ActionError as e:
                parser.error(str(e))
        else:
            print("Nothing changed.")

    elif args.command in ("reply", "send"):
        try:
            if args.command == "reply":
                fields = compose.prefill(db, args.message_id, "reply")  # To, Subject, quoted original
                quote = fields["body"]
                if args.ai_draft is not None:
                    print("Drafting with the local model...")
                    text = compose.draft_reply(db, args.message_id, args.ai_draft)
                else:
                    text = args.body if args.body is not None else edit_text("")
            else:
                fields = {"account": args.account, "mode": "new", "to": args.to, "subject": args.subject}
                quote = ""
                text = args.body if args.body is not None else edit_text("")
            while True:
                fields["body"] = text + quote
                print(f"\n--- To: {compose.valid_recipients(fields['to'])}\n--- Subject: {fields['subject']}\n\n{text}\n")
                answer = confirm("Send this email? It can't be unsent.", allow_edit=True)
                if answer == "edit":
                    text = edit_text(text)
                    continue
                break
            if answer:
                actions.send_email(db, fields["account"], fields, confirmed=True)
                print("Sent.")
            else:
                print("Not sent.")
        except (ValueError, actions.ActionError) as e:
            parser.error(str(e))

    elif args.command == "show":
        if not args.section:
            for name, n in db.execute("""
                SELECT coalesce(s.name, 'Unsorted'), count(*) FROM messages m
                LEFT JOIN sections s ON s.id = m.section_id WHERE m.status IN ('new', 'classified')
                GROUP BY 1 ORDER BY 2 DESC"""):
                print(f"  {name:<10} {n}")
        else:
            rows = db.execute("""
                SELECT m.id, m.sender, m.subject,
                       (SELECT confidence FROM actions_log a WHERE a.message_id = m.id AND a.undone_at IS NULL
                        ORDER BY a.id DESC LIMIT 1)
                FROM messages m LEFT JOIN sections s ON s.id = m.section_id
                WHERE lower(coalesce(s.name, 'Unsorted')) = lower(?) AND m.status IN ('new', 'classified')
                ORDER BY m.date DESC LIMIT ?""",
                (args.section, args.n)).fetchall()
            for mid, sender, subject, conf in rows:
                tag = f"{conf:.2f}" if conf is not None else "  - "
                print(f"  {mid:>5} {tag}  {(sender or '')[:28]:<28} {(subject or '')[:60]}")
            print(f"Fix one: python -m core.cli move <id> <section>")

    elif args.command == "move":
        try:
            action_id = actions.move_message(db, args.message_id, args.section, "user_move")
            print(f"Moved message {args.message_id} to {args.section} (action {action_id}; undo: python -m core.cli undo)")
        except actions.ActionError as e:
            parser.error(str(e))

    elif args.command == "history":
        for aid, action, section, conf, undone, mid, subject in actions.history(db, args.n):
            conf_s = f"{conf:.2f}" if conf is not None else "  - "
            print(f"  {aid:>5} {'UNDONE ' if undone else ''}{action:<12} {section:<9} {conf_s}  #{mid} {(subject or '')[:50]}")

    elif args.command == "undo":
        try:
            if args.all_auto:
                ids = [r[0] for r in db.execute(
                    "SELECT id FROM actions_log WHERE action='move_section' AND undone_at IS NULL ORDER BY id DESC")]
                skipped = 0
                for aid in ids:
                    try:
                        actions.undo(db, aid)
                    except actions.ActionError:
                        skipped += 1  # you moved it yourself afterwards: your move stands
                print(f"Undid {len(ids) - skipped} classifier moves; kept {skipped} you had moved since.")
            else:
                print(actions.undo(db, args.action_id))
        except actions.ActionError as e:
            parser.error(str(e))

    elif args.command == "summary":
        kind = "daily" if args.today else "ondemand"
        if args.account:
            accounts = [(args.account, f"account {args.account}")]
        else:
            accounts = db.execute("SELECT id, address FROM accounts").fetchall()
        if not accounts:
            print("No accounts yet. Run: python -m core.cli add-account")
        for account_id, label in accounts:
            text = generate_summary(db, account_id, kind)
            print(f"--- Summary for {label} ---\n{text}\n----------------------------------------")

def confirm(question: str, allow_edit: bool = False):
    """True only if a person types y at a terminal. There is deliberately no --yes flag."""
    if not sys.stdin.isatty():
        print("Refusing: this needs you to confirm at a terminal.")
        return False
    options = "[y/N/e=edit]" if allow_edit else "[y/N]"
    answer = input(f"{question} {options} ").strip().lower()
    if allow_edit and answer == "e":
        return "edit"
    return answer == "y"


def edit_text(text: str) -> str:
    """Open the text in your terminal editor ($EDITOR, default nano) and return what you saved."""
    os.makedirs(os.path.join("data", "drafts"), exist_ok=True)
    path = os.path.join("data", "drafts", "draft.txt")  # data/ is gitignored
    with open(path, "w") as f:
        f.write(text)
    subprocess.run([os.environ.get("EDITOR", "nano"), path])
    with open(path) as f:
        return f.read().strip()


# Commands that load the model; it's freed when they finish (see LocalLLM.unload)
MODEL_COMMANDS = {"classify", "accuracy", "summary", "propose", "events", "reply", "resort", "sections", "ask"}


def run():
    try:
        main()
    except RuntimeError as e:  # e.g. Ollama not running: one clear line instead of a traceback
        sys.exit(f"Error: {e}")
    finally:
        if len(sys.argv) > 1 and sys.argv[1] in MODEL_COMMANDS:
            LocalLLM().unload()


if __name__ == "__main__":
    run()
