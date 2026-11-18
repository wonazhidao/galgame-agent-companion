"""Read saved backlog records from the verified SubaHibi HD Chinese build."""
import hashlib
from functools import reduce
from operator import xor
from pathlib import Path
import struct

from subahibi import MAPPING


BUILD_SHA256 = "6eda794baf07d25092b640f8221ac93da810c144abd9f5d06832868187f97488"
RECORD_SIZE = 88124
SAVE_HEADER = 230656
MAX_SDC = 4 * 1024 * 1024
GLYPHS = str.maketrans(MAPPING)


def supports(exe):
    path = Path(exe)
    if path.name.casefold() != "bgi-chs.exe" or path.stat().st_size != 59940864:
        return False
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest() == BUILD_SHA256


def sdc_payload(encoded):
    if len(encoded) < 32 or encoded[:16] != b"SDC FORMAT 1.00\0":
        raise ValueError("存档的 SDC 文件头不匹配")
    seed, packed_size, unpacked_size, checksum, parity = struct.unpack_from("<IIIHH", encoded, 16)
    packed = bytearray(encoded[32:])
    if len(packed) != packed_size or unpacked_size > MAX_SDC:
        raise ValueError("存档大小不匹配")
    if sum(packed) % 65536 != checksum or reduce(xor, packed, 0) != parity:
        raise ValueError("存档校验失败，可能仍在写入")
    for i, value in enumerate(packed):
        product = seed * 0x015A4E35
        packed[i] = (value - (product >> 16)) & 255
        seed = (product + 1) & 0xffffffff
    data = bytearray()
    pos = 0
    while pos < len(packed):
        control = packed[pos]
        pos += 1
        if control < 128:
            end = pos + control + 1
            if end > len(packed):
                raise ValueError("SDC 原文块不完整")
            data.extend(packed[pos:end])
            pos = end
        else:
            if pos >= len(packed):
                raise ValueError("SDC 重复块不完整")
            distance = ((control & 7) << 8) + packed[pos] + 2
            pos += 1
            if distance > len(data):
                raise ValueError("SDC 重复块位置无效")
            for _ in range(2 + ((control >> 3) & 15)):
                data.append(data[-distance])
        if len(data) > unpacked_size:
            raise ValueError("SDC 解包长度超出声明")
    if len(data) != unpacked_size:
        raise ValueError("SDC 解包不完整")
    return data


def records(data):
    if len(data) < 24 or data[:16] != b"DCFS FORMAT 1.00":
        raise ValueError("缺少 DCFS 回看记录")
    size, count = struct.unpack_from("<II", data, 16)
    if size != RECORD_SIZE or not 1 <= count <= 401 or len(data) < 24 + size:
        raise ValueError("未经验证的回看记录布局")
    pos = 24 + size
    record = data[24:pos].copy()
    yield record
    for _ in range(count - 1):
        offset = 0
        literal = False
        while offset < size:
            length = 0
            for shift in (0, 7, 14):
                if pos >= len(data):
                    raise ValueError("DCFS 差分记录不完整")
                code = data[pos]
                pos += 1
                length |= (code & 127) << shift
                if code < 128:
                    break
            else:
                raise ValueError("DCFS 段长度无效")
            if offset + length > size:
                raise ValueError("DCFS 段超出记录范围")
            if literal:
                if pos + length > len(data):
                    raise ValueError("DCFS 字段不完整")
                record[offset:offset+length] = data[pos:pos+length]
                pos += length
            offset += length
            literal = not literal
        yield record
    if pos != len(data):
        raise ValueError("DCFS 回看表尾部不匹配")


def read_save(path):
    with path.open("rb") as f:
        f.seek(SAVE_HEADER)
        encoded = f.read(MAX_SDC + 1)
    if len(encoded) > MAX_SDC:
        raise ValueError("存档超过支持的大小")
    rows = []
    for record in records(sdc_payload(encoded)):
        length = struct.unpack_from("<I", record, 0x123e4)[0]
        segments = struct.unpack_from("<I", record, 0x11ce0)[0]
        if not length and not segments:  # Current engine state after the last completed line.
            continue
        if not 0 < length <= 0x1000 or not 1 <= segments <= 32:
            raise ValueError("存档中的台词字段超出范围")
        pool = record[0x123e8:0x123e8+length]

        def string_at(field):
            offset = struct.unpack_from("<i", record, field)[0]
            end = pool.find(0, offset) if 0 <= offset < length else -1
            if end < 0:
                raise ValueError("存档台词指针无效")
            return pool[offset:end].decode("gbk").translate(GLYPHS)

        speaker = "" if struct.unpack_from("<i", record, 0x11cd8)[0] == -1 else string_at(0x11cd8)
        text = "".join(string_at(0x11d00 + 0x38*j) for j in range(segments))
        rows.append(dict(key=hashlib.sha256(record).hexdigest(), speaker=speaker, text=text))
    return rows
