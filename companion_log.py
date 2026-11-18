"""Shared on-disk record numbering and atomic state for the companion and Agent CLI."""
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
import msvcrt

BASE = Path(__file__).resolve().parent
LOG_DIR = Path(os.environ.get("VNCOMPANION_SESSION", BASE / "sessions" / "manual")).resolve()
TEXT = LOG_DIR / "textlog.txt"
NOTES = LOG_DIR / "player_notes.txt"
CURSOR = LOG_DIR / "ai_cursor.json"
SNAPSHOT = LOG_DIR / "ai_reading_snapshot.json"
TRIGGER = LOG_DIR / "trigger.json"
REPLIES = LOG_DIR / "ai_replies.jsonl"
HISTORY = LOG_DIR / "save_history.sqlite3"


def read_json(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return default


def atomic_json(path, data):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            f.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        # Windows readers may briefly hold the destination without delete sharing.
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                break
            except PermissionError as exc:
                if exc.winerror not in (5, 32, 33) or attempt == 5:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def delivery_lock():
    LOG_DIR.mkdir(exist_ok=True)
    with (LOG_DIR / "delivery.lock").open("a+b") as f:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def dialogue_record(raw):
    return bool(raw.strip()) and not raw.startswith(b"===")


def count_records(end=None):
    if not TEXT.exists():
        return 0
    with TEXT.open("rb") as f:
        data = f.read() if end is None else f.read(end)
    return sum(dialogue_record(row) for row in data.split(b"\n")[:-1])


def dialogue_range(first, last):
    """Return complete records with stable IDs, including records in legacy logs."""
    if first > last or last < 1 or not TEXT.exists():
        return []
    result = []
    with TEXT.open("rb") as f:
        n = 0
        for raw in f:
            if not raw.endswith(b"\n") or not dialogue_record(raw):
                continue
            n += 1
            if n >= first:
                result.append((n, raw.decode("utf-8", "replace").rstrip("\r\n")))
            if n >= last:
                break
    return result


def load_cursor():
    cur = {"textlog_size": 0, "notes_size": 0, "last_shot": "", "trigger_seq": 0}
    cur.update(read_json(CURSOR, {}))
    if "text_seq" not in cur:
        cur["text_seq"] = count_records(cur["textlog_size"])
    return cur


def read_page(path, start, limit, seq=None):
    """Finish whole UTF-8 records, even if a single record exceeds the soft limit."""
    lines, size, end = [], 0, start
    if path.exists():
        with path.open("rb") as f:
            f.seek(start)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if seq is not None and dialogue_record(raw):
                    if f"[#{seq + 1}] " not in line:
                        line = f"#{seq + 1} {line}"
                if lines and size + len(line) + 1 > limit:
                    return lines, end, seq, True
                end += len(raw)
                if seq is not None:
                    if not dialogue_record(raw):
                        continue
                    seq += 1
                lines.append(line)
                size += len(line) + 1
    return lines, end, seq, False
