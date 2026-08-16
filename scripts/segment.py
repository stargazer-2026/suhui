#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
segment.py — 按时间/对话间隙切段 + 统计摘要（§4.1 / §5.4 输入）

用法：
  python3 segment.py messages.json [--out <目录>]
      [--gap-minutes N]  按对话间隙切段（连续消息间隔 > N 分钟则切）
      [--max-messages N] 按消息数硬切（token 预算：每段 ≤ 模型上下文的一半）
      [-k N]             可选采样加速（非默认：均匀抽样 N 条）
      [--days N]         按自然日切段（每天一段）

默认：全量一段（不切），符合「默认全量」规格。

输出：
  segments.json — {"meta": {...}, "segments": [{"id","start","end","count","messages"}]}
  stats.json    — 统计摘要（消息长度分布、活跃时段、口癖频率、emoji 频率、
                  标点习惯、发送者比例、她的回复延迟等——蒸馏模板的输入）
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u2764\u2763"
    "\U0001F1E6-\U0001F1FF\u2702-\u27B0]"
)

# 中文口语高频词（口癖候选过滤用停用词，词列表）
STOPWORDS = set("""的了 是在 有 你 我 她 他 就 都 也 还 很 吧 吗 啊 呢 哦 嗯 呀 嘛 啦 呗 吧
这 那 什么 怎么 为什么 一个 没有 不是 还是 真的 知道 喜欢 觉得 想 现在 今天 明天
昨天 时候 东西 事情 朋友 这样 那样 一下 一点 的话 感觉 有点 就是 你说 在吗 算了
晚安 图片 看到 说起 其实 然后 因为 所以 但是 如果 虽然 或者 不过 已经 可以 可能
应该 一定 一样 一起 之后 之前 后来 最后 最近 一直 总是 经常 有时 每次 突然
""".split())


def load_messages(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "messages" in data:
        data = data["messages"]
    return data


def parse_ts(ts):
    """ISO 时间 → (datetime, epoch)；None/异常 → None。"""
    if not ts:
        return None
    try:
        import datetime
        dt = datetime.datetime.fromisoformat(str(ts))
        return dt, dt.timestamp()
    except (ValueError, TypeError):
        return None


def seg_gap_minutes(msgs, gap_min):
    """按对话间隙切段：连续消息间隔 > gap_min 分钟 → 新段。"""
    segs, cur = [], []
    prev = None
    for m in msgs:
        t = parse_ts(m.get("ts"))
        if prev is not None and t is not None:
            gap = (t[1] - prev[1]) / 60.0
            if gap > gap_min and cur:
                segs.append(cur)
                cur = []
        cur.append(m)
        if t:
            prev = t
    if cur:
        segs.append(cur)
    return segs


def seg_by_days(msgs):
    segs, cur, curday = [], [], None
    for m in msgs:
        t = parse_ts(m.get("ts"))
        day = t[0].strftime("%Y-%m-%d") if t else None
        if curday is not None and day != curday and cur:
            segs.append(cur)
            cur = []
        curday = day
        cur.append(m)
    if cur:
        segs.append(cur)
    return segs


def seg_by_count(msgs, n):
    return [msgs[i:i + n] for i in range(0, len(msgs), n)]


def sample_even(msgs, k):
    """均匀抽样 k 条（v2.1 P1-8）：保证 k 个不同索引（原 round 步长可能取重）。"""
    n = len(msgs)
    if n <= k:
        return msgs
    if k <= 1:
        return [msgs[0]]
    idxs = sorted({min(n - 1, int(round(i * (n - 1) / (k - 1)))) for i in range(k)})
    # 若取重导致不足 k 个：向最大空隙插入中点补齐
    while len(idxs) < k:
        gaps = [(idxs[i + 1] - idxs[i], i) for i in range(len(idxs) - 1)]
        _, i = max(gaps)
        mid = (idxs[i] + idxs[i + 1]) // 2
        if mid == idxs[i]:
            mid += 1
        idxs.insert(i + 1, min(n - 1, mid))
    return [msgs[i] for i in idxs]


# ---------- 统计 ----------
def _tokens(text):
    """口癖候选（v2 收窄，新14）：仅 2 字 bigram + 英文词。
    完整句不再进口癖候选（低频整句归「经典语录」top_quotes）。"""
    out = []
    t = re.sub(r"\s+", "", text or "")
    for m in re.finditer(r"[a-zA-Z0-9]+", t):
        out.append(m.group(0).lower())
    for m in re.finditer(r"[\u4e00-\u9fff]+", t):
        seg = m.group(0)
        L = len(seg)
        if L == 1:
            out.append(seg)
        else:
            for i in range(L - 1):
                w = seg[i:i + 2]
                if w not in STOPWORDS:
                    out.append(w)
    return out


def top_phrases(texts, topn=20, min_count=2):
    """口癖=高频短词/短语：min_count 过滤（频率 1 的整句不进口癖表）。"""
    cnt = Counter()
    for t in texts:
        for w in _tokens(t):
            cnt[w] += 1
    return [{"phrase": w, "count": c} for w, c in cnt.most_common(topn)
            if c >= min_count]


def top_quotes(texts, topn=10, min_len=4, max_len=30):
    """经典语录（新14）：低频但完整的句子（4-30 字），与口癖分开归类。"""
    cnt = Counter()
    for t in texts:
        s = re.sub(r"\s+", "", t or "")
        if min_len <= len(s) <= max_len:
            cnt[s] += 1
    return [{"quote": q, "count": c} for q, c in cnt.most_common(topn)]


def compute_stats(msgs):
    total = len(msgs)
    per_sender = Counter(m.get("sender") for m in msgs)
    per_platform = Counter(m.get("platform") or "unknown" for m in msgs)
    per_type = Counter(m.get("type") or "text" for m in msgs)

    ts_list = [parse_ts(m.get("ts")) for m in msgs]
    ts_list = [t for t in ts_list if t]
    span = None
    if ts_list:
        ts_list.sort(key=lambda x: x[1])
        span = {"start": ts_list[0][0].strftime("%Y-%m-%dT%H:%M:%S"),
                "end": ts_list[-1][0].strftime("%Y-%m-%dT%H:%M:%S")}

    hourly = Counter()
    weekday = Counter()
    for dt, _ in ts_list:
        hourly[dt.hour] += 1
        weekday[dt.strftime("%a")] += 1

    # 句长（按消息内句子/消息本身字符数）
    # v2（P0-4）：kind=placeholder 的消息（[图片]/[表情]…）不计入文本统计；
    # 另给 trimmed 中位数（排除 ≤1 字消息，避免超短消息拉低句长中位数）
    lengths = []
    emoji_cnt = Counter()
    punct_cnt = Counter()
    sender_texts = {"A": [], "B": []}
    sender_lens = {"A": [], "B": []}
    sender_lens_main = {"A": [], "B": []}   # 排除 ≤1 字
    per_kind = Counter()
    for m in msgs:
        kind = m.get("kind") or "text"
        per_kind[kind] += 1
        text = m.get("text") or ""
        L = len(re.sub(r"\s+", "", text))
        if kind == "placeholder":
            continue  # 占位符不参与文本统计（P0-4）
        if L > 0:
            lengths.append(L)
        s = m.get("sender")
        if s in ("A", "B"):
            sender_texts[s].append(text)
            sender_lens[s].append(L)
            if L > 1:
                sender_lens_main[s].append(L)
        for ch in text:
            if EMOJI_RE.match(ch):
                emoji_cnt[ch] += 1
            if ch in "。？！~…，、；：":
                punct_cnt[ch] += 1

    def hist(vals, buckets=(1, 5, 10, 20, 50)):
        h = {}
        for i in range(len(buckets)):
            lo = buckets[i]
            hi = buckets[i + 1] if i + 1 < len(buckets) else None
            key = "%d-%d" % (lo, hi) if hi else "%d+" % lo
            h[key] = sum(1 for v in vals if (hi is None or v < hi) and v >= lo)
        return h

    # 回复延迟（B 回复 A 的间隔，中位数）
    delays = []
    prev_sender, prev_ts = None, None
    for m in msgs:
        t = parse_ts(m.get("ts"))
        s = m.get("sender")
        if prev_ts is not None and t is not None and prev_sender and \
                prev_sender != s:
            if s == "B" and prev_sender == "A":
                delays.append(t[1] - prev_ts)
        if t:
            prev_ts = t[1]
        prev_sender = s

    # 主动发起比例：以长间隙（>6h）后的第一条消息为"主动开启对话"
    initiator = Counter()
    prev = None
    for m in msgs:
        t = parse_ts(m.get("ts"))
        if prev is not None and t is not None and (t[1] - prev) / 3600 > 6:
            initiator[m.get("sender")] += 1
        if t:
            prev = t[1]

    stats = {
        "total_messages": total,
        "per_sender": dict(per_sender),
        "per_platform": dict(per_platform),
        "per_type": dict(per_type),
        "per_kind": dict(per_kind),
        "span": span,
        "hourly_activity": dict(sorted(hourly.items())),
        "weekday_activity": dict(weekday),
        "message_length_histogram": hist(lengths),
        "message_length_percentiles": _percentiles(lengths),
        "emoji_frequency": dict(emoji_cnt.most_common(15)),
        "emoji_total": sum(emoji_cnt.values()),
        "punctuation_frequency": dict(punct_cnt.most_common(12)),
        "top_phrases": top_phrases([t for t in sender_texts["A"] + sender_texts["B"]]),
        "top_phrases_A": top_phrases(sender_texts["A"]),
        "top_phrases_B": top_phrases(sender_texts["B"]),
        "top_quotes": top_quotes([t for t in sender_texts["A"] + sender_texts["B"]]),
        "top_quotes_B": top_quotes(sender_texts["B"]),
        "sender_len_B": _percentiles(sender_lens["B"]),
        "sender_len_A": _percentiles(sender_lens["A"]),
        "sender_len_B_main": _percentiles(sender_lens_main["B"]),
        "reply_delay_seconds": _percentiles(delays) if delays else None,
        "conversation_initiators": dict(initiator),
        "night_ratio_B": _night_ratio(msgs, "B"),
    }
    return stats


def _percentiles(vals, ps=(10, 25, 50, 75, 90)):
    if not vals:
        return None
    vals = sorted(vals)
    out = {}
    n = len(vals)
    for p in ps:
        out[str(p)] = vals[min(n - 1, int(n * p / 100.0))]
    out["n"] = n
    return out


def _night_ratio(msgs, sender):
    """该发送者在深夜（22-2 点）的消息占比。"""
    n_total, n_night = 0, 0
    for m in msgs:
        if m.get("sender") != sender:
            continue
        t = parse_ts(m.get("ts"))
        if not t:
            continue
        n_total += 1
        if t[0].hour >= 22 or t[0].hour < 2:
            n_night += 1
    return round(n_night / n_total, 3) if n_total else None


def build_segments(msgs, mode, gap_min, max_msgs, k):
    if k and len(msgs) > k:
        msgs = sample_even(msgs, k)
        mode = "sampled-%d" % k
    if mode == "gap":
        chunks = seg_gap_minutes(msgs, gap_min)
    elif mode == "day":
        chunks = seg_by_days(msgs)
    elif mode == "count":
        chunks = seg_by_count(msgs, max_msgs)
    else:
        chunks = [list(msgs)]
    segs = []
    for i, chunk in enumerate(chunks):
        ts_list = [parse_ts(m.get("ts")) for m in chunk if m.get("ts")]
        ts_list = [t for t in ts_list if t]
        start = ts_list[0][0].strftime("%Y-%m-%dT%H:%M:%S") if ts_list else None
        end = ts_list[-1][0].strftime("%Y-%m-%dT%H:%M:%S") if ts_list else None
        segs.append({"id": i, "start": start, "end": end,
                     "count": len(chunk), "messages": chunk})
    return segs, mode


def main(argv=None):
    ap = argparse.ArgumentParser(description="溯洄 · 按时间切段 + 统计摘要（§4.1）")
    ap.add_argument("messages", help="messages.json（§5.1 标准消息流）")
    ap.add_argument("--out", default=".", help="输出目录（默认当前目录）")
    ap.add_argument("--gap-minutes", type=int, default=None,
                    help="按对话间隙切段：间隔 > N 分钟切新段")
    ap.add_argument("--max-messages", type=int, default=None,
                    help="按消息数硬切（token 预算：每段 ≤ 模型上下文的一半）")
    ap.add_argument("--days", action="store_true",
                    help="（已弃用，等价 --active-gap）按活跃间隔切段："
                         "23:00-01:00 的跨午夜连续对话不切断（v2 语义）")
    ap.add_argument("--active-gap", action="store_true",
                    help="按活跃间隔切段（默认 6 小时；跨午夜连续对话不切断，"
                         "避免损失深夜场景样本）")
    ap.add_argument("--active-gap-minutes", type=int, default=360,
                    help="活跃间隔分钟数（默认 360=6h；连续对话中断超过该间隔才切新段，"
                         "跨午夜连续对话不切断）")
    ap.add_argument("-k", type=int, default=0, help="可选采样加速（均匀抽样 N 条，非默认）")
    args = ap.parse_args(argv)

    msgs = load_messages(args.messages)
    if not msgs:
        sys.stderr.write("messages.json 为空\n")
        return 1

    if args.days or args.active_gap:
        # v2（P1-8）：按活跃间隔切段——跨午夜连续对话（23:00-01:00）不切断，
        # 深夜场景样本保留（场景化 when→behavior 规则依赖深夜样本）
        mode = "gap"
        gap_min = args.active_gap_minutes
    elif args.gap_minutes:
        mode = "gap"
        gap_min = args.gap_minutes
    elif args.max_messages:
        mode = "count"
        gap_min = 720
    else:
        mode = "full"
        gap_min = 720

    segs, eff_mode = build_segments(msgs, mode, gap_min,
                                    args.max_messages or 2000, args.k)
    stats = compute_stats(msgs)

    os.makedirs(args.out, exist_ok=True)
    seg_path = os.path.join(args.out, "segments.json")
    stat_path = os.path.join(args.out, "stats.json")
    with open(seg_path, "w", encoding="utf-8") as f:
        json.dump({"meta": {"total": len(msgs), "segmentation": eff_mode,
                            "segments": len(segs),
                            "span": stats["span"]},
                   "segments": segs}, f, ensure_ascii=False, indent=2)
    with open(stat_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    print("切段完成：%d 条消息 → %d 段（模式: %s）" % (len(msgs), len(segs), eff_mode))
    print("时间跨度: %s → %s" % (stats["span"]["start"] if stats["span"] else "?",
                                stats["span"]["end"] if stats["span"] else "?"))
    print("发送者: %s" % json.dumps(stats["per_sender"], ensure_ascii=False))
    print("输出: %s / %s" % (seg_path, stat_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
