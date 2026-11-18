"""Post the reply and acknowledge exactly the outstanding delivery."""
import argparse
import json
import os
import sys
from datetime import datetime
import companion_log as log


def send(title, body, batch=None):
    quiet = title is None and body is None
    if quiet and not batch:
        raise ValueError("请指定已读批号")
    with log.delivery_lock():
        cur = log.load_cursor()
        snap = log.read_json(log.SNAPSHOT, {})
        if batch and batch == cur.get("batch"):
            return cur
        pending = snap.get("batch") and snap["batch"] != cur.get("batch")
        if batch and (not pending or batch != snap["batch"]):
            raise ValueError("批次不匹配，请重新读取待确认批次。")
        target = snap["cursor"] if pending else cur
        # A crash after the reply append and before cursor replacement must be retryable.
        seen = False
        if not quiet and pending and log.REPLIES.exists():
            with log.REPLIES.open("rb") as f:
                f.seek(snap.get("reply_start", 0))
                for row in f:
                    if snap["batch"].encode() in row:
                        try:
                            seen = json.loads(row).get("batch") == snap["batch"]
                        except ValueError:
                            continue
                        if seen:
                            break
        if not quiet and not seen:
            reply = dict(role="ai", time=datetime.now().strftime("%H:%M"), title=title,
                         body=body.replace("\\n", "\n"), ack_seq=target["trigger_seq"])
            if pending:
                reply["batch"] = snap["batch"]
            with log.REPLIES.open("a+b") as f:
                if f.tell():
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        f.write(b"\n")
                f.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
        if pending:
            log.atomic_json(log.CURSOR, target)
        return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--batch")
    args = parser.parse_args()
    print("OK ->", send(args.title, args.body, args.batch))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
