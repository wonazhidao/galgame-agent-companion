"""Wake for a new trigger, player message or the next page of an active delivery."""
import sys
import time
import companion_log as log
from check_delta import run_check
from save_history import history_page


def ready():
    cur = log.read_json(log.CURSOR, {})
    if cur.get("more") or log.read_json(log.TRIGGER, {}).get("seq", 0) > cur.get("trigger_seq", 0):
        return True
    if history_page(log.HISTORY, cur.get("history_seq", 0), 1)[0]:
        return True
    snap = log.read_json(log.SNAPSHOT, {})
    return bool(snap.get("batch") and snap["batch"] != cur.get("batch") or
                log.NOTES.exists() and log.NOTES.stat().st_size > cur.get("notes_size", 0))


def main():
    while not ready():
        time.sleep(0.35)
    run_check()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
