"""Short window of game-window samples and chronological, dialogue-labelled contact sheets."""
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import math
import re
import threading
import time

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat
import companion_log as log

WINDOW_SECONDS = 6
SAMPLE_SECONDS = 0.20
NAME_RE = re.compile(r"_(\d{2})(\d{2})(\d{2})(?:_(\d{6})_s(\d+))?\.png$")


@dataclass
class Frame:
    at: float
    sampled_at: float
    seq: int
    image: Image.Image
    changed: bool

    @property
    def name(self):
        stamp = datetime.fromtimestamp(self.at).strftime("%Y%m%d_%H%M%S_%f")
        return f"shot_{stamp}_s{self.seq:08d}.png"


def label(path):
    match = NAME_RE.search(Path(path).name)
    if not match:
        return Path(path).stem
    h, m, s, micro, seq = match.groups()
    return f"{h}:{m}:{s}" + (f".{micro[:3]}  #{int(seq)}" if micro else "")


def shot_seq(path):
    match = NAME_RE.search(Path(path).name)
    return int(match[5]) if match and match[5] else None


def make_sheet(items, dest):
    cols = min(len(items), 3)
    w, h = 480, 300
    sheet = Image.new("RGB", (cols*w, math.ceil(len(items)/cols)*h), "#131722")
    draw = ImageDraw.Draw(sheet)
    for i, (caption, img) in enumerate(items):
        x, y = (i % cols)*w, (i // cols)*h
        tile = ImageOps.contain(img, (w-4, h-22), Image.Resampling.LANCZOS)
        sheet.paste(tile, (x+(w-tile.width)//2, y))
        draw.text((x+6, y+h-18), caption, fill="white")
    tmp = dest.with_suffix(".tmp")
    sheet.save(tmp, "JPEG", quality=87)
    tmp.replace(dest)


def build_montage(paths):
    if not paths:
        return None, 0
    items = []
    first_label = None
    for path in paths:
        with Image.open(path) as im:
            frame = im.convert("RGB")
        caption = label(path)
        if items:
            previous = items[-1][1]
            if frame.size == previous.size and ImageChops.difference(previous, frame).getbbox() is None:
                items[-1] = (f"{first_label} - {caption}", previous)
                continue
        first_label = caption
        items.append((caption, frame))
    dest = log.LOG_DIR / "montage_latest.jpg"
    make_sheet(items, dest)
    return str(dest), len(items)


def significant(before, after):
    diff = ImageChops.difference(before, after)
    if sum(ImageStat.Stat(diff).mean) / 3 >= 7.5:
        return True
    # A small menu, expression or textbox can change only part of the window.
    return max(hi for _, hi in diff.resize((8, 6), Image.Resampling.BOX).getextrema()) >= 24


class SceneBuffer:
    def __init__(self):
        self.frames = deque(maxlen=1 + int(WINDOW_SECONDS / SAMPLE_SECONDS))
        self.lock = threading.Lock()
        self.previous = None
        self.after_change = False
        self.last_saved = ""
        self.last_cleanup = 0.0

    def sample(self, img, seq, at):
        img = img.convert("RGB")
        img.thumbnail((960, 720))
        thumb = img.resize((96, 72))
        changed = self.previous is None or significant(self.previous, thumb)
        sampled_at = time.monotonic()
        frame = Frame(at, sampled_at, seq, img, changed)
        with self.lock:
            while self.frames and sampled_at-self.frames[0].sampled_at > WINDOW_SECONDS:
                self.frames.popleft()
            prev = self.frames[-1] if self.frames else None
            self.frames.append(frame)
        save = []
        if changed and prev:
            save.append(prev)
        if changed or self.after_change:
            save.append(frame)
        self.after_change = changed
        if changed:
            self.previous = thumb
        saved = []
        for item in save:
            if item.name <= self.last_saved:
                continue
            path = log.LOG_DIR / item.name
            tmp = path.with_suffix(".tmp")
            item.image.save(tmp, "PNG", compress_level=2)
            tmp.replace(path)
            self.last_saved = item.name
            saved.append(path)
        if saved and at-self.last_cleanup >= 30:
            acknowledged = log.read_json(log.CURSOR, {}).get("last_shot", "")
            old = sorted(p for p in log.LOG_DIR.glob("shot_*.png") if p.name <= acknowledged)
            for path in old[:-40]:
                path.unlink(missing_ok=True)
            self.last_cleanup = at
        return saved

    def recent(self):
        with self.lock:
            now = time.monotonic()
            return [f for f in self.frames if now-f.sampled_at <= WINDOW_SECONDS]

    def save_review(self, frames):
        if not frames:
            return None
        chosen = {0, len(frames)-1}
        changes = [i for i, f in enumerate(frames) if f.changed and i]
        if len(changes) > 7:
            changes = [changes[round(j*(len(changes)-1)/6)] for j in range(7)]
        chosen.update(changes)
        for i in changes:
            for k in (i-1, i+1):
                if len(chosen) < 9 and 0 <= k < len(frames):
                    chosen.add(k)
        dest = log.LOG_DIR / ("review_" + frames[-1].name[5:-4] + ".jpg")
        make_sheet([(label(frames[i].name), frames[i].image) for i in sorted(chosen)], dest)
        return dest
