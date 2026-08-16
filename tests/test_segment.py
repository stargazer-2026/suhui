# -*- coding: utf-8 -*-
"""segment.py 单测（v2.1）——sample_even 样本数保证 + 跨午夜活跃间隔切段。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import segment  # noqa: E402


# ---------- sample_even（P1-8） ----------
def test_sample_even_counts():
    """保证 k 个不同索引（原 round 步长可能取重）。"""
    msgs = [{"id": i} for i in range(100)]
    for k in (1, 2, 3, 7, 50, 99, 100):
        out = segment.sample_even(msgs, k)
        assert len(out) == k, "k=%d 应取 %d 条，实际 %d" % (k, k, len(out))
        idxs = [m["id"] for m in out]
        assert len(set(idxs)) == k, "索引应互不相同"


def test_sample_even_edges():
    assert segment.sample_even([], 3) == []
    assert segment.sample_even([{"id": 0}], 5) == [{"id": 0}]
    assert segment.sample_even([{"id": 0}, {"id": 1}], 2) == [{"id": 0}, {"id": 1}]


# ---------- 跨午夜活跃间隔切段（v2/P1-8） ----------
def test_active_gap_midnight():
    msgs = [
        {"ts": "2030-01-01T23:50:00", "sender": "A", "text": "睡了吗"},
        {"ts": "2030-01-01T23:55:00", "sender": "B", "text": "还没"},
        {"ts": "2030-01-02T00:02:00", "sender": "A", "text": "聊聊"},
        {"ts": "2030-01-02T00:10:00", "sender": "B", "text": "嗯嗯"},
        {"ts": "2030-01-02T09:00:00", "sender": "A", "text": "早"},
    ]
    segs = segment.seg_gap_minutes(msgs, 360)  # 活跃间隔 6h
    assert len(segs) == 2
    assert len(segs[0]) == 4  # 跨午夜 23:50-00:10 不切断
    assert len(segs[1]) == 1  # 次日早晨独立成段


def test_active_gap_continuous_day():
    """同一天内间隔 < 6h 的对话不切。"""
    msgs = [
        {"ts": "2030-01-01T10:00:00", "sender": "A", "text": "在吗"},
        {"ts": "2030-01-01T12:00:00", "sender": "B", "text": "在"},
        {"ts": "2030-01-01T14:00:00", "sender": "A", "text": "好"},
    ]
    segs = segment.seg_gap_minutes(msgs, 360)
    assert len(segs) == 1
