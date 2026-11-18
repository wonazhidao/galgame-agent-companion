from dataclasses import dataclass
from pathlib import Path
import os
import struct
import ctypes
import psutil
from ctypes import wintypes
from game_window import WNDENUMPROC, user32
from profiles import norm


@dataclass(frozen=True)
class Window:
    hwnd: int
    pid: int
    title: str
    exe: str


def windows(include_minimized=False):
    result = []
    @WNDENUMPROC
    def collect(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or (not include_minimized and user32.IsIconic(hwnd)):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            return True
        buf = ctypes.create_unicode_buffer(n+1)
        user32.GetWindowTextW(hwnd, buf, n+1)
        try:
            exe = psutil.Process(pid.value).exe()
        except (psutil.Error, OSError):
            return True
        result.append(Window(int(hwnd), pid.value, buf.value, exe))
        return True
    user32.EnumWindows(collect, 0)
    return result


def matches(profile, available):
    aliases = {norm(a) for a in profile["aliases"] if norm(a)}
    known_names = {s.casefold() for s in profile["executables"]}
    expected = profile.get("exe")
    ranked = []
    for win in available:
        path = Path(win.exe)
        if expected:
            if os.path.normcase(str(path)) == os.path.normcase(expected):
                ranked.append((4, win))
            continue
        if path.stem.casefold() in {"chrome","msedge","firefox","codex","explorer","windowsterminal",
                                    "powershell","pwsh","cmd","textractor","code","steam"}:
            continue
        title, stem = norm(win.title), norm(path.stem)
        title_hit = any(a == title or len(a) >= 2 and a in title for a in aliases)
        generic = path.stem.casefold() in {"game","bgi","bgi-chs","main","launcher","nw"}
        exe_hit = path.name.casefold() in known_names or stem in aliases
        parent_hit = any(a in norm(path.parent.name) for a in aliases)
        if exe_hit and (not generic or title_hit or parent_hit):
            ranked.append((3, win))
        elif title_hit or parent_hit:
            ranked.append((2 if title_hit and parent_hit else 1, win))
    if not ranked:
        return []
    top = max(score for score, _ in ranked)
    return [w for score,w in ranked if score == top]


def target_arch(exe):
    with open(exe, "rb") as f:
        if f.read(2) != b"MZ":
            return None
        f.seek(60)
        f.seek(struct.unpack("<I", f.read(4))[0])
        if f.read(4) != b"PE\0\0":
            return None
        return {0x14c:"x86", 0x8664:"x64"}.get(struct.unpack("<H", f.read(2))[0])
