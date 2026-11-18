from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from profiles import BASE, folder, get_profile
from sources import Candidates, HEADER, ScreenText, Native
from targets import matches, Window


class ProfilesAndText(unittest.TestCase):
    def test_games_and_glyphs(self):
        a = Window(1,22,"美好的每一天",r"D:\\Games\素晴日\BGI-CHS.exe")
        b = Window(2,23,"Maple Diary",r"D:\\Games\Maple Diary\Game.exe")
        browser = Window(3,24,"Maple Diary - guide",r"D:\\Chrome\chrome.exe")
        self.assertEqual(matches(get_profile("Maple Diary"), [a,b,browser]), [b])
        self.assertEqual(matches(get_profile("素晴日"), [a,b,browser]), [a])
        self.assertEqual(folder("素晴日"), folder("美好的每一天"))
        self.assertNotEqual(folder("素晴日"), folder("Maple Diary"))
        from subahibi import MAPPING
        inv = {v:k for k,v in MAPPING.items()}
        raw = "".join(inv.get(c,c) for c in "「早上好」")
        picks = Candidates(get_profile("素晴日"))
        picks.push("A","dialogue","code",raw)
        self.assertEqual(picks.choose("A"), ["「早上好」"])

    def test_native_selection_and_ocr(self):
        m = HEADER.fullmatch("[C:A7:13:0:0:Engine:code] The door opens.")
        self.assertEqual(int(m[2],16),167)
        p = Candidates(get_profile("Maple Diary"))
        p.push("A","menu","1","Preferences")
        p.push("C","text","2","The door opens.")
        p.push("C","text","2","The door opens.")
        self.assertIsNone(p.suggest())
        p.recent_screen.append("thedooropens")
        self.assertEqual(p.suggest(),"C")
        self.assertEqual(p.choose("C"),["The door opens."]*2)
        self.assertEqual(p.push("A","menu","1","Save game"),[])
        st = ScreenText()
        out = []
        for s in ["The","The door","The door opens.","The door opens.","Goodbye.",""]:
            out.extend(st.update(s))
        self.assertEqual(out,["The door opens.","Goodbye."])


class NativeProtocol(unittest.TestCase):
    def test_utf16_streams_only_from_game_and_reuse_selection(self):
        import queue
        import threading
        import types
        import io
        data = ("Usage: cli\r\n[2:FF:1:0:0:Other:code] wrong target\r\n"
                "[3:AA:1:0:0:Menu:menu] 设置\r\n"
                "[4:AA:2:0:0:VN:stable] 第一句台词。\r\n第二句台词。\r\n")
        native = Native.__new__(Native)
        native.pid = 0xaa
        native.events = queue.Queue()
        native.p = types.SimpleNamespace(stdout=io.BytesIO(data.encode("utf-16le")))
        native._read()
        events = []
        while not native.events.empty():
            events.append(native.events.get())
        self.assertEqual([e[-1] for e in events],["设置","第一句台词。","第二句台词。"])
        profile = dict(decoder="plain", stream=dict(name="VN",code="stable"))
        choice = Candidates(profile)
        for event in events:
            choice.push(*event)
        self.assertEqual(choice.suggest(),"4")
        self.assertEqual(choice.choose("4"),["第一句台词。","第二句台词。"])
        p = Candidates(dict(decoder="plain"))
        for key in ("A","B"):
            p.push(key,"VN","c", "Which way will you go?")
        p.recent_screen.append("whichwaywillyougo")
        self.assertIsNone(p.suggest())
        self.assertEqual(p.choose("B"),["Which way will you go?"])


class FastDialogueDelivery(unittest.TestCase):
    def test_status_update_while_previous_state_is_being_read(self):
        import companion_log as log
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            log.atomic_json(path, {"seq": 1})
            with path.open("rb") as reader:
                with patch.object(log.time, "sleep", side_effect=lambda _: reader.close()) as retry:
                    log.atomic_json(path, {"seq": 2})
            retry.assert_called_once()
            self.assertEqual(log.read_json(path, {}), {"seq": 2})

    def test_only_adjacent_pixel_identical_frames_share_a_tile(self):
        import companion_log as log
        from scene_replay import build_montage
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp, patch.object(log, "LOG_DIR", Path(tmp)):
            first = Image.new("RGB", (100, 100), "gray")
            expression = first.copy()
            expression.putpixel((35, 35), (128, 128, 127))
            paths = []
            for i, im in enumerate([first, first, expression, first, first]):
                path = Path(tmp) / f"shot_20260102_120000_{i:06d}_s{i:08d}.png"
                im.save(path)
                paths.append(path)
            montage, count = build_montage(paths)
            self.assertEqual(count, 3)
            with Image.open(montage) as sheet:
                self.assertEqual(sheet.size, (1440, 300))

    def test_burst_arriving_while_agent_reads(self):
        title = "快翻测试"
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, VNCOMPANION_HOME=tmp, PYTHONIOENCODING="utf-8")
            session = Path(tmp) / folder(title).name
            session.mkdir()

            def cli(action, *options):
                return json.loads(subprocess.check_output(
                    [sys.executable, "-B", str(BASE / "vn.py"), action, title, *options],
                    env=env, encoding="utf-8", timeout=10))

            records = [f"[12:00:00] [#{n}] " + ("这是相邻的重复台词。" if n % 3 else "长句" * 30)
                       for n in range(1, 241)]
            with (session / "textlog.txt").open("w", encoding="utf-8") as log:
                log.writelines(line + "\n" for line in records[:4])
            page = cli("read", "--json")
            with (session / "textlog.txt").open("a", encoding="utf-8") as log:
                log.writelines(line + "\n" for line in records[4:])
            (session / "trigger.json").write_text('{"seq":1}', encoding="utf-8")
            self.assertEqual(cli("wait", "--timeout=0", "--json"), page)
            delivered = []
            pages = 0
            while page["batch"]:
                pages += 1
                self.assertLessEqual(pages, len(records))
                delivered.extend(line for line in page["text"].splitlines() if line.startswith("[12:00:00]"))
                cli("reply", "--batch", page["batch"], "--title", "已看", "--body", "继续")
                page = cli("wait", "--timeout=0", "--json")
            self.assertGreater(pages, 2)
            self.assertEqual(delivered, records)


@contextmanager
def simulated_game(work, title):
    """Launch a real Tk window; its PID can differ from the Windows venv launcher."""
    control = work / "line"
    pid_file = work / "window-pid"
    control.write_text("The door opens slowly.")
    game_code = """
import os, sys, tkinter as tk
from pathlib import Path
r = tk.Tk()
r.title(sys.argv[1])
r.geometry('800x480+6500+6500')
c = tk.Canvas(r, bg='#303949', highlightthickness=0)
c.pack(fill='both', expand=True)
c.create_rectangle(0, 292, 800, 460, fill='#151515', outline='')
item = c.create_text(50, 365, anchor='w', fill='white', font=('Arial', 26))
def tick():
    t = Path(sys.argv[2]).read_text()
    if t == 'exit':
        r.destroy()
        return
    c.itemconfigure(item, text=t)
    r.after(50, tick)
Path(sys.argv[3]).write_text(str(os.getpid()))
tick()
r.after(50000, r.destroy)
r.mainloop()
"""
    game = subprocess.Popen([sys.executable, "-B", "-c", game_code, title, str(control), str(pid_file)],
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        yield control, pid_file
    finally:
        control.write_text("exit")
        game.wait(timeout=7)


class DesktopSession(unittest.TestCase):
    def test_optional_panel_controls_same_session(self):
        import tkinter as tk
        from panel import Panel
        import profiles
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, VNCOMPANION_HOME=tmp), patch.object(profiles,"HOME",Path(tmp)):
            game = "Panel fixture " + str(os.getpid())
            def cli(cmd):
                return subprocess.check_output([str(BASE/"vn.cmd"),cmd,game],encoding="utf8",timeout=10)
            root = tk.Tk()
            root.withdraw()
            panel = Panel(root)
            panel.game.set(game)
            def pump(condition):
                until = time.monotonic()+8
                while time.monotonic()<until:
                    root.update()
                    if condition():
                        return
                    time.sleep(0.02)
                self.fail(panel.notice.get())
            try:
                panel.start()
                pump(lambda:not panel.busy and "等待游戏" in panel.status.get())
                self.assertTrue(json.loads(cli("status"))["running"])
                panel.note.insert(0,"panel question")
                panel.send()
                pump(lambda:not panel.busy)
                self.assertIn("panel question",cli("read"))
                panel.run("stop")
                pump(lambda:not panel.busy)
                self.assertFalse(json.loads(cli("status"))["running"])
            finally:
                cli("stop")
                root.destroy()

    def test_start_before_game_and_read_two_real_ocr_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            env = dict(os.environ,VNCOMPANION_HOME=str(work/"sessions"),PYTHONIOENCODING="utf-8")
            env["COMPANION_LOG_DIR"] = str(work/"must-not-write")
            title = "Maple Diary QA " + str(os.getpid())
            def cli(cmd,*args):
                return subprocess.check_output([sys.executable,"-B",str(BASE/"vn.py"),cmd,title,*args],
                                               env=env,timeout=12,encoding="utf8")
            def status():
                return json.loads(cli("status"))
            def wait(predicate):
                end = time.monotonic()+16
                while time.monotonic() < end:
                    s = status()
                    if predicate(s):
                        return s
                    time.sleep(0.25)
                self.fail(str(s))
            try:
                started = json.loads(cli("start","--language","en-GB"))
                self.assertEqual(started["state"],"等待游戏")
                self.assertEqual(json.loads(cli("start"))["pid"],started["pid"])
                with simulated_game(work, title) as (control, pid_file):
                    wait(lambda s:"The door opens slowly" in s["last_line"])
                    control.write_text("Please come inside.")
                    ready = wait(lambda s:"Please come inside" in s["last_line"])
                    self.assertEqual(ready["source"],"OCR")
                    self.assertEqual(ready["target"]["pid"],int(pid_file.read_text()))
                    out = cli("read")
                    self.assertLess(out.index("The door opens slowly"),out.rindex("Please come inside"))
                    self.assertIn("MONTAGE:",out)
                    batch = out.split("BATCH: ")[1].split()[0]
                    cli("reply","--title","Seen","--body","At the door","--batch",batch)
                    cli("note","Did you see the door?")
                    cli("review")
                    extra = cli("wait","--timeout","4")
                    self.assertIn("review_",extra)
                    self.assertIn("Did you see the door?",extra)
                wait(lambda s:s["state"]=="等待游戏")
            finally:
                cli("stop")
            self.assertFalse(status()["running"])
            self.assertFalse((work/"must-not-write").exists())


if __name__ == "__main__":
    unittest.main()
