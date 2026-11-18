"""按批交付尚未确认的台词、消息和画面。"""
import base64
import json
import os
from pathlib import Path
import re
import sys
import uuid
from scene_replay import build_montage, shot_seq
from save_history import history_page
import companion_log as log

MAX_DELTA_CHARS = 3200  # Keep ordinary CJK pages small enough for an Agent tool response.


REVIEW_NAME = re.compile(r"review_\d{8}_\d{6}_\d{6}_s\d{8,}\.jpg")


def review_names(notes):
    prefix = f"回看拼图 {log.LOG_DIR}{os.sep}"
    return [line[len(prefix):] for line in notes
            if line.startswith(prefix) and REVIEW_NAME.fullmatch(line[len(prefix):])]


def deliver(snapshot, as_json):
    output = snapshot["output"]
    if not as_json:
        print(output, flush=True)
        return
    names = snapshot.get("images")
    if names is None:  # A batch issued by an earlier CLI can still be pending.
        lines = output.splitlines()
        names = review_names(lines)
        if len(lines) > 2 and lines[2].startswith(f"MONTAGE: {log.LOG_DIR / 'montage_latest.jpg'} ("):
            names.insert(0, "montage_latest.jpg")
    images = []
    for name in dict.fromkeys(names):
        if name != "montage_latest.jpg" and not REVIEW_NAME.fullmatch(name):
            continue
        path = log.LOG_DIR / name
        if path.resolve().parent == log.LOG_DIR and path.is_file():
            images.append(dict(name=name, data=base64.b64encode(path.read_bytes()).decode("ascii")))
        else:
            output += f"\n本批画面文件已不可用：{name}"
    print(json.dumps(dict(batch=snapshot.get("batch"), text=output, images=images), ensure_ascii=False))


def run_check(as_json=False):
    with log.delivery_lock():
        cur = log.load_cursor()
        pending = log.read_json(log.SNAPSHOT, {})
        if pending.get("batch") and pending["batch"] != cur.get("batch"):
            deliver(pending, as_json)
            return
        trigger = log.read_json(log.TRIGGER, {})
        seq = int(trigger.get("seq", cur["trigger_seq"]))
        lines, end, number, more = log.read_page(log.TEXT, cur["textlog_size"], MAX_DELTA_CHARS, cur["text_seq"])
        notes, nend, _, more_notes = log.read_page(log.NOTES, cur["notes_size"], MAX_DELTA_CHARS)
        hstart = cur.get("history_seq", 0)
        history, hend, more_history = history_page(log.HISTORY, hstart, MAX_DELTA_CHARS, live_end=number)
        shots = sorted(p for p in log.LOG_DIR.glob("shot_*.png") if p.name > cur["last_shot"])
        # Stop at the first newer scene so the image cursor stays chronological.
        page = []
        for path in shots[:9]:
            frame_seq = shot_seq(path)
            if frame_seq is not None and frame_seq > number:
                break
            page.append(path)
        montage, count = build_montage(page)
        batch = uuid.uuid4().hex
        after = dict(textlog_size=end, text_seq=number, notes_size=nend,
                     last_shot=page[-1].name if page else cur["last_shot"],
                     trigger_seq=seq, history_seq=hend,
                     more=bool(more or more_notes or more_history or len(shots)>len(page)), batch=batch)
        output = [f"TRIGGER: seq={seq} reason={trigger.get('reason', 'manual_check')}",
                  f"已确认 #{cur['text_seq']}，本批读至 #{number}"]
        if montage:
            output.append(f"MONTAGE: {montage} ({count} 格，图中 # 为拍摄时已采集的台词编号，可按号回查)")
        if notes:
            output.extend(["玩家消息", *notes])
        if lines:
            output.extend(["期间新增游戏台词", *lines])
        if history:
            output.extend(["存档回看（按存档顺序，# 引用实时台词）", *history])
        if page or history or end != cur["textlog_size"] or nend != cur["notes_size"] or seq > cur["trigger_seq"]:
            output.append(f"BATCH: {batch}")
            if after["more"]:
                output.append("还有未读内容，确认本批后继续运行 wait_companion.py。")
            rendered = "\n".join(output)
            images = ([Path(montage).name] if montage else []) + review_names(notes)
            snapshot = dict(batch=batch, cursor=after, output=rendered, images=images,
                            reply_start=log.REPLIES.stat().st_size if log.REPLIES.exists() else 0)
            log.atomic_json(log.SNAPSHOT, snapshot)
        else:
            snapshot = dict(output="\n".join(output + ["暂无新内容。"]), images=[])
        deliver(snapshot, as_json)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    run_check()
