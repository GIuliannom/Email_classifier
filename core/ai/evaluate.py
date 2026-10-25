"""Accuracy check: hand-label a fixed sample of your mail, then score the classifier on it."""
import csv
import os
import random
import sqlite3
from collections import Counter

from core.ai.classify import classify_message, CONFIDENCE_THRESHOLD, LABELS_PATH, read_labels, is_example


def _sections(db: sqlite3.Connection) -> list:
    return [name for (name,) in db.execute(
        "SELECT name FROM sections WHERE approved=1 ORDER BY name='Unsorted', id")]  # Unsorted last


def label(db: sqlite3.Connection, n: int = 100, path: str = LABELS_PATH, seed: int = 0):
    """Show emails one by one and record your section for each. Safe to quit and resume."""
    sections = _sections(db)
    by_lower = {s.lower(): s for s in sections}
    ids = [r[0] for r in db.execute("SELECT id FROM messages ORDER BY id")]
    # fixed seed: the same sample every time, so a resumed session continues where it stopped
    sample = random.Random(seed).sample(ids, min(n, len(ids)))
    done = read_labels(path)
    todo = [i for i in sample if i not in done]
    descriptions = dict(db.execute("SELECT name, description FROM sections"))
    keys = " ".join(f"{i} {s}" for i, s in enumerate(sections, 1))
    print(f"{len(sample) - len(todo)}/{len(sample)} already labeled.\nSections:")
    for i, s in enumerate(sections, 1):
        print(f"  {i} {s:<9} {descriptions.get(s) or ''}")
    print("Type a number, or a new section name (e.g. Travel) | s skip | q quit")

    new_file = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["message_id", "label"])
        for k, msg_id in enumerate(todo, len(sample) - len(todo) + 1):
            sender, subject, body = db.execute(
                "SELECT sender, subject, body_text FROM messages WHERE id=?", (msg_id,)).fetchone()
            print(f"\n[{k}/{len(sample)}] From: {sender}\nSubject: {subject}\n{(body or '')[:300]}")
            while True:
                ans = input(f"{keys} > ").strip()
                if ans == "q":
                    return
                if ans == "s":
                    break
                if ans.isdigit() and 1 <= int(ans) <= len(sections):
                    lab = sections[int(ans) - 1]
                elif ans.replace(" ", "").isalpha():  # a new section name; rejects typos like "Ads803" or stray keys
                    lab = by_lower.get(ans.lower(), ans)
                else:
                    continue
                w.writerow([msg_id, lab])
                f.flush()
                break
    print(f"\nDone. Next: python -m core.cli accuracy")


def accuracy(db: sqlite3.Connection, path: str = LABELS_PATH, threshold: float = CONFIDENCE_THRESHOLD) -> dict:
    """Run the classifier (no writes) on the held-out labeled emails and compare with your labels."""
    all_labels = read_labels(path)
    if not all_labels:
        print(f"No labels in {path}. Run: python -m core.cli label")
        return {}
    # Emails used as prompt examples would score themselves: only the held-out half counts
    labels = {k: v for k, v in all_labels.items() if not is_example(k)}
    print(f"Scoring {len(labels)} held-out emails ({len(all_labels) - len(labels)} others are example candidates).")
    sections = set(_sections(db))
    rows = []
    for i, (msg_id, lab) in enumerate(labels.items(), 1):
        # A label that isn't a section yet can only be right if it stays in Unsorted
        expected = lab if lab in sections else "Unsorted"
        r = classify_message(db, msg_id, apply=False, labels_path=path)
        placed = r["section"] if r["section"] != "Unsorted" and r["confidence"] >= threshold else "Unsorted"
        subject = db.execute("SELECT subject FROM messages WHERE id=?", (msg_id,)).fetchone()[0]
        rows.append({"subject": subject or "", "label": lab, "expected": expected,
                     "model": r["section"], "confidence": r["confidence"], "placed": placed})
        if i % 10 == 0:
            print(f"  {i}/{len(labels)} scored")

    n = len(rows)
    correct = sum(r["placed"] == r["expected"] for r in rows)
    moved = [r for r in rows if r["placed"] != "Unsorted"]
    wrong_moves = [r for r in moved if r["placed"] != r["expected"]]

    print(f"\nPlacement accuracy: {correct}/{n} = {correct / n:.0%}   (week-1 target 85%)")
    print(f"Model's first choice right (ignoring the {threshold} threshold): "
          f"{sum(r['model'] == r['expected'] for r in rows)}/{n}")
    print(f"Auto-moved: {len(moved)}   wrong auto-moves: {len(wrong_moves)}   (target: rare)")

    # Same predictions at other thresholds: pick the lowest one whose wrong moves you can live with
    print("\nThreshold  accuracy  auto-moved  wrong moves")
    for t in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95):
        placed_t = [r["model"] if r["model"] != "Unsorted" and r["confidence"] >= t else "Unsorted" for r in rows]
        moved_t = [(p, r) for p, r in zip(placed_t, rows) if p != "Unsorted"]
        print(f"  {t:<9} {sum(p == r['expected'] for p, r in zip(placed_t, rows)) / n:>7.0%}  {len(moved_t):>10}"
              f"  {sum(p != r['expected'] for p, r in moved_t):>11}")

    print("\nPer section (your label -> placed correctly):")
    for sec in sorted({r["expected"] for r in rows}):
        group = [r for r in rows if r["expected"] == sec]
        print(f"  {sec:<10} {sum(r['placed'] == sec for r in group)}/{len(group)}")

    misses = [r for r in rows if r["placed"] != r["expected"]]
    if misses:
        print("\nMisses (subject | you | model, confidence):")
        for r in misses:
            print(f"  {r['subject'][:55]:<55} | {r['label']:<10} | {r['model']} {r['confidence']:.2f}")

    ideas = Counter(r["label"] for r in rows if r["label"] not in sections)
    if ideas:
        print("\nSections you used that don't exist yet:", ", ".join(f"{k} ({v})" for k, v in ideas.most_common()))

    return {"n": n, "correct": correct, "moved": len(moved), "wrong_moves": len(wrong_moves)}
