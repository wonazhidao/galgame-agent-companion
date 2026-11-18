"""Replay an original fast-reading scene against this or another checkout."""
import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from PIL import Image, ImageDraw


def replay(source):
    sys.path.insert(0, str(source.resolve()))
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, VNCOMPANION_SESSION=tmp):
        import companion_log as log
        from check_delta import run_check
        from save_history import SaveHistory
        from send_ai_reply import send

        script = [
            ("", "雨停以后，小站的灯一盏接一盏地亮了。"),
            ("阿遥", "我刚刚听到广播，说最后一班车会在这里多停一分钟。"),
            ("小澄", "那我们还有时间。你记不记得，那本蓝色的手账落在哪里了？"),
            ("阿遥", "嗯。"), ("小澄", "嗯。"),
            ("", "她把口袋翻过来，手心里只有两枚旧纽扣。"),
            ("小澄", "是不是放在售票窗口旁边了？我可以陪你回去找。"),
            ("阿遥", "我想起来了，刚才借铅笔的时候，我把它垫在了地图下面。"),
            ("", "隔着起雾的玻璃，能看见值班员在收拾桌上的东西。"),
            ("", "要是我们早点想起来就好了。"),
            ("小澄", "看，那扇门还开着。你去问，我留在这里看行李。"),
            ("阿遥", "找到了！原来他已经替我们把手账包好了。"),
        ]
        saved = [dict(key=str(i), speaker=s, text=t) for i, (s, t) in enumerate(script)]
        # OCR missed the pocket narration. Two speakers say the same word in a row.
        live = [row for i, row in enumerate(saved) if i != 5]
        watcher = SaveHistory(Path(tmp), log.HISTORY)
        slot = Path(tmp) / "SubarashikihibiHD001.sud"
        def poll(rows, stamp):
            slot.write_bytes(str(stamp).encode())
            os.utime(slot, (stamp, stamp))
            with patch("save_history.read_save", return_value=rows):
                watcher.poll()
        baseline = [dict(key="initial", speaker="", text="小站")]
        poll(baseline, 1000)
        texts, sheets = [], []
        def deliver():
            out = io.StringIO()
            with redirect_stdout(out):
                run_check(as_json=True)
            page = json.loads(out.getvalue())
            # Normalize only per-run paths and random batch IDs for tokenizer comparisons.
            texts.append(page["text"].replace(str(log.LOG_DIR), "demo").replace(page["batch"], "e312" * 8))
            with Image.open(log.LOG_DIR / "montage_latest.jpg") as im:
                sheets.append(list(im.size))
            send("已读", "继续", page["batch"])

        for begin, end in [(0, 5), (5, len(live))]:
            with log.TEXT.open("a", encoding="utf-8") as f:
                for index in range(begin, end):
                    seq, line = index + 1, live[index]["text"]
                    f.write(f"[12:00:00] [#{seq}] {line}\n")
                    if hasattr(watcher, "live"):
                        watcher.live.append((seq, line))
            # Three identical samples of each of two distinct drawings per batch.
            for i in range(6):
                im = Image.new("RGB", (240, 160), "#284050")
                ImageDraw.Draw(im).rectangle((50 + 2*(i//3), 60, 80, 90), fill="#efd9ae")
                seq = begin + 1 + (end - begin - 1) * i // 5
                im.save(log.LOG_DIR / f"shot_20261101_1200{begin:02d}_{i:06d}_s{seq:08d}.png")
            if begin:
                poll(baseline + saved, 1001)
                log.NOTES.write_text("刚才是谁先答应的？\n", "utf-8")
            deliver()
        result = dict(saved_lines=len(saved), live_lines=len(live), batches=len(texts),
                      text_characters=sum(map(len, texts)), image_sizes=sheets)
        try:
            import tiktoken
        except ImportError:
            pass
        else:
            encoding = tiktoken.get_encoding("o200k_base")
            result["o200k_text_tokens"] = sum(len(encoding.encode(t)) for t in texts)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent)
    print(json.dumps(replay(parser.parse_args().source), indent=2))
