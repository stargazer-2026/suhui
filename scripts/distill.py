#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
distill.py — 调 LLM API 逐段蒸馏 + 合并（§4.1 / §5.4）

用法：
  python3 distill.py <segments.json> <stats.json> <prompts目录>
      [--out <目录>] [--name <她的名字>] [--offline] [--no-merge]

环境变量（仅本脚本需要；会话内蒸馏（Step 3 主路径）不需要）：
  LLM_API_BASE  默认 https://api.deepseek.com/v1
  LLM_API_KEY   必填（未配置时本脚本明确提示，其余代码照常可写）
  LLM_MODEL     默认 deepseek-chat

特性：
  - 断点续传：checkpoint.json 记录每段状态（pending→running→done/failed），
    中断后从断点继续，不重复计费；failed 段重试，重试上限 3 次（熔断）
  - 每段全量送 API：persona_extract + memories_extract 两个模板（含世界树标签）
  - 各段产物合并再蒸馏一次（merge.md），输出 merged.json（build.py 的输入）
  - JSON mode 优先（LLM API 支持时），不支持自动回退并容错解析
  - --offline：无 key 的启发式骨架模式（仅用于管线验证/无 API 环境，
    产物标注 generation=offline-heuristic，正式蒸馏请配置 LLM_API_KEY）
"""
import argparse
import datetime
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_API_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
MAX_RETRIES = 3
HTTP_TIMEOUT = 120

# 中文口语高频词（离线实体候选过滤用停用词，词列表）
STOPWORDS = set("""的了 是在 有 你 我 她 他 就 都 也 还 很 吧 吗 啊 呢 哦 嗯 呀 嘛 啦 呗 吧
这 那 什么 怎么 为什么 一个 没有 不是 还是 真的 知道 喜欢 觉得 想 现在 今天 明天
昨天 时候 东西 事情 朋友 这样 那样 一下 一点 的话 感觉 有点 就是 你说 在吗 算了
晚安 图片 看到 说起 其实 然后 因为 所以 但是 如果 虽然 或者 不过 已经 可以 可能
应该 一定 一样 一起 之后 之前 后来 最后 最近 一直 总是 经常 有时 每次 突然
""".split())

# ---------- 环境 ----------
def api_config():
    base = os.environ.get("LLM_API_BASE", DEFAULT_API_BASE).rstrip("/")
    key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", DEFAULT_MODEL).strip()
    return base, key, model


# ---------- HTTP（OpenAI 兼容 chat/completions，标准库） ----------
def call_llm(base, key, model, messages, temperature=0.3, json_mode=True):
    url = base + "/chat/completions"
    body = {"model": model, "messages": messages, "temperature": temperature,
            "stream": False}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    return content


def parse_json_response(content):
    """容错解析：去 ```json 围栏、截取首个 {…} 块、修复常见尾逗号。"""
    if not content:
        raise ValueError("空响应")
    s = content.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        s = s[start:end + 1]
    try:
        return json.loads(s)
    except ValueError:
        s2 = re.sub(r",\s*([}\]])", r"\1", s)
        return json.loads(s2)


def call_json(base, key, model, messages, temperature=0.3):
    """先试 JSON mode，400 回退普通模式；返回 dict。"""
    try:
        content = call_llm(base, key, model, messages, temperature, True)
        return parse_json_response(content)
    except urllib.error.HTTPError as e:
        if e.code == 400:
            content = call_llm(base, key, model, messages, temperature, False)
            return parse_json_response(content)
        raise
    except ValueError:
        # JSON 解析失败：普通模式重试一次
        content = call_llm(base, key, model, messages, temperature, False)
        return parse_json_response(content)


RETRYABLE_HTTP = (429, 500, 502, 503, 504)


def _retry_delay(attempt, exc):
    """退避：429 优先读 Retry-After（上限 60s）；其余指数退避。"""
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
        ra = exc.headers.get("Retry-After") if exc.headers else None
        if ra:
            try:
                return min(60, max(1, int(ra)))
            except ValueError:
                pass
        return 30  # 429 无 Retry-After：较长退避
    return 2 ** (attempt - 1) + random.uniform(0, 0.5)  # v2.1 P2-19：并发退避加 jitter


def is_retryable(e):
    """v2（P1-5）：只重试 5xx/429/网络错误/响应解析失败；4xx 不重试。"""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in RETRYABLE_HTTP
    if isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError,
                      OSError, ValueError)):
        return True
    return False


def with_retry(fn, *args, **kwargs):
    """重试上限 3 次（§4.1 熔断：不无限重试）；4xx 直接抛不重试（P1-5）。"""
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last = e
            if not is_retryable(e):
                raise  # 4xx（除 429）等不可重试：直接抛
            sys.stderr.write("  [retry %d/%d] %s: %s\n"
                             % (attempt, MAX_RETRIES, type(e).__name__, e))
            if attempt < MAX_RETRIES:
                time.sleep(_retry_delay(attempt, e))
    raise RuntimeError("API 调用失败（已重试 %d 次）: %s" % (MAX_RETRIES, last))


# ---------- 模板填充 ----------
def load_prompt(prompts_dir, name):
    path = os.path.join(prompts_dir, name)
    if not os.path.isfile(path):
        raise FileNotFoundError("缺少模板: %s" % path)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def fill(template, **kw):
    out = template
    for k, v in kw.items():
        out = out.replace("{{%s}}" % k, str(v))
    return out


def format_segment_text(messages):
    lines = []
    for m in messages:
        ts = m.get("ts") or "时间未知"
        sender = m.get("sender") or "?"
        lines.append("[%s] %s: %s" % (ts, sender, m.get("text") or ""))
    return "\n".join(lines)


def stats_summary(stats):
    """统计摘要 → 紧凑文本（蒸馏模板输入）。"""
    if not stats:
        return "（无统计）"
    L = []
    L.append("总消息数: %s" % stats.get("total_messages"))
    L.append("发送者占比: %s" % json.dumps(stats.get("per_sender", {}), ensure_ascii=False))
    if stats.get("span"):
        L.append("时间跨度: %s → %s" % (stats["span"].get("start"), stats["span"].get("end")))
    L.append("消息长度百分位(字符): %s" % json.dumps(stats.get("message_length_percentiles") or {}))
    L.append("B 的句长百分位(排除≤1字短消息, v2.1): %s"
             % json.dumps(stats.get("sender_len_B_main")
                          or stats.get("sender_len_B") or {}))
    L.append("B 的深夜(22-2点)消息占比: %s" % stats.get("night_ratio_B"))
    L.append("B 回复 A 的中位延迟(秒): %s" % (stats.get("reply_delay_seconds") or {}).get("50"))
    L.append("高频词(全部, 排除占位符): %s" % json.dumps(
        [p["phrase"] for p in (stats.get("top_phrases") or [])[:15]], ensure_ascii=False))
    L.append("B 的高频词: %s" % json.dumps(
        [p["phrase"] for p in (stats.get("top_phrases_B") or [])[:15]], ensure_ascii=False))
    L.append("B 的经典语录(低频完整句): %s" % json.dumps(
        [p["quote"] for p in (stats.get("top_quotes_B") or [])[:8]], ensure_ascii=False))
    L.append("A 的高频词: %s" % json.dumps(
        [p["phrase"] for p in (stats.get("top_phrases_A") or [])[:15]], ensure_ascii=False))
    L.append("高频 emoji: %s" % json.dumps(stats.get("emoji_frequency") or {}, ensure_ascii=False))
    L.append("标点频率: %s" % json.dumps(stats.get("punctuation_frequency") or {}, ensure_ascii=False))
    L.append("活跃时段(小时→条数): %s" % json.dumps(stats.get("hourly_activity") or {}))
    L.append("对话开启者(长间隙后首发): %s" % json.dumps(stats.get("conversation_initiators") or {}, ensure_ascii=False))
    return "\n".join(L)


# ---------- 断点续传 ----------
def load_checkpoint(out_dir):
    path = os.path.join(out_dir, "checkpoint.json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"segments": [], "merged": False}


def save_checkpoint(out_dir, cp):
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, "checkpoint.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cp, f, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(out_dir, "checkpoint.json"))


def seg_status(cp, seg_id):
    for s in cp["segments"]:
        if s["id"] == seg_id:
            return s
    return None


def mark(cp, seg_id, status, retries=0, out=None):
    s = seg_status(cp, seg_id)
    if s is None:
        s = {"id": seg_id, "status": status, "retries": retries, "out": out}
        cp["segments"].append(s)
    else:
        s["status"] = status
        s["retries"] = retries
        if out:
            s["out"] = out
    return s


# ---------- 离线启发式骨架（无 key 验证管线用，低质量，明确标注） ----------
def offline_distill(segments, stats, name=""):
    seg_texts = []
    all_her_texts, all_a_texts = [], []
    for seg in segments:
        for m in seg["messages"]:
            if m.get("sender") == "B":
                all_her_texts.append(m.get("text") or "")
            elif m.get("sender") == "A":
                all_a_texts.append(m.get("text") or "")
        seg_texts.append(format_segment_text(seg["messages"]))

    ph = [p["phrase"] for p in (stats.get("top_phrases_B") or [])[:6]]
    her_len = stats.get("sender_len_B_main") or stats.get("sender_len_B") or {}
    med_len = her_len.get("50")
    night = stats.get("night_ratio_B")
    initiators = stats.get("conversation_initiators") or {}

    expression = {
        "catchphrases": [{"phrase": p, "freq": round(1.0 / max(1, len(ph)), 3),
                          "examples": [next((t[:30] for t in all_her_texts if p in t), "")],
                          "when": "日常"} for p in ph],
        "sentence_length": {"median_chars": med_len,
                            "percentiles": her_len,
                            "note": "artifact(统计)"},
        "punctuation": list((stats.get("punctuation_frequency") or {}).keys())[:5],
        "emoji_pattern": {"freq": stats.get("emoji_frequency") or {},
                          "note": "artifact(统计)"},
    }
    emotion = {
        "triggers": [],
        "expression_style": "（离线骨架：无法从统计推断情绪表达方式，待 API 蒸馏补充）",
        "day_night": [
            {"when": "深夜",
             "behavior": "深夜消息占比 %s，句长中位 %s 字（artifact 统计）"
                         % (night, med_len) if night else "（无统计）"},
            {"when": "白天", "behavior": "（无统计）"},
        ],
    }
    relationship = {
        "exclusive_behavior": [],
        "stage_changes": [],
        "active_rate": {"B 主动开启对话次数占比": initiators.get("B"),
                        "A 主动开启对话次数占比": initiators.get("A"),
                        "note": "artifact(统计)"},
    }
    values = [{"value": "（离线骨架未提取价值观，待 API 蒸馏）", "evidence": "",
               "level": "impression"}]
    knowledge_boundary = []
    speculative = [{"inference": "离线骨架为统计启发式产物，仅用于验证管线；"
                                 "正式蒸馏请配置 LLM_API_KEY。",
                    "level": "impression"}]
    first_mes = _offline_first_mes(segments)
    persona = {
        "core_traits": [
            {"trait": "话痨/安静：B 共 %s 条消息，句长中位 %s 字" % (
                stats.get("per_sender", {}).get("B"), med_len),
             "evidence_level": "artifact"},
            {"trait": "主动/被动：长间隙后 B 首发次数占比 %s" % initiators.get("B"),
             "evidence_level": "artifact"},
            {"trait": "深夜型/白天型：B 深夜消息占比 %s" % night,
             "evidence_level": "artifact"},
        ],
        "expression": expression,
        "emotion": emotion,
        "relationship": relationship,
        "platform_style": [],
        "values": values,
        "speculative": speculative,
        "knowledge_boundary": knowledge_boundary,
        "decision_weights": [],
        "language_fingerprint": {"typos": [], "note": "（待 API 蒸馏提取口误模式）"},
        "first_mes": first_mes[0] if first_mes else "在吗",
        "alternate_greetings": {
            "深夜": first_mes[1] if len(first_mes) > 1 and first_mes[1] else "还没睡吗",
            "白天": first_mes[2] if len(first_mes) > 2 and first_mes[2] else "今天怎么样",
            "久别后": first_mes[3] if len(first_mes) > 3 and first_mes[3] else "好久没聊了",
        },
    }

    # memories：按长间隙（>30 天）切阶段；节点=最长消息；日常模式=活跃时段
    timeline, cur_stage, cur_msgs = [], [], []
    prev = None
    for seg in segments:
        for m in seg["messages"]:
            t = _epoch(m.get("ts"))
            if prev is not None and t is not None and (t - prev) / 86400 > 30 \
                    and cur_msgs:
                timeline.append(_stage_from_msgs(cur_msgs, len(timeline)))
                cur_msgs = []
            cur_msgs.append(m)
            if t:
                prev = t
    if cur_msgs:
        timeline.append(_stage_from_msgs(cur_msgs, len(timeline)))

    nodes = []
    n_b = max(1, stats.get("per_sender", {}).get("B", 1))
    for seg in segments:
        for m in seg["messages"]:
            text = (m.get("text") or "").strip()
            if len(text) >= 20 and m.get("sender") == "B":
                nodes.append({
                    "event": text[:60],
                    "evidence": text,
                    "emotion": _rough_emotion(text),
                    "importance": round(min(1.0, len(text) / 120.0), 2),
                    "ts": m.get("ts"),
                    "tags": {"entities": [], "situations": [], "world": "你们的世界",
                             "platform": m.get("platform") or "unknown"},
                })
    nodes = nodes[:12]
    if not nodes:
        # 兜底：取 B 最长消息作节点（离线低质，仅保证管线有产物）
        cands = []
        for seg in segments:
            for m in seg["messages"]:
                text = (m.get("text") or "").strip()
                if text and m.get("sender") == "B":
                    cands.append((len(text), m))
        cands.sort(key=lambda x: -x[0])
        for _, m in cands[:3]:
            text = (m.get("text") or "").strip()
            nodes.append({
                "event": text[:60], "evidence": text,
                "emotion": _rough_emotion(text),
                "importance": round(min(1.0, len(text) / 80.0), 2),
                "ts": m.get("ts"),
                "tags": {"entities": [], "situations": [], "world": "你们的世界",
                         "platform": m.get("platform") or "unknown"},
            })

    # 实体簇：B 高频 2-gram 近似（离线低质，标注 impression；过滤虚词/过高频词）
    n_b = max(1, stats.get("per_sender", {}).get("B", 1))
    clusters = []
    for p in (stats.get("top_phrases_B") or [])[:12]:
        w = p["phrase"]
        cnt = p["count"]
        if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", w) and \
                w not in STOPWORDS and cnt / n_b <= 0.3:
            clusters.append({"entity": w, "aliases": [], "world": "你们的世界",
                             "situations": [], "platform": "unknown",
                             "memories": [{"text": next(
                                 (t for t in all_her_texts if w in t), "")[:80]}],
                             "quality": "offline-heuristic"})

    memories = {
        "timeline": timeline,
        "nodes": nodes,
        "daily_patterns": [
            {"pattern": "活跃时段：%s" % _hourly_str(stats.get("hourly_activity") or {}),
             "evidence_level": "artifact"},
        ],
        "unfinished": [],
    }
    return {"persona": persona, "memories": memories,
            "entity_clusters": clusters, "conflicts": [],
            "summary": "（离线骨架）%s 的统计画像——低质量占位，正式蒸馏请配置 LLM_API_KEY" % (name or "她"),
            "first_mes": first_mes[0] if first_mes else "在吗"}


def _epoch(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts)).timestamp()
    except (ValueError, TypeError):
        return None


def _stage_from_msgs(msgs, idx):
    ts_list = [_epoch(m.get("ts")) for m in msgs if m.get("ts")]
    ts_list = [t for t in ts_list if t]
    start = datetime.datetime.fromtimestamp(min(ts_list)).strftime("%Y-%m-%d") if ts_list else "?"
    end = datetime.datetime.fromtimestamp(max(ts_list)).strftime("%Y-%m-%d") if ts_list else "?"
    b_cnt = sum(1 for m in msgs if m.get("sender") == "B")
    rate = round(b_cnt / max(1, len(msgs)), 2)
    return {"stage": "阶段%d（离线推断）" % (idx + 1), "start": start, "end": end,
            "temperature": "偏冷" if rate < 0.3 else ("偏热" if rate > 0.55 else "适中"),
            "events": ["（离线骨架：阶段 %s→%s，B 消息占比 %s）" % (start, end, rate)]}


def _rough_emotion(text):
    e = 0
    for ch in text:
        if ch in "！!😭😢💔":
            e -= 1
        if ch in "！!😄😊🎉❤":
            e += 1
    return max(-10, min(10, e))


def _hourly_str(hourly):
    if not hourly:
        return "?"
    peak = max(hourly, key=lambda h: hourly[h])
    return "峰值 %s 点（%s 条）" % (peak, hourly[peak])


def _offline_first_mes(segments):
    """从记录提取她的典型开场：长间隙（>12h）后 B 的第一条消息。"""
    from collections import Counter
    openers = Counter()
    by_hour = {"深夜": Counter(), "白天": Counter(), "久别后": Counter()}
    prev = None
    for seg in segments:
        for m in seg["messages"]:
            t = _epoch(m.get("ts"))
            gap_h = 999
            if prev is not None and t is not None:
                gap_h = (t - prev) / 3600
            if m.get("sender") == "B" and gap_h > 12:
                text = (m.get("text") or "").strip()
                if text:
                    key = text[:12]
                    openers[key] += 1
                    if t is not None:
                        hour = datetime.datetime.fromtimestamp(t).hour
                        if gap_h > 24 * 7:
                            by_hour["久别后"][key] += 1
                        elif hour >= 22 or hour < 5:
                            by_hour["深夜"][key] += 1
                        else:
                            by_hour["白天"][key] += 1
            if t:
                prev = t
    def top(c):
        return c.most_common(1)[0][0] if c else None
    return [top(openers), top(by_hour["深夜"]), top(by_hour["白天"]),
            top(by_hour["久别后"])]


# ---------- 单段蒸馏（并行安全） ----------
_CP_LOCK = threading.Lock()


def _distill_segment(seg, args, cp, base, key, model, persona_tpl,
                     memories_tpl, stats, total):
    """蒸馏单段；返回 (sid, status, out_file)。checkpoint 操作带锁（并行安全）。"""
    sid = seg["id"]
    with _CP_LOCK:
        st = seg_status(cp, sid)
        if st and st["status"] == "done" and st.get("out") and \
                os.path.isfile(os.path.join(args.out, st["out"])):
            return sid, "done", st["out"]
        mark(cp, sid, "running")
        save_checkpoint(args.out, cp)

    print("[%d/%d] 蒸馏段 %d（%d 条消息，%s → %s）..."
          % (sid + 1, total, sid, seg["count"], seg.get("start"),
             seg.get("end")))
    seg_text = format_segment_text(seg["messages"])
    if len(seg_text) > 60000:
        sys.stderr.write("  ⚠ 该段文本约 %d 字符，可能超出模型上下文一半；"
                         "建议用 segment.py --max-messages 重新切段\n" % len(seg_text))

    out_file = "seg_%d.json" % sid
    try:
        if args.offline:
            sub = offline_distill([seg], stats, args.name)
            result = {"segment_id": sid, "persona": sub["persona"],
                      "memories": sub["memories"],
                      "entity_clusters": sub["entity_clusters"]}
        else:
            result = {}
            user_stats = stats_summary(stats)
            for key_tpl, tpl in (("persona", persona_tpl),
                                 ("memories", memories_tpl)):
                prompt = fill(tpl, SEGMENT_TEXT=seg_text, STATS=user_stats,
                              SEGMENT_ID=sid, TOTAL_SEGMENTS=total,
                              HER_NAME=args.name or "她")
                out = with_retry(call_json, base, key, model,
                                 [{"role": "user", "content": prompt}])
                result[key_tpl] = out
            if "persona" not in result or "memories" not in result:
                raise RuntimeError("段产物缺少 persona/memories 字段")
            result["entity_clusters"] = result.get("memories", {}).get(
                "entity_clusters", [])
    except Exception as e:
        sys.stderr.write("  段 %d 蒸馏失败：%s\n" % (sid, e))
        with _CP_LOCK:
            mark(cp, sid, "failed", retries=MAX_RETRIES)
            save_checkpoint(args.out, cp)
        return sid, "failed", None

    with open(os.path.join(args.out, out_file), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    with _CP_LOCK:
        mark(cp, sid, "done", retries=0, out=out_file)
        save_checkpoint(args.out, cp)
    print("  ✓ 段 %d 完成 → %s" % (sid, out_file))
    return sid, "ok", out_file


# ---------- 主流程 ----------
def main(argv=None):
    ap = argparse.ArgumentParser(description="溯洄 · LLM 逐段蒸馏 + 合并（§4.1）")
    ap.add_argument("segments", help="segments.json")
    ap.add_argument("stats", help="stats.json")
    ap.add_argument("prompts", help="prompts/ 目录")
    ap.add_argument("--out", default="distill_out", help="输出目录（默认 distill_out/）")
    ap.add_argument("--name", default="", help="她的名字（写入 meta）")
    ap.add_argument("--offline", action="store_true",
                    help="离线启发式骨架模式（无 key 验证管线用，低质量）")
    ap.add_argument("--no-merge", action="store_true", help="只逐段蒸馏，不合并")
    ap.add_argument("--parallel", type=int, default=1,
                    help="并发蒸馏段数（P2-10；默认 1 串行；注意 API 限流，"
                         "429 由重试退避保护）")
    args = ap.parse_args(argv)

    with open(args.segments, "r", encoding="utf-8") as f:
        seg_data = json.load(f)
    segments = seg_data["segments"] if isinstance(seg_data, dict) else seg_data
    with open(args.stats, "r", encoding="utf-8") as f:
        stats = json.load(f)

    base, key, model = api_config()

    if not args.offline and not key:
        sys.stderr.write(
            "错误：未检测到 LLM_API_KEY 环境变量。\n"
            "  · 会话内蒸馏（Step 3 主路径）不需要密钥——在对话中直接让 AI 分批蒸馏即可；\n"
            "  · 脚本路径需要配置：export LLM_API_KEY=... [LLM_API_BASE=... LLM_MODEL=...]\n"
            "  · 或先用 --offline 模式验证整条管线（产物为低质量骨架）。\n")
        return 2

    os.makedirs(args.out, exist_ok=True)
    cp = load_checkpoint(args.out)
    if not cp["segments"]:
        for seg in segments:
            mark(cp, seg["id"], "pending")
        save_checkpoint(args.out, cp)

    prompts_dir = args.prompts
    persona_tpl = load_prompt(prompts_dir, "persona_extract.md")
    memories_tpl = load_prompt(prompts_dir, "memories_extract.md")

    total = len(segments)
    done_seg_files = []

    # 预扫描：跳过已完成段
    for seg in segments:
        st = seg_status(cp, seg["id"])
        if st and st["status"] == "done" and st.get("out") and \
                os.path.isfile(os.path.join(args.out, st["out"])):
            done_seg_files.append(st["out"])
            print("[%d/%d] 段 %d 已完成（断点续传跳过）" % (seg["id"] + 1, total,
                                                   seg["id"]))
    todo = [seg for seg in segments
            if not (seg_status(cp, seg["id"]) and
                    seg_status(cp, seg["id"])["status"] == "done")]

    if todo and args.parallel > 1 and not args.offline:
        # v2（P2-10）：并发蒸馏（--parallel N，注意限流——429 由重试退避处理）
        print("并发蒸馏：%d 段 / %d 并发（限流由重试退避保护）..."
              % (len(todo), args.parallel))
        with ThreadPoolExecutor(max_workers=args.parallel) as ex:
            futs = {ex.submit(_distill_segment, seg, args, cp, base, key,
                              model, persona_tpl, memories_tpl, stats,
                              total): seg for seg in todo}
            for fut in as_completed(futs):
                seg = futs[fut]
                try:
                    sid, status, out_file = fut.result()
                except Exception as e:
                    print("  ✗ 段 %d 异常：%s" % (seg["id"], e))
                    continue
                if status == "ok":
                    done_seg_files.append(out_file)
                elif status == "done":
                    done_seg_files.append(out_file)
                    print("[%d/%d] 段 %d 已完成（断点续传跳过）"
                          % (seg["id"] + 1, total, seg["id"]))
    else:
        for seg in todo:
            sid, status, out_file = _distill_segment(
                seg, args, cp, base, key, model, persona_tpl, memories_tpl,
                stats, total)
            if status in ("ok", "done"):
                done_seg_files.append(out_file)

    # 检查是否有 failed 段
    failed = [s for s in cp["segments"] if s["status"] == "failed"]
    if failed and not args.offline:
        sys.stderr.write("⚠ %d 段蒸馏失败（已熔断，可用 --out 同目录重跑以重试）：%s\n"
                         % (len(failed), [s["id"] for s in failed]))

    if not done_seg_files:
        sys.stderr.write("错误：没有任何段蒸馏成功，无法合并\n")
        return 3

    # 合并
    merged_path = os.path.join(args.out, "merged.json")
    if cp.get("merged") and os.path.isfile(merged_path):
        print("合并已完成（断点续传跳过）")
        return 0

    seg_objs = []
    for fn in sorted(done_seg_files):
        with open(os.path.join(args.out, fn), "r", encoding="utf-8") as f:
            seg_objs.append(json.load(f))

    if args.no_merge:
        print("--no-merge：跳过合并步骤，各段产物保留在 %s" % args.out)
        return 0

    if args.offline:
        merged = offline_distill(segments, stats, args.name)
        merged.update({
            "version": 1,
            "name": args.name or "",
            "generation": "offline-heuristic",
            "coverage": "full" if len(segments) == 1 else "segmented",
            "stats": stats,
            "segment_count": len(done_seg_files),
            "corpus": [m for seg in segments for m in seg["messages"]],
        })
    else:
        merge_tpl = load_prompt(prompts_dir, "merge.md")
        payload = {"persona_segments": [s.get("persona", {}) for s in seg_objs],
                   "memories_segments": [s.get("memories", {}) for s in seg_objs],
                   "entity_clusters": [c for s in seg_objs
                                       for c in (s.get("entity_clusters") or [])],
                   "conflicts": [c for s in seg_objs for c in (s.get("conflicts") or [])],
                   "stats": stats_summary(stats),
                   "HER_NAME": args.name or "她"}
        prompt = fill(merge_tpl, PERSONA_SEGMENTS=json.dumps(
            payload["persona_segments"], ensure_ascii=False, indent=2),
            MEMORIES_SEGMENTS=json.dumps(payload["memories_segments"],
                                         ensure_ascii=False, indent=2),
            ENTITY_CLUSTERS=json.dumps(payload["entity_clusters"],
                                       ensure_ascii=False, indent=2),
            CONFLICTS=json.dumps(payload["conflicts"], ensure_ascii=False, indent=2),
            STATS=payload["stats"], HER_NAME=payload["HER_NAME"])
        print("合并蒸馏中（merge.md）...")
        merged = with_retry(call_json, base, key, model,
                            [{"role": "user", "content": prompt}])
        merged.update({
            "version": 1,
            "name": args.name or merged.get("name", ""),
            "generation": "api",
            "coverage": "full" if len(segments) == 1 else "segmented",
            "stats": stats,
            "segment_count": len(done_seg_files),
            "corpus": [m for seg in segments for m in seg["messages"]],
        })

    with open(merged_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    cp["merged"] = True
    save_checkpoint(args.out, cp)
    print("蒸馏完成：merged.json → %s" % merged_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
