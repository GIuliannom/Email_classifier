"""Resource check (plan Day 6-7): idle footprint, peak RAM and time to summarize 50 emails,
classification speed. Works on an in-memory copy of the database: nothing is saved.
Run: python -m core.measure   (Ollama must be running)
"""
import sqlite3
import subprocess
import threading
import time

from core.ai.classify import classify_message
from core.ai.llm import LocalLLM
from core.ai.summarize import generate_summary


def _pid(pattern: str):
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
    return out[0] if out else None


def footprint_mb(pid) -> float:
    """macOS 'physical footprint' (what Activity Monitor calls Memory), in MB."""
    if not pid:
        return 0.0
    out = subprocess.run(["vmmap", "--summary", pid], capture_output=True, text=True).stdout
    line = next((l for l in out.splitlines() if l.startswith("Physical footprint:")), "")
    value = line.split(":")[1].strip() if line else "0K"
    return float(value[:-1]) * {"K": 1 / 1024, "M": 1, "G": 1024}.get(value[-1], 0)


def cpu_seconds(pid) -> float:
    if not pid:
        return 0.0
    t = subprocess.run(["ps", "-o", "time=", "-p", pid], capture_output=True, text=True).stdout.strip()
    parts = [float(p) for p in t.replace("-", ":").split(":")] if t else [0]
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))


class PeakSampler:
    """Samples the model process's footprint every second while a block runs."""
    def __enter__(self):
        self.peak, self.cpu, self._stop = 0.0, 0.0, threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            pid = _pid("llama-server")
            self.peak = max(self.peak, footprint_mb(pid))
            self.cpu = max(self.cpu, cpu_seconds(pid))  # the runner starts fresh after an unload
            self._stop.wait(1)

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()


def main():
    disk = sqlite3.connect("data/mail.db")
    db = sqlite3.connect(":memory:")
    disk.backup(db)  # measure on a copy: the summary rows below are thrown away
    disk.close()
    llm = LocalLLM()
    llm.unload()
    time.sleep(3)

    rows = [("Engine (python core.server), idle", f"{footprint_mb(_pid('core.server')):.0f} MB"
             if _pid("core.server") else "not running"),
            ("Ollama server (always on)", f"{footprint_mb(_pid('ollama serve') or _pid('Ollama.app/Contents/Resources/ollama')):.0f} MB"),
            ("Model process when idle", "not loaded" if not _pid("llama-server") else f"{footprint_mb(_pid('llama-server')):.0f} MB")]

    account_id, = db.execute("SELECT id FROM accounts LIMIT 1").fetchone()
    dates = [d for (d,) in db.execute("SELECT date FROM messages WHERE account_id=? AND status != 'trashed' "
                                      "ORDER BY date DESC LIMIT 50", (account_id,))]
    with PeakSampler() as s:
        t0 = time.time()
        generate_summary(db, account_id, "ondemand", (dates[-1], "9999"))
        took = time.time() - t0
    rows += [(f"Summarize {len(dates)} emails: time", f"{took:.0f} s   (target < 120 s)"),
             ("Summarize: peak model RAM", f"{s.peak / 1024:.1f} GB   (target < 4 GB)"),
             ("Summarize: model CPU", f"{s.cpu:.0f} s CPU in {took:.0f} s = {s.cpu / took:.1f} cores busy on average")]

    ids = [i for (i,) in db.execute("SELECT id FROM messages ORDER BY date DESC LIMIT 20")]
    with PeakSampler() as s:
        t0 = time.time()
        for i in ids:
            classify_message(db, i, apply=False)
        per = (time.time() - t0) / len(ids)
    rows += [("Classify: time per email", f"{per:.2f} s"),
             ("Classify 20: peak model RAM", f"{s.peak / 1024:.1f} GB")]

    llm.unload()
    time.sleep(3)
    rows.append(("Model process after unload", "not loaded" if not _pid("llama-server") else "still loaded"))
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        print(f"  {k:<{width}}  {v}")


if __name__ == "__main__":
    main()
