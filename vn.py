"""Start or operate the local visual-novel companion by game name."""
import argparse
import contextlib
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from profiles import BASE, folder, parse_region


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    cmds = p.add_subparsers(dest="cmd", required=True)
    cmds.add_parser("mcp-config", help="输出本机的 MCP stdio 客户端配置")
    start = cmds.add_parser("start")
    start.add_argument("game")
    start.add_argument("--exe", help="指定游戏的可执行文件完整路径")
    start.add_argument("--language", help="OCR language tag, e.g. ja, zh-Hans or en-US")
    start.add_argument("--region", type=parse_region)
    start.add_argument("--mode", choices=["auto","ocr","history"])
    start.add_argument("--textractor", help="包含 x86、x64 子目录的安装目录")
    for name in ("status","read","wait","review","history","dialogue","stop","choose","window","note","reply","ack","_worker"):
        sub = cmds.add_parser(name)
        sub.add_argument("game")
        if name in ("choose","window","note","_worker"):
            sub.add_argument("arg")
        if name == "wait":
            sub.add_argument("--timeout",type=float,default=20)
        if name in ("history", "dialogue"):
            sub.add_argument("--limit", type=int, default=12)
        if name == "dialogue":
            sub.add_argument("--start", type=int, required=True)
        if name in ("read", "wait"):
            sub.add_argument("--json", action="store_true", help="同时返回本批图片数据")
        if name == "reply":
            sub.add_argument("--title",required=True)
            sub.add_argument("--body",required=True)
        if name in ("reply", "ack"):
            sub.add_argument("--batch",required=True)
    return p


def owner_alive(owner):
    import psutil
    try:
        return bool(owner) and psutil.Process(owner["pid"]).create_time() == owner["born"]
    except (psutil.Error, KeyError):
        return False


@contextlib.contextmanager
def start_lock(folder):
    with (folder/"start.lock").open("a+b") as f:
        f.seek(0)
        msvcrt.locking(f.fileno(),msvcrt.LK_LOCK,1)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)


def command(args):
    if args.cmd == "mcp-config":
        print(json.dumps({"mcpServers": {"vn-companion": {
            "command": str(BASE / ".venv" / "Scripts" / "python.exe"),
            "args": ["-B", str(BASE / "mcp_server.py")],
        }}}, ensure_ascii=False, indent=2))
        return
    d = folder(args.game)
    d.mkdir(exist_ok=True,parents=True)
    os.environ["VNCOMPANION_SESSION"] = str(d)
    import companion_log as log
    owner_path = d/"owner.json"
    if args.cmd == "_worker":
        from session_worker import Worker
        w = Worker(args.game,args.arg)
        try:
            w.run()
        except Exception as e:
            w.state = "启动或采集出错"
            w.control_info = f"{type(e).__name__}: {e}"
            w.status()
            raise
        return
    if args.cmd == "start":
        import psutil
        with start_lock(d):
            owner = log.read_json(owner_path,{})
            if not owner_alive(owner):
                profile = log.read_json(d/"profile.json",{})
                for key in ("exe","language","region","mode","textractor"):
                    value = getattr(args,key)
                    if value is not None:
                        if key in ("exe","textractor"):
                            path = Path(value).expanduser().resolve(strict=True)
                            if key=="exe" and (not path.is_file() or path.suffix.casefold()!=".exe"):
                                raise ValueError("--exe 需要游戏的 .exe 文件")
                            if key=="textractor" and not path.is_dir():
                                raise ValueError("--textractor 需要安装目录")
                            value = str(path)
                        profile[key] = value
                log.atomic_json(d/"profile.json",profile)
                (d/"commands").mkdir(exist_ok=True)
                run = uuid.uuid4().hex
                with (d/"worker.log").open("ab") as err:
                    child = subprocess.Popen([sys.executable,"-B",str(BASE/"vn.py"),"_worker","--",args.game,run],
                                             cwd=BASE,stdin=subprocess.DEVNULL,stdout=err,stderr=err,
                                             creationflags=subprocess.CREATE_NO_WINDOW)
                owner = dict(run=run,pid=child.pid,born=psutil.Process(child.pid).create_time())
                log.atomic_json(owner_path,owner)
            elif any(getattr(args,k) is not None for k in ("exe","language","region","mode","textractor")):
                raise ValueError("此游戏会话正在运行。更改配置前，请先 stop 再 start。")
        until = time.monotonic()+4
        while log.read_json(d/"status.json",{}).get("run") != owner["run"] and time.monotonic()<until and owner_alive(owner):
            time.sleep(0.1)
        args.cmd = "status"
    owner = log.read_json(owner_path,{})
    if args.cmd in ("history", "dialogue"):
        if not 1 <= args.limit <= 60:
            raise ValueError("回看条数应为 1 到 60")
        if args.cmd == "history":
            from save_history import recent
            print(recent(log.HISTORY, args.limit))
        else:
            if args.start < 1:
                raise ValueError("台词编号从 1 开始")
            rows = log.dialogue_range(args.start, args.start + args.limit - 1)
            for seq, line in rows:
                print(line if f"[#{seq}] " in line else f"#{seq} {line}")
            if not rows:
                print("此编号尚无实时台词")
        return
    if args.cmd == "status":
        state = log.read_json(d/"status.json",{})
        if not state or state.get("run") != owner.get("run"):
            state = {"game":args.game,"state":"启动中" if owner_alive(owner) else "未启动"}
        state["running"] = owner_alive(owner)
        if not state["running"] and state.get("state") not in ("已停止","未启动","启动或采集出错"):
            state["state"] = "进程已退出，详见 worker.log"
        print(json.dumps(state,ensure_ascii=False,indent=2))
        return
    if args.cmd == "read":
        from check_delta import run_check
        run_check(as_json=args.json)
        return
    if args.cmd == "wait":
        from wait_companion import ready
        from check_delta import run_check
        end = time.monotonic()+max(0,args.timeout)
        while not ready():
            if time.monotonic()>=end or not owner_alive(owner):
                print(json.dumps(dict(batch=None, text="暂无新内容", images=[]), ensure_ascii=False)
                      if args.json else "暂无新内容")
                return
            time.sleep(0.25)
        run_check(as_json=args.json)
        return
    if args.cmd in ("reply", "ack"):
        from send_ai_reply import send
        if args.cmd == "ack":
            cur = send(None, None, args.batch)
            print(json.dumps(dict(batch=cur["batch"], confirmed=cur["text_seq"])))
        else:
            print(json.dumps(send(args.title,args.body,args.batch),ensure_ascii=False))
        return
    if not owner_alive(owner):
        if args.cmd == "stop":
            print("已停止")
            return
        raise ValueError("请先启动此游戏会话")
    if args.cmd in ("choose","window"):
        state = log.read_json(d/"status.json",{})
        valid = (state.get("candidates",{}) if args.cmd=="choose" else
                 {str(w["hwnd"]) for w in state.get("windows",[])})
        if args.arg not in valid:
            raise ValueError("请使用 status 列出的当前候选")
    request = d/"commands"/f"{time.time_ns()}-{uuid.uuid4().hex}.json"
    log.atomic_json(request,dict(run=owner["run"],action=args.cmd,arg=getattr(args,"arg",None)))
    end = time.monotonic()+5
    while time.monotonic()<end and owner_alive(owner):
        if args.cmd != "stop" and not request.exists():
            receipt = log.read_json(request.with_suffix(".result"),{})
            request.with_suffix(".result").unlink(missing_ok=True)
            if receipt.get("error"):
                raise ValueError(receipt["error"])
            print(receipt.get("message","已处理"))
            return
        time.sleep(0.1)
    print("已停止" if not owner_alive(owner) else "请求已排队")


if __name__ == "__main__":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        command(parser().parse_args())
    except (OSError,ValueError,ImportError) as exc:
        sys.exit(str(exc))
