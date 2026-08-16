# -*- coding: utf-8 -*-
"""parse.py 单测（P2-12）——全部使用占位符数据（铁律：零真实数据）。"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import parse  # noqa: E402


# ---------- 编码 ----------
def test_encoding_utf8_bom(tmp_path):
    p = tmp_path / "chat.txt"
    p.write_bytes("\ufeff[2030-01-01 10:00] __NAME__: 你好\n".encode("utf-8"))
    msgs = parse._parse_txt_lines(parse.open_text_stream(str(p)))
    assert len(msgs) == 1
    assert msgs[0]["text"] == "你好"


def test_encoding_gbk(tmp_path):
    p = tmp_path / "chat_gbk.txt"
    raw = "[2030-01-01 10:00] __NAME__: 早上好（GBK测试）\n".encode("gbk")
    p.write_bytes(raw)
    msgs = parse._parse_txt_lines(parse.open_text_stream(str(p)))
    assert msgs[0]["text"].startswith("早上好")


# ---------- 去重（v2.1 P0-1：跨文件重叠去重） ----------
def test_dedup_same_file_kept():
    """同文件内重复保留（真实对话：同分钟连发相同文本不被误杀）"""
    msgs = [
        {"platform": "wechat", "ts": "2030-01-01T10:00:00", "sender": "A",
         "text": "晚安", "file_id": 0},
        {"platform": "wechat", "ts": "2030-01-01T10:00:00", "sender": "A",
         "text": "晚安", "file_id": 0},  # 同文件重复 → 保留
        {"platform": "wechat", "ts": "2030-01-01T10:01:00", "sender": "A",
         "text": "晚安", "file_id": 0},
    ]
    out, removed = parse.dedup_messages(msgs)
    assert removed == 0
    assert len(out) == 3


def test_dedup_cross_file():
    """跨文件（重复导出）重叠 → 去重；同文件内重复保留"""
    msgs = [
        # 文件0：同 key 出现两次（同文件重复 → 都保留）
        {"platform": "wechat", "ts": "2030-01-01T10:00:00", "sender": "A",
         "text": "在吗", "file_id": 0},
        {"platform": "wechat", "ts": "2030-01-01T10:00:00", "sender": "A",
         "text": "在吗", "file_id": 0},
        # 文件1：与文件0 重叠（重复导出）→ 文件1 的该 key 全部去重
        {"platform": "wechat", "ts": "2030-01-01T10:00:00", "sender": "A",
         "text": "在吗", "file_id": 1},
    ]
    out, removed = parse.dedup_messages(msgs)
    assert removed == 1
    assert len(out) == 2
    assert all(m["file_id"] == 0 for m in out)


def test_dedup_no_file_id_kept():
    """无 file_id 保守保留"""
    msgs = [
        {"platform": "wechat", "ts": "2030-01-01T10:00:00", "sender": "A",
         "text": "在吗"},
        {"platform": "wechat", "ts": "2030-01-01T10:00:00", "sender": "A",
         "text": "在吗"},
    ]
    out, removed = parse.dedup_messages(msgs)
    assert removed == 0
    assert len(out) == 2


def test_dedup_cross_platform_kept():
    """跨平台不去重（多面性素材）"""
    msgs = [
        {"platform": "wechat", "ts": "2030-01-01T10:00:00", "sender": "A",
         "text": "在吗", "file_id": 0},
        {"platform": "telegram", "ts": "2030-01-01T10:00:00", "sender": "A",
         "text": "在吗", "file_id": 1},
    ]
    out, removed = parse.dedup_messages(msgs)
    assert removed == 0
    assert len(out) == 2


# ---------- kind（P0-4） ----------
def test_kind_placeholder():
    assert parse.detect_kind("[图片]") == "placeholder"
    assert parse.detect_kind("[表情]") == "placeholder"
    assert parse.detect_kind("[语音]") == "placeholder"


def test_kind_placeholder_sequence():
    """v2.1（P1-7）：连发占位符序列整条判 placeholder"""
    assert parse.detect_kind("[图片][图片]") == "placeholder"
    assert parse.detect_kind("[表情][语音][图片]") == "placeholder"


def test_kind_media_message():
    """v2.1（P0-6）：媒体消息（照片节点/媒体占位）kind=placeholder 不进文本统计"""
    m = parse.make_msg("2030-01-01T10:00:00", None, "照片: img_001.tif",
                       "image", "photos")
    assert m["kind"] == "placeholder"
    m2 = parse.make_msg("2030-01-01T10:00:00", "A", "[photo_1.jpg]",
                        "file", "telegram")
    assert m2["kind"] == "placeholder"


def test_kind_emoji():
    assert parse.detect_kind("😂😭") == "emoji"
    assert parse.detect_kind("_(:з」∠)_") == "emoji"


def test_kind_text():
    assert parse.detect_kind("今天好累啊") == "text"
    assert parse.detect_kind("") == "text"


def test_kind_in_message(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text("[2030-01-01 10:00] A: [图片]\n[2030-01-01 10:01] B: 哈哈\n",
                 encoding="utf-8")
    msgs = parse.parse_txt_lines(p.read_text(encoding="utf-8"))
    assert msgs[0]["kind"] == "placeholder"
    assert msgs[1]["kind"] == "text"


# ---------- 时间戳格式（P1-7） ----------
def test_timestamp_chinese():
    m = parse.TS_LINE_RE.match("2023年9月27日 21:00 __NAME__: 在吗")
    assert m is not None
    assert parse.fmt_ts(*m.groups()[:6]) == "2023-09-27T21:00:00"


def test_timestamp_slash():
    m = parse.TS_LINE_RE.match("2023/09/27 21:00 __NAME__: 在吗")
    assert m is not None


def test_timestamp_tz_iso():
    """v2.1（P0-4）：保留本地时间原样输出，不转 UTC"""
    assert parse.parse_iso("2023-09-27T21:00:00+08:00") == "2023-09-27T21:00:00"
    assert parse.parse_iso("2023-09-27T21:00:00Z") == "2023-09-27T21:00:00"


def test_timestamp_tz_line():
    m = parse.TS_LINE_RE.match("2023-09-27T21:00:00+08:00 __NAME__: 在吗")
    assert m is not None
    ts = parse.fmt_ts(*m.groups()[:6], tz=m.groups()[6])
    assert ts == "2023-09-27T21:00:00"  # 本地时间保留（深夜统计不偏移）


# ---------- 多行合并 + 流式（P1-6） ----------
def test_multiline_message():
    text = "[2030-01-01 10:00] __NAME__: 第一行\n第二行\n[2030-01-01 10:01] 我: 好\n"
    msgs = parse.parse_txt_lines(text)
    assert len(msgs) == 2
    assert msgs[0]["text"] == "第一行\n第二行"


def test_stream_large():
    """5 万行流式解析：不爆内存、结果正确（O(n) 拼接）。"""
    lines = []
    for i in range(25000):
        lines.append("[2030-01-01 10:00] __NAME__: 第%d条" % i)
        lines.append("续行%d" % i)
    msgs = parse._parse_txt_lines(iter(lines))
    assert len(msgs) == 25000
    assert msgs[0]["text"] == "第0条\n续行0"
    assert msgs[-1]["text"] == "第24999条\n续行24999"


def test_single_timestamp_not_fallback(tmp_path):
    """v2.1：仅 1 条时间戳消息的文件不被 plain_txt fallback 覆盖（时间戳保留）"""
    src = tmp_path / "one.txt"
    src.write_text("2023-09-27T21:00:00+08:00 __NAME__: 在吗\n", encoding="utf-8")
    out = tmp_path / "out_one"
    rc = parse.main([str(src), "--out", str(out)])
    assert rc == 0
    data = json.loads((out / "messages.json").read_text(encoding="utf-8"))
    assert data[0]["ts"] == "2023-09-27T21:00:00"  # 保留本地时间
    assert data[0]["text"] == "在吗"


def test_plain_text_fallback_still_works(tmp_path):
    """纯文本（无时间戳）仍走 fallback"""
    src = tmp_path / "plain.txt"
    src.write_text("__NAME__: 在吗\n我: 在\n", encoding="utf-8")
    out = tmp_path / "out_plain"
    rc = parse.main([str(src), "--out", str(out)])
    assert rc == 0
    data = json.loads((out / "messages.json").read_text(encoding="utf-8"))
    assert len(data) == 2
    assert all(m["ts"] is None for m in data)


# ---------- 端到端 CLI ----------
def test_cli_dedup_flag(tmp_path, capsys):
    src = tmp_path / "dup.txt"
    src.write_text("[2030-01-01 10:00] __NAME__: 在吗\n"
                   "[2030-01-01 10:00] __NAME__: 在吗\n", encoding="utf-8")
    out = tmp_path / "out"
    rc = parse.main([str(src), "--out", str(out), "--map",
                     '{"__NAME__":"B"}'])
    assert rc == 0
    data = json.loads((out / "messages.json").read_text(encoding="utf-8"))
    # v2.1（P1-15）：单文件导入 → 同文件内重复保留（真实对话），不去重
    assert len(data) == 2
    assert all(m.get("file_id") == 0 for m in data)
    meta = json.loads((out / "parse_meta.json").read_text(encoding="utf-8"))
    assert not any("去重" in w for w in meta["warnings"])  # 单文件无跨文件重叠


def test_cli_dedup_cross_file(tmp_path):
    """跨文件重叠去重：同一文件传两次 → 第二次的内容被去重"""
    src = tmp_path / "dup.txt"
    src.write_text("[2030-01-01 10:00] __NAME__: 在吗\n"
                   "[2030-01-01 10:00] __NAME__: 在吗\n", encoding="utf-8")
    out = tmp_path / "out3"
    rc = parse.main([str(src), str(src), "--out", str(out), "--map",
                     '{"__NAME__":"B"}'])
    assert rc == 0
    data = json.loads((out / "messages.json").read_text(encoding="utf-8"))
    # 文件0 两条 + 文件1 两条（重叠两条被杀）→ 共 2 条
    assert len(data) == 2
    meta = json.loads((out / "parse_meta.json").read_text(encoding="utf-8"))
    assert any("跨文件重叠" in w for w in meta["warnings"])


def test_cli_no_dedup(tmp_path):
    src = tmp_path / "dup.txt"
    src.write_text("[2030-01-01 10:00] __NAME__: 在吗\n"
                   "[2030-01-01 10:00] __NAME__: 在吗\n", encoding="utf-8")
    out = tmp_path / "out2"
    rc = parse.main([str(src), "--out", str(out), "--no-dedup"])
    assert rc == 0
    data = json.loads((out / "messages.json").read_text(encoding="utf-8"))
    assert len(data) == 2  # 关闭去重
