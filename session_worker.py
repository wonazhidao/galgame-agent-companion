"""Background worker scoped to one named game and one independent log directory."""
import asyncio
from collections import deque
from datetime import datetime
from pathlib import Path
import os
import queue
import sqlite3
import sys
import threading
import time

import companion_log as log
from game_window import capture_hwnd_image, user32
from profiles import get_profile, norm
from scene_replay import SceneBuffer, SAMPLE_SECONDS
from sources import Candidates, Native, ScreenText, WindowsOCR, cli_path, extractor_busy
from targets import matches, windows


class OcrPump:
    def __init__(self, profile):
        self.profile = profile
        self.input = queue.Queue(1)
        self.output = queue.Queue()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            engine = WindowsOCR(self.profile["language"])
            with asyncio.Runner() as runner:
                while not self.closed.is_set():
                    try:
                        im = self.input.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    text = runner.run(engine.recognize(im,self.profile["region"]))
                    self.output.put(("text", text, engine.language))
        except Exception as e:
            self.output.put(("error", str(e), ""))

    def submit(self, im):
        if self.thread.is_alive() and self.input.empty():
            self.input.put_nowait(im)


class Worker:
    def __init__(self, game, run_id):
        self.game, self.run_id = game, run_id
        self.profile = get_profile(game)
        self.seq = log.count_records()
        self.target = None
        self.frames = SceneBuffer()
        self.options = []
        self.chosen_window = None
        self.native = self.ocr = None
        self.selector = Candidates(self.profile)
        self.screen = ScreenText()
        self.recent = deque(maxlen=32)
        self.mirror = []
        self.last_source = ""
        self.first_line = self.last_line = ""
        self.last_frame_at = None
        self.history = None
        self.history_info = ""
        self.last_history_poll = 0
        self.last_native = 0
        self.last_ocr = self.last_scan = self.last_status = 0
        self.ocr_language = ""
        self.native_info = self.ocr_info = self.control_info = ""
        self.stopping = False
        self.pending_wake = None
        self.state = "等待游戏"

    def status(self):
        t = self.target
        log.atomic_json(log.LOG_DIR / "status.json", dict(
            run=self.run_id, pid=os.getpid(), game=self.game, state=self.state, updated=time.time(),
            target={"pid":t.pid,"hwnd":t.hwnd,"title":t.title,"exe":t.exe} if t else None,
            windows=[dict(hwnd=w.hwnd, title=w.title, exe=w.exe) for w in self.options],
            captured=self.seq, confirmed=log.load_cursor()["text_seq"], first_line=self.first_line,
            last_line=self.last_line, source=self.last_source, stream=self.selector.selected,
            candidates=self.selector.samples(), native=self.native_info, ocr=self.ocr_info,
            history=dict(available=self.history is not None, message=self.history_info,
                         slot=self.history.last_slot if self.history else "",
                         entries=len(self.history.state.get("rows", [])) if self.history else 0,
                         saved_ns=self.history.state.get("saved_ns") if self.history else None),
            language=self.ocr_language, last_frame_at=self.last_frame_at, message=self.control_info))

    def emit(self, lines, source):
        if not lines:
            return
        now = time.monotonic()
        if self.last_source and source != self.last_source:
            self.mirror = [(t,norm(s)) for t,s in self.recent if now-t < 4]
        self.last_source = source
        waiting_for_first_line = not self.first_line
        with log.TEXT.open("a",encoding="utf8") as f:
            for line in lines:
                key = norm(line)
                match = next((v for v in self.mirror if now-v[0] < 4 and key and v[1]==key), None)
                if match:
                    self.mirror.remove(match)
                    continue
                self.recent.append((now,line))
                self.seq += 1
                stamp = datetime.now().strftime("%H:%M:%S")
                f.write(f"[{stamp}] [#{self.seq}] {line}\n")
                if self.history and self.history.observed_baseline:
                    self.history.live.append((self.seq, line))
                self.first_line = self.first_line or line
                self.last_line = line
                self.state = "已读到文字"
                if self.pending_wake is None:
                    self.pending_wake = now
        if waiting_for_first_line and self.first_line:
            self.wake("first_line")

    def wake(self, reason):
        with log.delivery_lock():
            seq = log.read_json(log.TRIGGER, {}).get("seq", 0) + 1
            log.atomic_json(log.TRIGGER, dict(seq=seq, reason=reason))
        self.pending_wake = None

    def disconnect(self):
        if self.ocr:
            self.ocr.closed.set()
        if self.native:
            self.native.close()
        if self.last_source != "Textractor":
            self.emit(self.screen.update(""), "OCR")
        self.target = self.native = self.ocr = None
        self.history = None
        self.history_info = ""
        self.last_frame_at = None
        self.first_line = self.last_line = ""
        self.ocr_language = self.ocr_info = self.native_info = ""
        self.screen = ScreenText()
        self.frames = SceneBuffer()
        self.recent.clear()
        self.mirror.clear()
        self.last_source = ""
        self.selector.close()
        self.selector = Candidates(self.profile)
        self.last_native = 0

    def connect(self, win):
        self.target = win
        self.selector = Candidates(dict(self.profile))
        if Path(win.exe).name.casefold() != "bgi-chs.exe":
            self.selector.profile["decoder"] = "plain"
        self.first_line = self.last_line = ""
        self.state = "已找到窗口，等待读到文字"
        if self.profile["mode"] != "ocr":
            from bgi_history import supports
            from save_history import SaveHistory
            try:
                if supports(win.exe):
                    self.history = SaveHistory(Path(win.exe).parent / "UserData", log.HISTORY)
                    self.history_info = "正在核对存档回看"
                else:
                    self.history_info = "此游戏版本尚无已验证的存档回看适配"
            except (OSError, ValueError, sqlite3.Error) as e:
                self.history_info = f"存档回看暂不可用 · {e}"
        if self.profile["mode"] == "history":
            self.state = self.history_info
            return
        self.ocr_info = "正在准备 Windows OCR"
        self.ocr = OcrPump(self.profile)
        self.native_info = "画面与 OCR 模式"
        known = (self.profile.get("exe") or Path(win.exe).name.casefold() in
                 {s.casefold() for s in self.profile["executables"]} or
                 norm(Path(win.exe).stem) in map(norm, self.profile["aliases"]) or
                 any(norm(a) in norm(win.title) and norm(a) in norm(Path(win.exe).parent.name)
                     for a in self.profile["aliases"]))
        if self.profile["mode"] != "ocr" and known:
            try:
                cli = cli_path(win.exe, self.profile)
                if extractor_busy():
                    self.native_info = "已有提取器在运行，本次使用 OCR"
                elif cli:
                    self.native = Native(cli, win.pid, log.LOG_DIR)
                    self.native_info = "等待命令行提取器返回台词"
                else:
                    self.native_info = "未找到匹配位数的提取器，使用 OCR"
            except (ValueError,OSError) as e:
                self.native_info = f"提取器不可用，使用 OCR · {e}"
        elif not known and self.profile["mode"] != "ocr":
            self.native_info = "已按标题匹配；可用 window 确认此程序以启用提取器"

    def matching_windows(self):
        background = self.profile["mode"] == "history" or (
            self.profile["mode"] == "auto" and self.profile["decoder"] == "subahibi")
        return matches(self.profile, windows(include_minimized=background))

    def scan(self):
        self.options = self.matching_windows()
        if self.target and any((w.hwnd,w.pid,w.exe)==(self.target.hwnd,self.target.pid,self.target.exe)
                               for w in self.options):
            return
        if self.target:
            self.disconnect()
        chosen = next((w for w in self.options if w.hwnd == self.chosen_window), None)
        if not chosen and len(self.options)==1:
            chosen = self.options[0]
        if chosen:
            self.connect(chosen)
        else:
            self.state = "等待选择窗口" if self.options else "等待游戏"

    def remember(self, **settings):
        path = log.LOG_DIR / "profile.json"
        saved = log.read_json(path,{})
        saved.update(settings)
        log.atomic_json(path,saved)
        self.profile.update(settings)

    def choose(self, key, remember):
        lines = self.selector.choose(key)
        row = self.selector.streams[key]
        if remember:
            self.remember(stream=dict(name=row["name"],code=row["code"]))
        self.last_native = time.monotonic()
        self.native_info = f"已选择 {row['name']} ({key})"
        self.emit(lines,"Textractor")

    def controls(self):
        for p in sorted((log.LOG_DIR/"commands").glob("*.json")):
            msg = log.read_json(p,{})
            receipt = {}
            try:
                if msg.get("run") != self.run_id:
                    continue
                action, arg = msg["action"], msg.get("arg")
                if action == "stop":
                    self.stopping = True
                elif action == "choose":
                    self.choose(arg, True)
                elif action == "window":
                    win = next((w for w in self.matching_windows() if w.hwnd == int(arg)),None)
                    if win is None:
                        raise ValueError("窗口已变化，请再查看 status")
                    self.disconnect()
                    self.chosen_window = win.hwnd
                    self.remember(exe=win.exe)
                    self.scan()
                elif action == "note":
                    if not arg or not arg.strip():
                        raise ValueError("请输入给 Agent 的消息")
                    with log.NOTES.open("a", encoding="utf8") as f:
                        f.write(f"[{datetime.now():%H:%M:%S}] {arg.strip()}\n")
                    self.wake("player_note")
                elif action == "review":
                    frames = self.frames.recent()
                    current_only = False
                    if not frames and self.target:
                        self.scan()
                        if self.target:
                            image = capture_hwnd_image(self.target.hwnd)
                            if image is not None:
                                self.last_frame_at = time.time()
                                self.frames.sample(image, self.seq, self.last_frame_at)
                                frames = self.frames.recent()
                                current_only = True
                    img = self.frames.save_review(frames)
                    start = max(1, frames[0].seq if frames else self.seq-7)
                    lines = [s for _,s in log.dialogue_range(start,self.seq)]
                    with log.NOTES.open("a",encoding="utf8") as f:
                        f.write("玩家请求补看刚才这段\n" + (f"回看拼图 {img}\n" if img else "最近没有游戏画面\n"))
                        if current_only:
                            f.write("过去六秒没有缓存画面，拼图中只有请求时的当前帧。\n")
                        f.writelines(s+"\n" for s in lines)
                    self.wake("review_recent")
                self.control_info = f"已处理 {action}"
                receipt["message"] = self.control_info
            except (OSError,ValueError) as e:
                self.control_info = str(e)
                receipt["error"] = str(e)
            finally:
                if receipt and action != "stop":
                    log.atomic_json(p.with_suffix(".result"), receipt)
                p.unlink(missing_ok=True)

    def tick(self):
        now = time.monotonic()
        self.controls()
        if self.stopping:
            return
        if now-self.last_scan >= 0.6:
            self.scan()
            self.last_scan = now
        if self.target:
            self.observe(now)
        if self.pending_wake is not None and now-self.pending_wake >= 2:
            self.wake("new_game_content")
        if now-self.last_status >= 0.5:
            self.status()
            self.last_status = now

    def observe(self, now):
        if self.history and now-self.last_history_poll >= 1:
            self.last_history_poll = now
            try:
                if self.history.poll() and self.pending_wake is None:
                    self.pending_wake = now
                self.history_info = self.history.message
                if self.profile["mode"] == "history":
                    self.state = f"存档回看 · {self.history_info}"
            except (OSError, ValueError, sqlite3.Error) as e:
                self.history_info = f"存档回看等待重试 · {e}"
        if self.native:
            while not self.native.events.empty():
                lines = self.selector.push(*self.native.events.get_nowait())
                if lines:
                    self.emit(lines, "Textractor")
                    self.last_native = now
            if self.native.p.poll() is not None:
                self.native.close()
                self.native = None
                self.native_info = "提取器已退出，继续 OCR"
        im = None if user32.IsIconic(self.target.hwnd) else capture_hwnd_image(self.target.hwnd)
        if im is not None:
            if self.last_frame_at is None and not self.first_line and self.profile["mode"] != "history":
                self.state = "已取得画面，等待读到文字"
            self.last_frame_at = time.time()
            saved = self.frames.sample(im,self.seq,self.last_frame_at)
            if saved and self.pending_wake is None:
                self.pending_wake = now
            if self.ocr and now-self.last_ocr >= 0.45:
                self.ocr.submit(im)
                self.last_ocr = now
        while self.ocr and not self.ocr.output.empty():
            kind, text, language = self.ocr.output.get_nowait()
            if kind == "error":
                self.ocr_info = f"OCR 不可用 · {text}。仍可读取画面。"
                continue
            self.ocr_language = language
            self.ocr_info = "Windows OCR"
            if norm(text):
                self.selector.recent_screen.append(norm(text))
            suggestion = self.selector.suggest()
            if suggestion:
                self.choose(suggestion, True)
            screen_lines = self.screen.update(text)
            if not self.native or not self.selector.selected or now-self.last_native >= 4:
                self.emit(screen_lines, "OCR")
        if not self.selector.selected:
            suggestion = self.selector.suggest()
            if suggestion:
                self.choose(suggestion, True)

    def run(self):
        try:
            self.status()
            while not self.stopping:
                started = time.monotonic()
                self.tick()
                time.sleep(max(0.01, SAMPLE_SECONDS-(time.monotonic()-started)))
        finally:
            self.disconnect()
            if self.pending_wake is not None:
                self.wake("session_end")
            self.state = "已停止"
            self.status()
