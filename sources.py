"""Text from the official Textractor CLI and the Windows OCR engine."""
import tempfile
import json
from collections import deque
import io
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import psutil

from profiles import BASE, norm
from targets import target_arch

HEADER = re.compile(r"^\[([0-9a-f]+):([0-9a-f]+):[0-9a-f]+:[0-9a-f]+:[0-9a-f]+:([^:]*):(.*?)] (.*)$", re.I)


def cli_path(exe, profile):
    arch = target_arch(exe)
    if not arch:
        return None
    roots = [profile.get("textractor"), os.getenv("TEXTRACTOR_HOME"), BASE / "tools" / "Textractor",
             Path.home() / "Desktop" / "Textractor", Path(os.getenv("ProgramFiles", "C:/Program Files")) / "Textractor"]
    for root in filter(None, roots):
        for p in (Path(root)/arch/"TextractorCLI.exe", Path(root)/"TextractorCLI.exe"):
            if p.is_file() and target_arch(p) == arch:
                return p
    return None


def extractor_busy():
    return any((p.info["name"] or "").casefold() in ("textractor.exe", "textractorcli.exe")
               for p in psutil.process_iter(["name"], ad_value=""))


class Native:
    def __init__(self, cli, pid, cwd):
        self.pid = pid
        self.events = queue.Queue()
        self.p = subprocess.Popen([str(cli)], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.command("attach")
        except OSError:
            self.close()
            raise

    def command(self, verb):
        self.p.stdin.write(f"{verb} -P{self.pid}\r\n".encode("utf-16le"))
        self.p.stdin.flush()

    def _read(self):
        last = None
        with io.TextIOWrapper(self.p.stdout, encoding="utf-16le", errors="replace") as lines:
            for line in lines:
                line = line.rstrip("\r\n")
                m = HEADER.match(line)
                if m:
                    key, pid, name, code, line = m.groups()
                    last = (key, name, code) if int(pid, 16) == self.pid else None
                if last and line.strip():
                    self.events.put((*last, line))

    def close(self):
        try:
            if self.p.poll() is None:
                self.command("detach")
        except OSError:
            pass
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(timeout=2)
        self.reader.join(timeout=1)


class Candidates:
    def __init__(self, profile):
        self.profile = profile
        self.streams = {}
        self.selected = None
        self.recent_screen = deque(maxlen=8)

    def push(self, key, name, code, raw):
        if self.selected and key != self.selected:
            return []
        lines = [raw.strip()]
        if self.profile["decoder"] == "subahibi":
            from subahibi import is_subahibi_raw, translate_and_split
            if is_subahibi_raw(raw):
                lines = translate_and_split(raw)
        lines = [s for s in lines if s and any(c.isalnum() or c in "…！？。「」" for c in s)]
        if not lines:
            return []
        if key not in self.streams:
            self.streams[key] = dict(name=name, code=code, sample=deque(maxlen=4),
                                     pending=tempfile.SpooledTemporaryFile(max_size=65536, mode="w+t", encoding="utf8"))
        row = self.streams[key]
        row["sample"].extend(lines)
        if self.selected:
            return lines
        for line in lines:
            row["pending"].write(json.dumps(line,ensure_ascii=False)+"\n")
        return []

    def choose(self, key):
        if key not in self.streams:
            raise ValueError("候选文本已变化，请重新查看 status")
        self.selected = key
        spool = self.streams[key]["pending"]
        lines = []
        if not spool.closed:
            spool.seek(0)
            lines = [json.loads(line) for line in spool]
        self.close()
        return lines

    def close(self):
        for row in self.streams.values():
            row["pending"].close()

    def suggest(self):
        if self.selected:
            return None
        matched = []
        for key, row in self.streams.items():
            if any(len(q) >= 4 and (q in screen or len(screen) >= 6 and screen in q)
                   for sample in row["sample"] for q in [norm(sample)] for screen in self.recent_screen):
                matched.append(key)
        if len(matched) == 1:
            return matched[0]
        saved = self.profile.get("stream", {})
        known = [k for k,r in self.streams.items() if saved.get("name") == r["name"] and saved.get("code") == r["code"]]
        if len(known) == 1 and not matched and len(self.streams[known[0]]["sample"]) >= 2:
            return known[0]
        return None

    def samples(self):
        return {k: r["sample"][-1][:180] for k,r in self.streams.items() if r["sample"]}


class WindowsOCR:
    def __init__(self, language):
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language
        self.engine = (OcrEngine.try_create_from_user_profile_languages() if language == "auto" else
                       OcrEngine.try_create_from_language(Language(language)))
        if self.engine is None:
            tags = ", ".join(l.language_tag for l in OcrEngine.available_recognizer_languages)
            raise ValueError(f"未找到 {language} OCR 语言包。已安装 {tags}")
        self.language = self.engine.recognizer_language.language_tag

    async def recognize(self, image, region):
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter
        w, h = image.size
        crop = image.crop(tuple(round(v*(w if i%2==0 else h)) for i,v in enumerate(region))).convert("RGBA")
        crop.thumbnail((2000, 2000))
        writer = DataWriter()
        writer.write_bytes(crop.tobytes("raw","BGRA"))
        bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, *crop.size)
        try:
            result = await self.engine.recognize_async(bitmap)
            return " ".join(line.text.strip() for line in result.lines if line.text.strip())
        finally:
            bitmap.close()
            writer.close()


class ScreenText:
    """Wait for typewriter text to settle; retain a sampled line that is immediately replaced."""
    def __init__(self):
        self.pending = ""
        self.emitted = ""

    def update(self, text):
        lines = []
        if self.pending and text != self.pending and not text.startswith(self.pending):
            if self.pending != self.emitted:
                lines.append(self.pending)
            self.emitted = ""
        if text and text == self.pending and text != self.emitted:
            lines.append(text)
            self.emitted = text
        self.pending = text
        return lines
