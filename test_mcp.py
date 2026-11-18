import asyncio
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

from profiles import BASE
from test_branch import simulated_game


class CompanionMCP(unittest.TestCase):
    def test_game_to_agent_over_stdio(self):
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(self.exercise_session(Path(tmp)))

    async def exercise_session(self, work):
        config = json.loads(subprocess.check_output(
            [sys.executable, "-B", str(BASE / "vn.py"), "mcp-config"], encoding="utf-8"))
        server = StdioServerParameters(**config["mcpServers"]["vn-companion"], cwd=str(work),
                                      env={"VNCOMPANION_HOME": str(work / "sessions")})
        game = f"Maple MCP 物语 {os.getpid()}"
        other = f"Second game {os.getpid()}"
        with (work / "server.log").open("w", encoding="utf-8") as err:
            async with stdio_client(server, errlog=err) as streams, ClientSession(*streams) as client:
                await client.initialize()
                tools = await client.list_tools()
                self.assertEqual({t.name for t in tools.tools}, {
                    "start_game", "game_status", "read_game", "reply_game", "ack_game",
                    "select_game", "review_game", "stop_game"})

                async def call(name, **kwargs):
                    result = await client.call_tool(name, {"game": game, **kwargs})
                    self.assertFalse(result.isError, result.content[0].text)
                    return result

                async def see(line):
                    until = time.monotonic() + 15
                    while time.monotonic() < until:
                        state = (await call("game_status")).structuredContent
                        if line in state["last_line"]:
                            return state
                        await asyncio.sleep(0.2)
                    self.fail(str(state))

                def jpeg(page):
                    images = [block for block in page.content if block.type == "image"]
                    self.assertTrue(images)
                    for block in images:
                        self.assertEqual(block.mimeType, "image/jpeg")
                        with Image.open(io.BytesIO(base64.b64decode(block.data, validate=True))) as im:
                            self.assertEqual(im.format, "JPEG")
                            im.verify()
                    return images[0].data

                try:
                    started = (await call("start_game", language="en-GB")).structuredContent
                    self.assertEqual(started["state"], "等待游戏")
                    self.assertIsNone(started["last_frame_at"])
                    self.assertIsNone((await call("read_game", timeout=1)).structuredContent["batch"])
                    with simulated_game(work, game) as (control, pid_file):
                        state = await see("The door opens slowly")
                        self.assertEqual(state["target"]["pid"], int(pid_file.read_text()))
                        self.assertIsNotNone(state["last_frame_at"])
                        control.write_text("Please come inside.")
                        await see("Please come inside")
                        page = await call("read_game", timeout=8)
                        text = page.content[0].text
                        self.assertLess(text.index("The door opens slowly"), text.rindex("Please come inside"))
                        picture = jpeg(page)
                        batch = page.structuredContent["batch"]
                        await call("review_game")
                        retry, empty = await asyncio.gather(
                            call("read_game", timeout=0), call("read_game", game=other, timeout=0))
                        self.assertEqual(retry.structuredContent["batch"], batch)
                        self.assertEqual(jpeg(retry), picture)
                        self.assertIsNone(empty.structuredContent["batch"])
                        self.assertFalse(any(b.type == "image" for b in empty.content))
                        reply = dict(batch=batch, title="门开了", body="我看到这两句了。")
                        wrong = await client.call_tool("reply_game", {"game": other, **reply})
                        self.assertTrue(wrong.isError)
                        await call("reply_game", **reply)
                        await call("reply_game", **reply)
                        review = await call("read_game", timeout=5)
                        self.assertNotEqual(review.structuredContent["batch"], batch)
                        if "回看拼图" not in review.content[0].text:
                            state = (await call("game_status")).structuredContent
                            self.fail(f"{review.content[0].text}\nstatus={state}")
                        self.assertTrue(any(b.type == "text" and b.text.startswith("review_") for b in review.content))
                        jpeg(review)
                        queried = await call("review_game", kind="dialogue", start=1, limit=1)
                        self.assertIn("The door opens slowly", queried.content[0].text)
                        self.assertNotIn("Please come inside", queried.content[0].text)
                        review_batch = review.structuredContent["batch"]
                        wrong_ack = await client.call_tool("ack_game", {"game": other, "batch": review_batch})
                        self.assertTrue(wrong_ack.isError)
                        self.assertEqual((await call("read_game", timeout=0)).structuredContent["batch"], review_batch)
                        for _ in range(2):
                            await call("ack_game", batch=review_batch)
                        logs = list((work / "sessions").glob("*/ai_replies.jsonl"))
                        self.assertEqual(len(logs), 1)
                        self.assertEqual(len(logs[0].read_text("utf-8").splitlines()), 1)
                finally:
                    await call("stop_game")
                self.assertFalse((await call("game_status")).structuredContent["running"])
