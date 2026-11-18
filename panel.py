"""Optional player controls. The game session also works with the panel closed."""
import json
import queue
from pathlib import Path
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from profiles import BASE, folder
from vn import owner_alive


def read_json(path):
    try:
        return json.loads(path.read_text("utf8"))
    except (OSError, ValueError):
        return {}


class Panel:
    def __init__(self, root):
        self.root = root
        root.title("视觉小说陪玩 · 开发版")
        root.geometry("520x465")
        root.columnconfigure(0,weight=1)
        box = ttk.Frame(root,padding=14)
        box.grid(sticky="nsew")
        box.columnconfigure(0,weight=1)
        self.game = tk.StringVar(value=sys.argv[1] if len(sys.argv)>1 else "")
        self.exe = ""
        self.msgs = queue.Queue()
        self.busy = False
        self.status = tk.StringVar()
        self.line = tk.StringVar()
        self.reply = tk.StringVar()
        self.notice = tk.StringVar()
        ttk.Label(box,text="游戏名称").grid(row=0,column=0,sticky="w")
        field = ttk.Entry(box,textvariable=self.game)
        field.grid(row=1,column=0,sticky="ew",pady=4)
        field.bind("<Return>",lambda _:self.start())
        field.focus_set()
        bar = ttk.Frame(box)
        bar.grid(row=2,column=0,sticky="w",pady=4)
        for label, action in (("开始陪玩",self.start),("选游戏程序…",self.pick_exe),
                              ("补看刚才这段",lambda:self.run("review")),("结束陪玩",lambda:self.run("stop"))):
            ttk.Button(bar,text=label,command=action).pack(side="left",padx=(0,4))
        ttk.Label(box,textvariable=self.status,wraplength=480).grid(row=3,column=0,sticky="w",pady=5)
        ttk.Label(box,textvariable=self.line,wraplength=480).grid(row=4,column=0,sticky="w",pady=4)
        self.choices = ttk.Combobox(box,state="readonly")
        self.choices.grid(row=5,column=0,sticky="ew",pady=2)
        self.confirm = ttk.Button(box,text="使用选中的窗口或台词",command=self.choose)
        self.confirm.grid(row=6,column=0,sticky="w")
        ttk.Separator(box).grid(row=7,column=0,sticky="ew",pady=8)
        ttk.Label(box,textvariable=self.reply,wraplength=480).grid(row=8,column=0,sticky="w")
        self.note = ttk.Entry(box)
        self.note.grid(row=9,column=0,sticky="ew",pady=(8,2))
        self.note.bind("<Return>",lambda _:self.send())
        ttk.Button(box,text="发给 Agent",command=self.send).grid(row=10,column=0,sticky="w")
        ttk.Label(box,textvariable=self.notice,wraplength=480).grid(row=11,column=0,sticky="w",pady=5)
        self.actions = {}
        self.root.after(100,self.refresh)

    def pick_exe(self):
        path = filedialog.askopenfilename(filetypes=[("游戏程序","*.exe")])
        if path:
            if not self.game.get().strip():
                self.game.set(Path(path).stem)
            self.exe = (self.game.get().strip(),path)
            self.notice.set(path)

    def start(self):
        options = ["--exe",self.exe[1]] if self.exe and self.exe[0]==self.game.get().strip() else []
        self.run("start",*options)

    def choose(self):
        if self.choices.get() in self.actions:
            self.run(*self.actions[self.choices.get()])

    def send(self):
        text = self.note.get().strip()
        if text and not self.busy and self.game.get().strip():
            self.run("note",text)
            self.note.delete(0,"end")

    def run(self, cmd, *args):
        game = self.game.get().strip()
        if not game or self.busy:
            return
        self.busy = True
        # A pythonw parent may have no console, but the child still writes to its pipes.
        exe = Path(sys.executable).with_name("python.exe")
        def work():
            try:
                p = subprocess.run([str(exe),"-B",str(BASE/"vn.py"),cmd,game,*args],
                                   capture_output=True,encoding="utf8",errors="replace",
                                   creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
                self.msgs.put(p.stderr.strip() if p.returncode else ("会话已启动" if cmd=="start" else p.stdout.strip()))
            except (OSError,subprocess.TimeoutExpired) as e:
                self.msgs.put(str(e))
        threading.Thread(target=work,daemon=True).start()

    def refresh(self):
        while not self.msgs.empty():
            self.notice.set(self.msgs.get_nowait())
            self.busy = False
        game = self.game.get().strip()
        try:
            d = folder(game) if game else None
        except ValueError:
            d = None
        s = read_json(d/"status.json") if d else {}
        live = owner_alive(read_json(d/"owner.json")) if d else False
        self.status.set(f"{s.get('state','启动中')} · {s.get('source') or '等待文字'} · 已采集 {s.get('captured',0)} / 已读 {s.get('confirmed',0)}"
                        if live else "输入游戏名后开始陪玩" if not s else "会话已结束")
        history = s.get("history", {})
        if live and history.get("available"):
            self.status.set(self.status.get() + f"\n存档回看 · {history.get('entries', 0)} 条 · {history.get('message', '')}")
        self.line.set(s.get("last_line","")[-220:])
        actions = {}
        if live:
            for w in s.get("windows",[]):
                if len(s["windows"])>1 or "window" in s.get("native",""):
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
        self.reply.set(("Agent · "+last[:280]) if last else "")
        self.root.after(750,self.refresh)


if __name__=="__main__":
    root = tk.Tk()
    Panel(root)
    root.mainloop()
