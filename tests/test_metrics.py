# -*- coding: utf-8 -*-
"""metrics.py 单测（P2-12）——KL/JS/emoji 率/前缀命中。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import metrics  # noqa: E402


# ---------- 口癖分布 KL ----------
def test_kl_identity_zero():
    texts = ["晚安", "晚安", "在吗", "睡了吗"]
    p, _ = metrics.phrase_dist(texts)
    q, _ = metrics.phrase_dist(texts)
    assert metrics.symmetric_kl(p, q) == 0.0


def test_kl_symmetric():
    t1 = ["晚安"] * 8 + ["在吗"] * 2
    t2 = ["晚安"] * 3 + ["在吗"] * 7
    p, _ = metrics.phrase_dist(t1)
    q, _ = metrics.phrase_dist(t2)
    assert metrics.kl(p, q) != metrics.kl(q, p)  # KL 不对称
    assert metrics.symmetric_kl(p, q) == metrics.symmetric_kl(q, p)  # 对称化


def test_kl_different_is_larger():
    same, _ = metrics.phrase_dist(["晚安"] * 10)
    other, _ = metrics.phrase_dist(["在吗"] * 10)
    assert metrics.symmetric_kl(same, same) < metrics.symmetric_kl(same, other)


# ---------- 句长 JS 距离 ----------
def test_js_identity_zero():
    texts = ["晚安", "今天好累啊", "嗯", "你也是"]
    p, _ = metrics.len_hist(texts)
    q, _ = metrics.len_hist(texts)
    assert metrics.js_distance(p, q) == 0.0


def test_js_bounds():
    p, _ = metrics.len_hist(["晚安", "嗯"])
    q, _ = metrics.len_hist(["今天好累啊，加班到九点，_(:з」∠)_"] * 5)
    d = metrics.js_distance(p, q)
    assert 0.0 <= d <= 1.0


# ---------- emoji 频率 ----------
def test_emoji_rate():
    assert metrics.emoji_rate(["😂😂", "文字"]) > 0
    assert metrics.emoji_rate(["没有表情的文字"]) == 0.0
    assert metrics.emoji_rate(["😂😂😂"]) > metrics.emoji_rate(["😂文字"])


# ---------- 前缀预测命中率（检索式） ----------
def test_prefix_hit():
    msgs = [
        {"ts": "2030-01-01T10:00:00", "sender": "A", "text": "在吗"},
        {"ts": "2030-01-01T10:01:00", "sender": "B", "text": "在呢"},
        {"ts": "2030-01-01T10:02:00", "sender": "A", "text": "睡了吗"},
        {"ts": "2030-01-01T10:03:00", "sender": "B", "text": "还没"},
        {"ts": "2030-01-01T10:04:00", "sender": "A", "text": "在吗"},
        {"ts": "2030-01-01T10:05:00", "sender": "B", "text": "在呢"},
    ]
    hits, n = metrics.prefix_hit_rate(msgs, "B", topks=(1, 5))
    assert n == 2
    assert 0 <= hits[1] <= 1.0
    assert hits[5] >= hits[1]  # Top-5 命中率不低于 Top-1


def test_prefix_hit_no_data():
    msgs = [{"ts": "2030-01-01T10:00:00", "sender": "A", "text": "在吗"}]
    hits, n = metrics.prefix_hit_rate(msgs, "B")
    assert n == 0
    assert hits[1] is None
