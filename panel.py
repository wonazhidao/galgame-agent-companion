"""Optional player controls. The game session also works with the panel closed."""
import json
import queue
from pathlib import Path
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from profiles import BASE, BUILTINS, folder, builtin, norm
from targets import NON_GAME_APPS, windows
from vn import owner_alive, saved_sessions


def read_json(path):
    try:
        return json.loads(path.read_text("utf8"))
    except (OSError, ValueError):
        return {}


class Panel:
    def __init__(self, root, initial_game=""):
        self.root = root
        root.title("Galgame 陪玩")
        root.minsize(470, 420)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        paper, ink, quiet = "#f5f3ed", "#24362f", "#52655c"
        green, white = "#275d4c", "#fffefa"
        font = ("Microsoft YaHei UI", 10)
        root.option_add("*Font", font)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", font=font, background=paper, foreground=ink)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Heading.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Quiet.TLabel", foreground=quiet, font=("Microsoft YaHei UI", 9))
        style.configure("State.TLabel", background="#dce9e1", foreground=green, padding=(9, 4))
        style.configure("Pane.TFrame", background=white)
        style.configure("Pane.TLabel", background=white)
        style.configure("Caption.TLabel", background=white, foreground=quiet, font=("Microsoft YaHei UI", 9))
        style.configure("TCombobox", fieldbackground=white, padding=6)
        style.configure("TEntry", fieldbackground=white, padding=6)
        style.configure("TButton", padding=(9, 5))
        style.configure("Primary.TButton", background=green, foreground=white, padding=(12, 6))
        style.map("Primary.TButton", background=[("disabled", "#d5d9d3"), ("pressed", "#194334"), ("active", "#307159")],
                  foreground=[("disabled", quiet), ("!disabled", white)])
        box = ttk.Frame(root, padding=(20, 15, 20, 14))
        box.grid(sticky="nsew")
        box.columnconfigure(0, weight=1)
        self.game = tk.StringVar(value=initial_game)
        self.exe = None
        self.window_id = None
        self.window_pid = None
        self.game_options = {}
        self.msgs = queue.Queue()
        self.busy = False
        self.status = tk.StringVar()
        self.line = tk.StringVar()
        self.reply = tk.StringVar()
        self.notice = tk.StringVar()
        self.badge = tk.StringVar(value="待连接")
        header = ttk.Frame(box)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(header, text="Galgame 陪玩", style="Title.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.badge, style="State.TLabel").pack(side="right")
        titlebar = ttk.Frame(box)
        titlebar.grid(row=1, column=0, sticky="ew")
        ttk.Label(titlebar, text="选择游戏", style="Heading.TLabel").pack(side="left")
        ttk.Button(titlebar, text="浏览程序…", command=self.pick_exe).pack(side="right")
        bar = ttk.Frame(box)
        bar.grid(row=2, column=0, sticky="ew", pady=(5, 0))
        bar.columnconfigure(0, weight=1)
        self.field = ttk.Combobox(bar, textvariable=self.game, postcommand=self.refresh_games, height=12)
        self.field.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.field.bind("<<ComboboxSelected>>", self.select_game)
        self.field.bind("<Return>", lambda _: self.start())
        self.start_button = ttk.Button(bar, text="开始陪玩", style="Primary.TButton", command=self.start)
        self.start_button.grid(row=0, column=1)
        ttk.Label(box, textvariable=self.status, style="Quiet.TLabel", wraplength=440).grid(row=3, column=0, sticky="w", pady=(4, 8))
        pane = ttk.Frame(box, padding=(12, 9), style="Pane.TFrame")
        pane.grid(row=4, column=0, sticky="ew")
        self.details = ttk.Frame(pane, style="Pane.TFrame")
        self.details.pack(fill="x")
        ttk.Label(self.details, text="当前台词", style="Caption.TLabel").pack(side="left")
        self.review_button = ttk.Button(self.details, text="回看", command=lambda: self.run("review"))
        self.review_button.pack(side="right")
        ttk.Label(pane, textvariable=self.line, style="Pane.TLabel", wraplength=420).pack(anchor="w", pady=(3, 0))
        self.choices = ttk.Combobox(box,state="readonly")
        self.choices.grid(row=5,column=0,sticky="ew",pady=2)
        self.confirm = ttk.Button(box,text="使用选中的窗口或台词",command=self.choose)
        self.confirm.grid(row=6,column=0,sticky="w")
        chatbar = ttk.Frame(box)
        chatbar.grid(row=7, column=0, sticky="ew", pady=(12, 2))
        ttk.Label(chatbar, text="和 Agent 聊天", style="Heading.TLabel").pack(side="left")
        self.stop_button = ttk.Button(chatbar, text="结束陪玩", command=lambda: self.run("stop"))
        self.stop_button.pack(side="right")
        ttk.Label(box,textvariable=self.reply,wraplength=440).grid(row=8,column=0,sticky="w",pady=(2,5))
        composer = ttk.Frame(box)
        composer.grid(row=9, column=0, sticky="ew")
        composer.columnconfigure(0, weight=1)
        self.note = ttk.Entry(composer)
        self.note.grid(row=0,column=0,sticky="ew",padx=(0,8))
        self.note.bind("<Return>",lambda _:self.send())
        self.send_button = ttk.Button(composer,text="发送",command=self.send)
        self.send_button.grid(row=0,column=1)
        ttk.Label(box,textvariable=self.notice,style="Quiet.TLabel",wraplength=440).grid(row=10,column=0,sticky="w",pady=(4,0))
        self.actions = {}
        self.choices.grid_remove()
        self.confirm.grid_remove()
        self.refresh_games()
        opened = [v[0] for k, v in self.game_options.items() if k.startswith("陪玩中 · ")]
        if not self.game.get() and len(opened) == 1:
            self.game.set(opened[0])
        self.refresh()

    def refresh_games(self):
        options = {}
        saved = saved_sessions(active_only=False)
        for s in saved:
            if s["running"]:
                options["陪玩中 · " + s["game"]] = (s["game"], None, None, None)
        for w in windows(include_minimized=True):
            exe = Path(w.exe)
            if exe.stem.casefold() in NON_GAME_APPS or not norm(w.title):
                continue
            known = builtin(w.title)
            game = known["aliases"][0] if known else w.title
            label = f"窗口 · {w.title}  ({exe.name})"
            if label in options:
                label += f" · {w.hwnd}"
            options[label] = (game, w.exe, w.hwnd, w.pid)
        names = [s["game"] for s in saved] + [p["aliases"][0] for p in BUILTINS]
        present = {entry[0] for entry in options.values()}
        for game in dict.fromkeys(names):
            if game not in present:
                options["候场 · " + game] = (game, None, None, None)
        self.game_options = options
        self.field["values"] = list(options)

    def select_game(self, event=None):
        item = self.game_options.get(self.game.get())
        if item:
            game, exe, self.window_id, self.window_pid = item
            self.game.set(game)
            self.exe = (game, exe) if exe else None
            self.notice.set("")
            self.start_button.configure(text="连接所选窗口" if self.window_id else "开始陪玩")

    def pick_exe(self):
        path = filedialog.askopenfilename(filetypes=[("游戏程序","*.exe")])
        if path:
            if not self.game.get().strip():
                self.game.set(Path(path).parent.name if Path(path).stem.lower() in ("game", "main") else Path(path).stem)
            self.exe = (self.game.get().strip(),path)
            self.window_id = None
            self.window_pid = None
            self.notice.set(path)

    def start(self):
        game = self.game.get().strip()
        try:
            d = folder(game)
        except ValueError as exc:
            self.notice.set(str(exc))
            return
        options = ["--exe", self.exe[1]] if self.exe and self.exe[0] == game else []
        if options and self.window_id:
            options += ["--window-id", str(self.window_id), "--window-pid", str(self.window_pid)]
        if owner_alive(read_json(d/"owner.json"), d):
            s = read_json(d/"status.json")
            if options and self.window_id and (s.get("target") or {}).get("hwnd") != self.window_id:
                if any(w["hwnd"] == self.window_id for w in s.get("windows", [])):
                    self.run("window", str(self.window_id))
                else:
                    self.run("start", *options, restart=True)
            else:
                self.run("start")
            return
        self.run("start",*options)

    def choose(self):
        if self.choices.get() in self.actions:
            self.run(*self.actions[self.choices.get()])

    def send(self):
        text = self.note.get().strip()
        if text and not self.busy and self.game.get().strip() and not self.send_button.instate(["disabled"]):
            self.run("note",text)

    def run(self, cmd, *args, restart=False):
        game = self.game.get().strip()
        if not game or self.busy:
            return
        self.busy = True
        # A pythonw parent may have no console, but the child still writes to its pipes.
        exe = Path(sys.executable).with_name("python.exe")
        def work():
            def invoke(*argv):
                return subprocess.run([str(exe), "-B", str(BASE/"vn.py"), *argv],
                                      capture_output=True, encoding="utf8", errors="replace",
                                      creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
            try:
                if restart:
                    stopped = invoke("stop", "--", game)
                    if stopped.returncode or stopped.stdout.strip() != "已停止":
                        self.msgs.put((stopped.stderr.strip() or "正在结束旧会话，请稍后再试。", None))
                        return
                command = [*args, "--", game] if cmd == "start" else ["--", game, *args]
                p = invoke(cmd, *command)
                message = p.stderr.strip() if p.returncode else ("会话已启动" if cmd=="start" else p.stdout.strip())
                self.msgs.put((message, args[0] if cmd == "note" and not p.returncode else None))
            except (OSError,subprocess.TimeoutExpired) as e:
                self.msgs.put((str(e), None))
        threading.Thread(target=work,daemon=True).start()

    def refresh(self):
        while not self.msgs.empty():
            message, sent = self.msgs.get_nowait()
            self.notice.set(message)
            if sent is not None and self.note.get().strip() == sent:
                self.note.delete(0, "end")
            self.busy = False
        game = self.game.get().strip()
        try:
            d = folder(game) if game else None
        except ValueError:
            d = None
        s = read_json(d/"status.json") if d else {}
        live = owner_alive(read_json(d/"owner.json"), d) if d else False
        badge = "已读到台词" if s.get("first_line") else "已找到窗口" if s.get("target") else "候场中"
        self.badge.set(badge if live else "待连接")
        self.status.set(f"{s.get('state','启动中')} · 已采集 {s.get('captured',0)} / 已读 {s.get('confirmed',0)}"
                        if live else "从下拉列表选游戏，也可以输入名称候场。" if not s else "这次陪玩已结束。")
        switching = live and self.exe and self.exe[0] == game and self.window_id and self.window_id != (s.get("target") or {}).get("hwnd")
        self.start_button.configure(text="切换到此窗口" if switching else "接入会话" if live else "开始陪玩")
        self.start_button.state(["disabled"] if self.busy else ["!disabled"])
        for button in (self.send_button, self.stop_button, self.review_button):
            button.state(["!disabled"] if live and not self.busy else ["disabled"])
        history = s.get("history", {})
        if live and history.get("available"):
            self.status.set(self.status.get() + f"\n存档回看 · {history.get('entries', 0)} 条 · {history.get('message', '')}")
        self.line.set(s.get("last_line","")[-160:] if live and s.get("last_line") else "等待读到第一句。")
        actions = {}
        if live:
            for w in s.get("windows",[]):
                if len(s["windows"])>1 or s.get("state") == "等待选择窗口" or "window" in s.get("native",""):
                    actions[f"窗口 · {w['title']} · {w['hwnd']}"] = ("window",str(w["hwnd"]))
            for key,line in s.get("candidates",{}).items():
                actions[f"{key} · {line[:70]}"] = ("choose",key)
        if actions != self.actions:
            before = self.choices.get()
            self.actions = actions
            self.choices["values"] = list(actions)
            self.choices.set(before if before in actions else next(iter(actions),""))
        if actions:
            self.choices.grid()
            self.confirm.grid()
        else:
            self.choices.grid_remove()
            self.confirm.grid_remove()
        last = ""
        if d and (d/"ai_replies.jsonl").is_file():
            with (d/"ai_replies.jsonl").open("rb") as f:
                f.seek(max(0,f.seek(0,2)-65536))
                for row in reversed(f.read().splitlines()):
                    try:
                        obj = json.loads(row)
                        if obj.get("role")=="ai":
                            last = obj["body"]
                            break
                    except (ValueError,KeyError):
                        continue
        self.reply.set(last[:240] if last else "")
        self.root.after(750,self.refresh)


if __name__=="__main__":
    root = tk.Tk()
    Panel(root, sys.argv[1] if len(sys.argv)>1 else "")
    root.mainloop()
