"""Incremental delivery of a game's saved history, independent of the live text cursor."""
from collections import Counter, deque
from contextlib import closing
from difflib import SequenceMatcher
import json
from datetime import datetime
import sqlite3

from bgi_history import read_save


def history_delta(previous, current):
    """Match playback records, including repeated text and a capacity-limited sliding window."""
    old = [r["key"] for r in previous]
    new = [r["key"] for r in current]
    if old == new:
        return "", []
    if old and new[:len(old)] == old:
        return "新增", current[len(old):]
    overlap = [k for k in range(1, min(len(old), len(new)) + 1) if old[-k:] == new[:k]]
    if len(overlap) == 1:
        return "新增", current[overlap[0]:]
    if overlap:
        return "重复锚点有歧义，已重新定位", []
    match = SequenceMatcher(None, old, new, autojunk=False).find_longest_match()
    if match.size and (match.a == 0 or match.b == 0):
        anchor = old[match.a:match.a + match.size]
        if all(sum(seq[i:i+len(anchor)] == anchor for i in range(len(seq)-len(anchor)+1)) == 1
               for seq in (old, new)):
            end = match.b + match.size
            if end == len(new):
                return "存档回到较早位置", []
            return "存档轨迹发生变化，以下为分歧后的记录", current[end:]
    return "无共同锚点，已重新定位；可查询这个存档的最近原文", []


def display(row):
    name = f"【{row['speaker']}】" if row["speaker"] else ""
    return name + row["text"]


def live_references(rows, live):
    """Only reference uniquely placed occurrences between two distinctive anchors."""
    if not 2 <= len(live) <= min(400, len(rows)):
        return {}
    forms = [{row["text"], display(row)} for row in rows]

    def placements(step):
        pos = 0 if step == 1 else len(rows) - 1
        result = []
        for _, line in (live if step == 1 else reversed(live)):
            while 0 <= pos < len(rows) and line not in forms[pos]:
                pos += step
            if not 0 <= pos < len(rows):
                return []
            result.append(pos)
            pos += step
        return result if step == 1 else result[::-1]

    # A missing save-only sentence may sit between any two live records.
    # Leftmost and rightmost placements agree exactly where every alignment agrees.
    matched = [(i, seq, line) for i, j, (seq, line) in zip(placements(1), placements(-1), live)
               if i == j]
    counts = Counter(form for choices in forms for form in choices)
    anchors = [i for i, _, line in matched if counts[line] == 1
               and sum(c.isalnum() for c in rows[i]["text"]) >= 4]
    if len(anchors) < 2:
        return {}
    return {i: (seq, rows[i]["speaker"] if line != display(rows[i]) else "")
            for i, seq, line in matched if anchors[0] <= i <= anchors[-1]}


class SaveHistory:
    def __init__(self, directory, database):
        self.directory = directory
        self.database = database
        # A reconnect, backlog overflow or multiple pending slots loses provenance.
        self.live = deque(maxlen=401)
        self.observed_baseline = False
        with closing(sqlite3.connect(database)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, line TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS event_refs (event_id INTEGER PRIMARY KEY, seq INTEGER NOT NULL,
                                                       speaker TEXT NOT NULL);
            """)
            row = db.execute("SELECT data FROM snapshot WHERE id=1").fetchone()
        self.state = json.loads(row[0]) if row else {}
        if self.state.get("directory") != str(directory):
            self.state = {}
        self.message = self.state.get("message", "等待游戏写入存档")
        self.last_slot = self.state.get("slot", "")

    def poll(self):
        files = []
        for path in self.directory.glob("SubarashikihibiHD[0-9][0-9][0-9].sud"):
            stat = path.stat()
            files.append((stat.st_mtime_ns, path.name, stat.st_size))
        files.sort()
        if not files:
            return False
        known = self.state.get("files", {})
        pending = [f for f in files if known.get(f[1]) != [f[0], f[2]]]
        first = not self.state
        if first:
            pending = files[-1:]
        changed = False
        for index, (stamp, name, size) in enumerate(pending):
            path = self.directory / name
            try:
                rows = read_save(path)
                after = path.stat()
                if (after.st_mtime_ns, after.st_size) != (stamp, size):
                    raise ValueError("存档写入中")
            except (ValueError, OSError):
                if index + 1 == len(pending):
                    raise
                continue  # A partial slot must not block a newer complete save.
            if first:
                reason, delta = "已记录初始存档位置", []
                known = {n: [s, z] for s, n, z in files}
            else:
                reason, delta = history_delta(self.state["rows"], rows)
                known = {**known, name: [stamp, size]}
            message = reason or "与上次存档的回看相同"
            refs = {}
            if (reason == "新增" and self.observed_baseline and len(pending) == 1
                    and stamp > self.state.get("saved_ns", 0)):
                refs = live_references(delta, self.live)
            state = dict(directory=str(self.directory), files=known, slot=name, rows=rows,
                         saved_ns=stamp, message=message)
            # Commit the file cursor and delivered records together so a retry cannot duplicate them.
            with closing(sqlite3.connect(self.database)) as db, db:
                if reason and not first and (delta or reason != "新增"):
                    db.execute("INSERT INTO events(line) VALUES (?)", (f"〔{name} · {reason}〕",))
                    for i, row in enumerate(delta):
                        event = db.execute("INSERT INTO events(line) VALUES (?)", (display(row),))
                        if i in refs:
                            db.execute("INSERT INTO event_refs VALUES (?, ?, ?)", (event.lastrowid, *refs[i]))
                    changed = True
                db.execute("INSERT OR REPLACE INTO snapshot VALUES (1, ?)", (json.dumps(state, ensure_ascii=False),))
            self.state, self.message, self.last_slot = state, message, name
            self.live.clear()
        self.observed_baseline = True
        return changed


def recent(database, limit):
    if not database.exists():
        return "本游戏尚无可读的存档历史"
    with closing(sqlite3.connect(database)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshot'").fetchone():
            return "正在准备存档回看"
        row = db.execute("SELECT data FROM snapshot WHERE id=1").fetchone()
    if row is None:
        return "等待游戏写入首个可读存档"
    state = json.loads(row[0])
    saved = datetime.fromtimestamp(state["saved_ns"] / 1e9).strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"存档回看 · {state['slot']} · {saved} 写入 · 共 {len(state['rows'])} 条，末尾最多 {limit} 条"]
    return "\n".join(lines + [display(r) for r in state["rows"][-limit:]])


def history_page(database, start, limit, live_end=None):
    if not database.exists():
        return [], start, False
    with closing(sqlite3.connect(database)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone():
            return [], start, False
        query = "SELECT id, line, NULL, NULL FROM events WHERE id>? ORDER BY id"
        if live_end is not None and db.execute("SELECT 1 FROM sqlite_master WHERE name='event_refs'").fetchone():
            query = ("SELECT id, line, seq, speaker FROM events LEFT JOIN event_refs ON event_id=id "
                     "WHERE id>? ORDER BY id")
        lines, size, end = [], 0, start
        more = False
        for number, line, seq, speaker in db.execute(query, (start,)):
            if end != start and size + len(line) + 1 > limit:
                more = True
                break
            if seq is not None and seq <= live_end:
                if lines and isinstance(lines[-1], list) and lines[-1][1:] == [seq - 1, speaker]:
                    lines[-1][1] = seq
                else:
                    lines.append([seq, seq, speaker])
            else:
                lines.append(line)
            size += len(line) + 1
            end = number
        return [line if isinstance(line, str) else reference_text(*line) for line in lines], end, more


def reference_text(first, last, speaker):
    ids = f"#{first}" if first == last else f"#{first}–#{last}"
    return ids + (f" 存档署名【{speaker}】" if speaker else " 与存档一致")
