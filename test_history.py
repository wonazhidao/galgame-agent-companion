import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from bgi_history import RECORD_SIZE, SAVE_HEADER, read_save, records, sdc_payload, supports
from profiles import BASE, folder
from save_history import display, history_delta, history_page, live_references, recent, SaveHistory


def rows(keys):
    return [dict(key=key, speaker="", text="嗯") for key in keys]


class HistoryAlignment(unittest.TestCase):
    def test_only_unique_occurrences_get_speaker_corrections(self):
        saved = [dict(key=str(i), speaker=speaker, text=text) for i, (speaker, text) in enumerate([
            ("", "外面已经放晴。"), ("阿青", "嗯。"), ("小舟", "嗯。"),
            ("", "她没有回头。"), ("阿青", "待会儿在这里见。")])]
        live = [(20, saved[0]["text"]), (21, "嗯。"), (22, "嗯。"), (23, display(saved[-1]))]
        self.assertEqual(live_references(saved, live), {0: (20, ""), 1: (21, "阿青"),
                                                       2: (22, "小舟"), 4: (23, "")})
        self.assertEqual(live_references(saved, live[:1] + live[2:]), {0: (20, ""), 4: (23, "")})
        self.assertEqual(live_references(saved, live[::-1]), {})
        self.assertEqual(live_references(saved, [(1, "嗯。"), (2, "嗯。")]), {})
        self.assertEqual(live_references(saved, live[:2] + [(22, "思。")] + live[-1:]), {})

    def test_saved_references_survive_restart_and_respect_reader_position(self):
        initial = [dict(key="base", speaker="", text="开始")]
        extra = [dict(key=str(i), speaker=s, text=t) for i, (s, t) in enumerate([
            ("", "灯光落在写字台。"), ("", "我把书页翻过来。"), ("阿青", "嗯。"),
            ("小舟", "嗯。"), ("", "存档单独记着这一句。"), ("", "窗外传来脚步声。")])]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            db = directory / "history.db"
            slot = directory / "SubarashikihibiHD001.sud"
            current = initial.copy()
            def write_save(watch, sequence, stamp):
                current[:] = sequence
                slot.write_bytes(str(stamp).encode())
                os.utime(slot, (stamp, stamp))
                watch.poll()
            with patch("save_history.read_save", side_effect=lambda _: list(current)):
                watch = SaveHistory(directory, db)
                write_save(watch, initial, 1000)
                watch.live.extend(enumerate([r["text"] for r in extra if r != extra[4]], 1))
                write_save(watch, initial + extra, 1001)
                self.assertFalse(SaveHistory(directory, db).poll())
                raw, end, _ = history_page(db, 0, 9999)
                compact, same_end, _ = history_page(db, 0, 9999, live_end=4)
                self.assertEqual(end, same_end)
                self.assertEqual(raw[1:], list(map(display, extra)))
                self.assertEqual(compact[1:], ["#1–#2 与存档一致", "#3 存档署名【阿青】",
                                               "#4 存档署名【小舟】", extra[4]["text"], extra[5]["text"]])
                self.assertIn(extra[5]["text"], recent(db, 1))
                branch = [dict(r, key="branch"+r["key"]) for r in extra]
                watch.live.extend(enumerate([r["text"] for r in branch], 6))
                write_save(watch, initial + branch, 1002)
                changed, _, _ = history_page(db, end, 9999, live_end=11)
                self.assertIn("分歧", changed[0])
                self.assertEqual(changed[1:], list(map(display, branch)))

    def test_speaker_and_body_offsets_exclude_voice_and_ruby(self):
        # Standalone SDC literal blocks enclosing one DCFS record, with two text segments.
        from functools import reduce
        from operator import xor
        rec = bytearray(RECORD_SIZE)
        pool = b"Ada\0Hello, \0audio_4\0world!\0ruby\0"
        rec[0x123e8:0x123e8+len(pool)] = pool
        for offset, value in [(0x123e4, len(pool)), (0x11ce0, 2), (0x11cd8, 0),
                              (0x11d00, 4), (0x11d38, pool.index(b"world!"))]:
            struct.pack_into("<I", rec, offset, value)
        data = b"DCFS FORMAT 1.00" + struct.pack("<II", RECORD_SIZE, 1) + rec
        packed = bytearray()
        for i in range(0, len(data), 128):
            block = data[i:i+128]
            packed.append(len(block)-1)
            packed.extend(block)
        seed = 0
        for i, v in enumerate(packed):
            product = seed * 0x015a4e35
            packed[i] = (v + (product >> 16)) & 255
            seed = (product + 1) & 0xffffffff
        header = struct.pack("<IIIHH", 0, len(packed), len(data), sum(packed) & 65535, reduce(xor, packed))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.sud"
            path.write_bytes(bytes(SAVE_HEADER) + b"SDC FORMAT 1.00\0" + header + packed)
            row, = read_save(path)
            self.assertEqual((row["speaker"], row["text"]), ("Ada", "Hello, world!"))

    def test_repeats_rolling_window_and_branch(self):
        old = rows("abcd")
        self.assertEqual(history_delta(old, rows("cdef"))[1], rows("ef"))
        self.assertEqual(history_delta(old, rows("abxy"))[1], rows("xy"))
        self.assertEqual(history_delta(old, rows("ab"))[1], [])
        self.assertIn("无共同", history_delta(old, rows("wxyz"))[0])
        self.assertIn("歧义", history_delta(rows("abb"), rows("bbc"))[0])
        self.assertEqual(history_delta(old, old), ("", []))

    def test_incomplete_or_unknown_layout(self):
        with self.assertRaises(ValueError):
            sdc_payload(b"SDC")
        with self.assertRaises(ValueError):
            list(records(bytearray(b"DCFS FORMAT 1.00" + struct.pack("<II", RECORD_SIZE, 401))))


@unittest.skipUnless(os.getenv("VNCOMPANION_TEST_GAME_DIR"), "需要另行指定可选的真实存档测试样本")
class ActualSaves(unittest.TestCase):
    def test_real_saves_through_cli_batch_and_restart(self):
        game_dir = Path(os.environ["VNCOMPANION_TEST_GAME_DIR"])
        first_slot = game_dir / "UserData" / "SubarashikihibiHD001.sud"
        next_slot = first_slot.with_name("SubarashikihibiHD003.sud")
        self.assertTrue(supports(game_dir / "BGI-CHS.exe"))
        old, new = read_save(first_slot), read_save(next_slot)
        self.assertEqual((len(old), len(new)), (400, 400))
        reason, delta = history_delta(old, new)
        self.assertEqual(reason, "新增")
        self.assertEqual(delta, new[-91:])
        self.assertEqual(history_delta(new, old)[1], [])
        self.assertTrue(any(r["speaker"] for r in delta))
        self.assertTrue(any(not r["speaker"] for r in delta))

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            slots = tmp / "UserData"
            slots.mkdir()
            game = "素晴日"
            session_dir = tmp / folder(game).name
            session_dir.mkdir()
            database = session_dir / "save_history.sqlite3"
            env = dict(os.environ, VNCOMPANION_HOME=str(tmp), PYTHONIOENCODING="utf-8")

            def cli(action, *args):
                return subprocess.check_output([sys.executable, "-B", str(BASE/"vn.py"), action, game, *args],
                                               env=env, encoding="utf-8", timeout=10)

            shutil.copyfile(first_slot, slots / first_slot.name)
            watch = SaveHistory(slots, database)
            self.assertFalse(watch.poll())
            self.assertIn(old[-1]["text"].strip(), cli("history", "--limit=1"))
            self.assertEqual(history_page(database, 0, 1000), ([], 0, False))
            target = slots / next_slot.name
            target.write_bytes(b"incomplete")
            with self.assertRaises(ValueError):
                watch.poll()
            shutil.copyfile(next_slot, target)
            self.assertTrue(watch.poll())
            self.assertFalse(SaveHistory(slots, database).poll())
            delivered, cursor = [], 0
            while True:
                page = json.loads(cli("wait", "--timeout=0", "--json"))
                if not page["batch"]:
                    break
                self.assertEqual(page, json.loads(cli("read", "--json")))
                lines, cursor, _ = history_page(database, cursor, 3200)
                self.assertIn("\n".join(lines), page["text"])
                delivered.extend(lines)
                cli("reply", "--batch", page["batch"], "--title", "已看", "--body", "继续")
            self.assertEqual(len(delivered), 1 + len(delta))
            self.assertIn("新增", delivered[0])
            self.assertIn(new[-1]["text"].strip(), recent(database, 1))
