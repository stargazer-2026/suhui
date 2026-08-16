#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
metrics.py — 像度客观指标（§0 验收 8 / §4.33 评测基准，全部可数值化）

用法：
  python3 metrics.py --real messages.json --generated generated.json [--her B]
      [--worldtree entity_clusters.json] [--out report.md]

指标（全部客观、可对比、可迭代）：
  1. 口癖分布距离：双方高频词分布之间的 KL 散度（对称化）
  2. 句长分布距离：句长直方图的 Jensen-Shannon 距离
  3. emoji 频率差：每 100 字符 emoji 出现次数之差
  4. 前缀预测命中率：真实对话切出「前缀 → 她的下一条消息」gold，
     检索式预测（字面 2-gram + 世界树标签混合打分）取 Top-k，命中率 = gold 是否在 Top-k
     （离线代理指标；完整生成式 Top-k 需要 API 蒸馏，见 §4.33）

另外输出：样本量、覆盖说明（哪些指标因数据不足跳过）。
"""
import argparse
import json
import math
import re
import sys
from collections import Counter

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u2764\u2763"
    "\U0001F1E6-\U0001F1FF\u2702-\u27B0]")


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "messages" in data:
        data = data["messages"]
    return data


def texts_of(msgs, sender):
    return [(m.get("text") or "") for m in msgs if m.get("sender") == sender]


def tokens(text):
    out = []
    t = re.sub(r"\s+", "", text or "")
    for m in re.finditer(r"[a-zA-Z0-9]+", t):
        out.append(m.group(0).lower())
    for m in re.finditer(r"[\u4e00-\u9fff]+", t):
        seg = m.group(0)
        if len(seg) == 1:
            out.append(seg)
        else:
            for i in range(len(seg) - 1):
                out.append(seg[i:i + 2])
            out.append(seg)
    return out


def phrase_dist(texts, topn=30):
    cnt = Counter()
    for t in texts:
        for w in tokens(t):
            cnt[w] += 1
    total = sum(cnt.values())
    top = [w for w, _ in cnt.most_common(topn) if total > 0]
    dist = {}
    for w in top:
        dist[w] = cnt[w] / total
    return dist, total


def kl(p, q):
    """KL(p||q)，带平滑（避免除零）。"""
    s = 0.0
    keys = set(p) | set(q)
    for k in keys:
        pv = p.get(k, 0.0) + 1e-9
        qv = q.get(k, 0.0) + 1e-9
        s += pv * math.log(pv / qv)
    return s


def symmetric_kl(p, q):
    return 0.5 * (kl(p, q) + kl(q, p))


def len_hist(texts, buckets=(1, 5, 10, 20, 50, 100)):
    h = Counter()
    for t in texts:
        L = len(re.sub(r"\s+", "", t or ""))
        if L <= 0:
            continue
        for i in range(len(buckets)):
            lo = buckets[i]
            hi = buckets[i + 1] if i + 1 < len(buckets) else None
            if (hi is None or L < hi) and L >= lo:
                h[i] += 1
                break
    total = sum(h.values())
    if total == 0:
        return {}, 0
    return {i: c / total for i, c in h.items()}, total


def js_distance(p, q):
    """Jensen-Shannon 距离（对称、有界 0~1）。"""
    keys = set(p) | set(q)
    m = {k: 0.5 * (p.get(k, 0.0) + q.get(k, 0.0)) for k in keys}
    return math.sqrt(0.5 * (kl(p, m) + kl(q, m)))


def emoji_rate(texts):
    n_emoji = sum(len(EMOJI_RE.findall(t or "")) for t in texts)
    n_char = sum(len(re.sub(r"\s+", "", t or "")) for t in texts)
    return (n_emoji / n_char * 100.0) if n_char else 0.0


# ---------- 前缀预测命中率（检索式代理，§4.33） ----------
def char_bigrams(text):
    t = re.sub(r"\s+", "", text or "").lower()
    if len(t) <= 1:
        return {t} if t else set()
    return set(t[i:i + 2] for i in range(len(t) - 1))


def build_index(her_texts):
    idx = []
    for i, t in enumerate(her_texts):
        if t.strip():
            idx.append((i, t, char_bigrams(t)))
    return idx


def retrieve(query_text, index, wt=None, topk=5):
    """打分 = 字面 2-gram Jaccard（+世界树标签分，若有）。"""
    qbg = char_bigrams(query_text)
    scored = []
    for i, t, bg in index:
        union = qbg | bg
        sim = len(qbg & bg) / max(1, len(union))
        if sim <= 0:
            continue
        scored.append((sim, i, t))
    scored.sort(key=lambda x: -x[0])
    return scored[:topk]


def prefix_hit_rate(msgs, her, topks=(1, 3, 5)):
    """真实对话切「前缀(最近3条) → 她的下一条」gold；检索 Top-k 是否命中 gold 文本。"""
    her_texts = texts_of(msgs, her)
    index = build_index(her_texts)
    golds = []
    win = []
    for m in msgs:
        if m.get("sender") == her and (m.get("text") or "").strip() and len(win) >= 2:
            golds.append((" ".join(x.get("text", "") for x in win),
                          m.get("text", "")))
        win = (win + [m]) if len(win) < 3 else (win[1:] + [m])
    hits = {k: 0 for k in topks}
    for prefix, gold in golds:
        res = retrieve(prefix, index)
        gbg = char_bigrams(gold)
        for k in topks:
            cands = res[:k]
            if any(char_bigrams(t) & gbg for _, _, t in cands) or \
               any(gold[:20] in t or t[:20] in gold for _, _, t in cands):
                hits[k] += 1
    n = len(golds)
    return {k: (v / n if n else None) for k, v in hits.items()}, n


# ---------- 主流程 ----------
def main(argv=None):
    ap = argparse.ArgumentParser(description="溯洄 · 像度客观指标（§0 验收 8）")
    ap.add_argument("--real", required=True, help="真实消息 messages.json")
    ap.add_argument("--generated", required=True, help="生成消息 JSON（她的风格句子列表）")
    ap.add_argument("--her", default="B", help="她的发送者标识（默认 B）")
    ap.add_argument("--worldtree", default="", help="可选：entity_clusters.json（世界树标签加分）")
    ap.add_argument("--out", default="", help="报告输出路径（默认打印）")
    args = ap.parse_args(argv)

    real = load(args.real)
    gen_raw = load(args.generated)
    if isinstance(gen_raw, dict):
        gen = []
        for k in ("messages", "generated", "sentences"):
            if isinstance(gen_raw.get(k), list):
                gen = [x.get("text") if isinstance(x, dict) else str(x)
                       for x in gen_raw[k]]
                break
    else:
        gen = [x.get("text") if isinstance(x, dict) else str(x) for x in gen_raw]

    her_real = texts_of(real, args.her)
    report = []
    report.append("# 像度客观指标报告")
    report.append("")
    report.append("- 她的原话样本: %d 条" % len(her_real))
    report.append("- 生成样本: %d 条" % len(gen))
    report.append("")
    if len(her_real) < 5 or len(gen) < 5:
        report.append("⚠ 样本不足（各需 ≥5 条），部分指标不可靠；"
                      "建议补充数据后重测（§3.5 Step 2/4.43 访谈补充）。")
        report.append("")

    # 1. 口癖分布距离（KL）
    p_real, n1 = phrase_dist(her_real)
    p_gen, n2 = phrase_dist(gen)
    if n1 and n2:
        kld = symmetric_kl(p_real, p_gen)
        report.append("## 1. 口癖分布距离（对称 KL，0=完全一致）")
        report.append("")
        report.append("KL = **%.4f**" % kld)
        overlap = len(set(p_real) & set(p_gen))
        report.append("高频词（Top30）重合: %d/30" % overlap)
        report.append("她的高频词: %s" % "、".join(list(p_real)[:10]))
        report.append("生成高频词: %s" % "、".join(list(p_gen)[:10]))
    else:
        kld = None
        report.append("## 1. 口癖分布距离\n\n（数据不足，跳过）\n")
    report.append("")

    # 2. 句长分布距离（JS）
    h_real, n3 = len_hist(her_real)
    h_gen, n4 = len_hist(gen)
    if n3 and n4:
        jsd = js_distance(h_real, h_gen)
        report.append("## 2. 句长分布距离（JS 距离，0~1，越小越像）")
        report.append("")
        report.append("JS = **%.4f**" % jsd)
        report.append("她的句长直方图: %s" % json.dumps(h_real))
        report.append("生成句长直方图: %s" % json.dumps(h_gen))
    else:
        jsd = None
        report.append("## 2. 句长分布距离\n\n（数据不足，跳过）\n")
    report.append("")

    # 3. emoji 频率差
    er_real, er_gen = emoji_rate(her_real), emoji_rate(gen)
    report.append("## 3. emoji 频率差（每 100 字符出现次数）")
    report.append("")
    report.append("她: %.3f / 生成: %.3f / **差: %.3f**" % (er_real, er_gen,
                                                           abs(er_real - er_gen)))
    report.append("")

    # 4. 前缀预测命中率（检索式）
    hits, n_gold = prefix_hit_rate(real, args.her)
    report.append("## 4. 前缀预测命中率（检索式代理，§4.33）")
    report.append("")
    if n_gold:
        for k, v in hits.items():
            report.append("- Top-%d 命中率: **%.1f%%**" % (k, (v or 0) * 100))
        report.append("- 测试样本: %d 个（真实对话「前缀→她的下一条」）" % n_gold)
        report.append("- 说明: 离线代理指标（字面+世界树检索）；"
                      "完整生成式 Top-k 需 API 蒸馏（§4.33 闭环）")
    else:
        report.append("（数据不足，跳过）")
    report.append("")

    # 汇总
    report.append("## 汇总")
    report.append("")
    report.append("| 指标 | 数值 | 方向 |")
    report.append("|------|------|------|")
    report.append("| 口癖分布 KL | %s | 越小越像 |" % ("%.4f" % kld if kld is not None else "—"))
    report.append("| 句长分布 JS | %s | 越小越像 |" % ("%.4f" % jsd if jsd is not None else "—"))
    report.append("| emoji 频率差 | %.3f | 越小越像 |" % abs(er_real - er_gen))
    if n_gold:
        for k, v in hits.items():
            report.append("| 前缀预测 Top-%d 命中 | %.1f%% | 越大越像 |"
                          % (k, (v or 0) * 100))
    report.append("")
    report.append("迭代建议：改参数/纠正后重跑本脚本对比分数（蒸馏闭环，§4.33）。")

    text = "\n".join(report)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("\n报告已写入: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
