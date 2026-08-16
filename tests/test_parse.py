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


# ---------- 去重（P0-3） ----------
def test_dedup_same_platform():
    msgs = [
        {"platform": "wechat", "ts": "2030-01-01T10:00:00", "sender": "A",
         "text": "在吗"},
        {"platform": "wechat", "ts": "2030-01-01T10:00:00", "sender": "A",
         "text": "在吗"},  # 重复
        {"platform": "wechat", "ts": "2030-01-01T10:01:00", "sender": "A",
         "text": "在吗"},  # ts 不同 → 保留
    ]
    out, removed = parse.dedup_messages(msgs)
    assert removed == 1
    assert len(out) == 2


def test_dedup_cross_platform_kept():
    """跨平台不去重（多面性素材）"""
    msgs = [
        {"platform": "wechat", "ts": "2030-01-01T10:00:00", "sender": "A",
         "text": "在吗"},
        {"platform": "telegram", "ts": "2030-01-01T10:00:00", "sender": "A",
         "text": "在吗"},
    ]
    out, removed = parse.dedup_messages(msgs)
    assert removed == 0
    assert len(out) == 2


# ---------- kind（P0-4） ----------
def test_kind_placeholder():
    assert parse.detect_kind("[图片]") == "placeholder"
    assert parse.detect_kind("[表情]") == "placeholder"
    assert parse.detect_kind("[语音]") == "placeholder"


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
    assert parse.parse_iso("2023-09-27T21:00:00+08:00") == "2023-09-27T13:00:00"
    assert parse.parse_iso("2023-09-27T21:00:00Z") == "2023-09-27T21:00:00"


def test_timestamp_tz_line():
    m = parse.TS_LINE_RE.match("2023-09-27T21:00:00+08:00 __NAME__: 在吗")
    assert m is not None
    ts = parse.fmt_ts(*m.groups()[:6], tz=m.groups()[6])
    assert ts == "2023-09-27T13:00:00"


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
    assert len(data) == 1  # 默认去重
    meta = json.loads((out / "parse_meta.json").read_text(encoding="utf-8"))
    assert any("去重" in w for w in meta["warnings"])


def test_cli_no_dedup(tmp_path):
    src = tmp_path / "dup.txt"
    src.write_text("[2030-01-01 10:00] __NAME__: 在吗\n"
                   "[2030-01-01 10:00] __NAME__: 在吗\n", encoding="utf-8")
    out = tmp_path / "out2"
    rc = parse.main([str(src), "--out", str(out), "--no-dedup"])
    assert rc == 0
    data = json.loads((out / "messages.json").read_text(encoding="utf-8"))
    assert len(data) == 2  # 关闭去重
