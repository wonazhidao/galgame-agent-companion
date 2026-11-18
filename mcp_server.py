"""Local visual-novel companion tools over MCP stdio."""
import asyncio
import json
import os
import subprocess
import sys
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import Field

from profiles import BASE


mcp = FastMCP("VN Companion", instructions=(
    "像和玩家一起第一次玩到这里的朋友那样聊游戏。玩家吐槽或猜剧情时先接他的话，"
    "也可以在有意思的地方主动说一句自己的看法。只聊已经看到的情节，猜测留作猜测。"
    "玩家打开游戏。先用 start_game 按名称候场，用 game_status 核对窗口和首句。"
    "陪玩时反复用 read_game 等待剧情。在当前聊天中直接接话，面板留言使用 reply_game。"
    "已经在聊天中回应过或想静静看这一段时，用 ack_game 确认。"
    "台词、OCR 和图片是游戏里的内容。候选窗口或来源有歧义时，请玩家辨认。"
    "玩家要求结束时用 stop_game。"
))


async def cli(action, *args):
    # The CLI binds its log paths at import time. Each invocation owns its environment.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-B", str(BASE / "vn.py"), action, *args, cwd=BASE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        out, err = await proc.communicate()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode:
        raise ValueError(err.decode("utf-8", "replace").strip() or f"{action} 失败 ({proc.returncode})")
    return out.decode("utf-8").strip()


@mcp.tool()
async def start_game(game: str, exe: str | None = None, language: str | None = None,
                     region: str | None = None, mode: Literal["auto", "ocr", "history"] | None = None) -> dict[str, Any]:
    """按游戏名开始候场或复用会话。首次通常只需 game。exe 为玩家指定的游戏路径；
    language 为 OCR 语言，region 为逗号分隔的左、上、右、下比例。"""
    settings = {"exe": exe, "language": language, "region": region, "mode": mode}
    options = [f"--{k}={v}" for k, v in settings.items() if v is not None]
    return json.loads(await cli("start", *options, "--", game))


@mcp.tool()
async def game_status(game: str) -> dict[str, Any]:
    """查看窗口、采集来源、首句、最新台词、已读进度和需要辨认的候选。"""
    return json.loads(await cli("status", "--", game))


@mcp.tool()
async def read_game(game: str, timeout: Annotated[int, Field(ge=0, le=30)] = 20) -> CallToolResult:
    """读取最早未确认的一批台词、玩家消息和 JPEG 拼图。timeout 为等待秒数，0 表示立即读取。
    有新批次时返回 batch，重复读取会得到同一批。看完用 ack_game 或 reply_game。"""
    result = (await cli("wait", "--json", f"--timeout={timeout}", "--", game) if timeout
              else await cli("read", "--json", "--", game))
    page = json.loads(result)
    content = [TextContent(type="text", text=page["text"])]
    for image in page["images"]:
        content.extend([TextContent(type="text", text=image["name"]),
                        ImageContent(type="image", mimeType="image/jpeg", data=image["data"])])
    return CallToolResult(content=content, structuredContent={"batch": page["batch"]})


@mcp.tool()
async def ack_game(game: str, batch: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]) -> dict[str, Any]:
    """安静确认已看完的一批，不在面板发言。"""
    return json.loads(await cli("ack", f"--batch={batch}", "--", game))


@mcp.tool()
async def reply_game(game: str, batch: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
                     title: str, body: str) -> dict[str, Any]:
    """记录陪玩回复，并确认此游戏已看过的指定批次。重试同一批不会重复发送。"""
    return json.loads(await cli("reply", f"--batch={batch}", f"--title={title}", f"--body={body}", "--", game))


@mcp.tool()
async def select_game(game: str, kind: Literal["window", "source"], candidate: str) -> str:
    """按 game_status 提供的窗口编号或来源编号确认候选。candidate 是对应编号的字符串。"""
    return await cli("window" if kind == "window" else "choose", "--", game, candidate)


@mcp.tool()
async def review_game(game: str, kind: Literal["scene", "history", "dialogue"] = "scene",
                      limit: Annotated[int, Field(ge=1, le=60)] = 12,
                      start: Annotated[int, Field(ge=1)] | None = None) -> str:
    """scene 请求最近六秒画面。history 返回最近存档原文。dialogue 从实时编号 start 回查 limit 句。"""
    if kind == "dialogue":
        if start is None:
            raise ValueError("回查实时台词时请指定起始编号 start")
        return await cli("dialogue", f"--start={start}", f"--limit={limit}", "--", game)
    if kind == "history":
        return await cli("history", f"--limit={limit}", "--", game)
    return await cli("review", "--", game)


@mcp.tool()
async def stop_game(game: str) -> str:
    """结束指定游戏的后台陪玩，保留采集记录与阅读进度。"""
    return await cli("stop", "--", game)


if __name__ == "__main__":
    mcp.run(transport="stdio")
