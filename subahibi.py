"""Decoder for the Chinese custom-font edition only."""
import json
import re
from pathlib import Path
MAPPING=json.loads(Path(__file__).with_name("char_mapping.json").read_text("utf-8"))

FRAME_HDR_RE = re.compile(r"\d+\.\d+\t\d+\t(\d+)\t([01])\t")


def is_specific_quote(qt: str) -> bool:
    """至少含5个有效汉字/字母数字才进跨场景说话人缓存，杜绝「……」/「呜！」/「什么？」张冠李戴"""
    return sum(1 for c in qt if ("\u4e00" <= c <= "\u9fff") or c.isalnum()) >= 5


def is_subahibi_raw(raw: str) -> bool:
    """校验是否为《素晴日》自定义字形编码文本（排除玩家平时 Ctrl+C 复制的普通中英文）"""
    if not raw:
        return False
    non_ws = [c for c in raw if not c.isspace()]
    if not non_ws:
        return False
    mapped_cnt = sum(1 for c in non_ws if c in MAPPING)
    if mapped_cnt / len(non_ws) < 0.85:
        return False
    return any(c in MAPPING and MAPPING[c] != c for c in non_ws)


def translate_and_split(raw: str) -> list[str]:
    """将原始字形还原为中文，并将快速滑滚轮合并的多句对话/旁白精准拆成独立行"""
    if not raw:
        return []
    # 剥离偶发串入的 TSV 帧头
    raw = FRAME_HDR_RE.split(raw)[0]
    raw = re.sub(r"[\r\n\t]+", " ", raw)
    raw = raw.replace("\uf8f5", "").replace("\ufffd", "")
    if not is_subahibi_raw(raw):
        return []

    tr = "".join(MAPPING.get(c, "" if 0x4E00 <= ord(c) <= 0x6A77 else c) for c in raw).rstrip()
    # 剥离尾部残留的时间戳碎数字
    tr = re.sub(r"\d{3,}\.\d{3}.*$", "", tr).rstrip()
    # 收拢游戏文本框自动折行产生的超长句中缩进（如「在看这里哦…              …」->「在看这里哦……」），保留2空格旁白缩进
    tr = re.sub(r"\s{6,}", "", tr)
    if tr.strip() in ("", "▽", "…"):
        return []

    # 拆分高速滑滚轮合并的全部 5 类句界：
    # 1) 连续台词：「……」「……」
    tr = re.sub(r"([」』）])\s*(?=[「『（])", r"\1\n", tr)
    # 2) 台词后接带冒号角色台词：「……」女子校生：  「……」
    tr = re.sub(r"([」』）])\s*(?=[^\s「『（【]{1,10}[：:]\s*[「『])", r"\1\n", tr)
    # 3) 台词后接旁白：「……」  蠢女人们消失了……
    tr = re.sub(r"([」』）])\s{2,}", r"\1\n  ", tr)
    # 4) 旁白后接台词：  然而……「要不是……」
    tr = re.sub(r"([。！？…—])\s*(?=[「『])", r"\1\n", tr)
    # 5) 连续多句旁白：  第一句旁白。  第二句旁白。
    tr = re.sub(r"([。！？…—])\s{2,}(?=[^\s「『▽])", r"\1\n  ", tr)

    lines = []
    for sub in tr.splitlines():
        s = re.sub(r"\s*▽+\s*$", "", sub).rstrip()
        if s.strip() and s.strip() not in ("▽", "…"):
            lines.append(s)
    return lines
