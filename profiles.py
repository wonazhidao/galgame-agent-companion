"""Per-game profiles and isolated session locations."""
import hashlib
import json
import os
from pathlib import Path
import unicodedata

BASE = Path(__file__).resolve().parent
HOME = Path(os.getenv("VNCOMPANION_HOME") or BASE / "sessions").resolve()
BUILTINS = [
    {"aliases": ["素晴日", "美好的每一天", "素晴らしき日々", "Subarashiki Hibi", "Wonderful Everyday"],
     "executables": ["BGI-CHS.exe"], "language": "zh-Hans", "decoder": "subahibi"},
    {"aliases": ["天使们的约定", "天使たちの約束", "YAKUSOKU"], "executables": ["YAKUSOKU.EXE"]},
]


def norm(name):
    return "".join(c for c in unicodedata.normalize("NFKC", name).casefold() if c.isalnum())


def builtin(game):
    return next((p for p in BUILTINS if norm(game) in map(norm, p["aliases"])), {})


def folder(game):
    p = builtin(game)
    key = norm(p["aliases"][0] if p else game)
    if not key:
        raise ValueError("请输入游戏名称")
    return HOME / ("vn-" + key[:24] + "-" + hashlib.sha256(key.encode()).hexdigest()[:10])


def get_profile(game):
    saved = folder(game) / "profile.json"
    value = dict(language="auto", region=[0.03, 0.60, 0.97, 0.96], mode="auto",
                 aliases=[game], executables=[], decoder="plain")
    value.update(builtin(game))
    if saved.exists():
        value.update(json.loads(saved.read_text("utf8")))
    return value


def parse_region(raw):
    v = list(map(float, raw.split(",")))
    if len(v) != 4 or not (0 <= v[0] < v[2] <= 1 and 0 <= v[1] < v[3] <= 1):
        raise ValueError("字幕区使用 0 到 1 的四个坐标，如 0.03,0.60,0.97,0.96")
    return v
