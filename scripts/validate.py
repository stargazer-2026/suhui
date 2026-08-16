#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate.py — 段产物 JSON 校验（P0-2，distill 断点续传配套）

用法：
  python3 validate.py <distill_dir> [--strict]

功能：
  - 扫描 distill 目录下的 seg_*.json / merged.json / checkpoint.json
  - 逐个 json.loads 解析，JSON 语法错误报文件+行号+列号（转义错误定位）
  - 字段完整性检查：persona/memories 必填结构、证据分级枚举、
    情感值 -10~10、重要性 0~1、实体簇 entity/aliases/world
  - 输出逐文件报告 + 汇总；exit 0 全部通过 / 1 存在问题

校验标准（§5.4/§4.1）：产物必须可被 build.py 消费，且满足证据分级铁律。
"""
import argparse
import json
import os
import re
import sys

EVIDENCE_LEVELS = {"verbatim", "artifact", "impression"}

# 必填结构（宽松版：键存在且类型正确）
PERSONA_KEYS = {
    "core_traits": list,
    "expression": dict,
    "emotion": dict,
    "relationship": dict,
    "values": list,
    "speculative": list,
}
MEMORIES_KEYS = {
    "timeline": list,
    "nodes": list,
    "daily_patterns": list,
    "unfinished": list,
}


def iter_json_files(distill_dir):
    """扫描候选 JSON 文件。"""
    names = sorted(os.listdir(distill_dir))
    for n in names:
        if n.endswith(".json"):
            yield os.path.join(distill_dir, n)


def check_json_file(path, strict=False):
    """返回 (ok, problems) 。"""
    problems = []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return False, ["JSON 语法错误：行 %d 列 %d（%s）"
                       % (e.lineno, e.colno, e.msg)]

    if not isinstance(data, dict):
        return False, ["顶层不是 JSON 对象"]

    base = os.path.basename(path)
    if base.startswith("seg_"):
        _check_segment(data, problems, strict)
    elif base == "merged.json":
        _check_merged(data, problems, strict)
    elif base == "checkpoint.json":
        _check_checkpoint(data, problems, strict)
    else:
        problems.append("未知文件类型（跳过结构校验）")
    return len(problems) == 0, problems


def _check_evidence(item, problems, where):
    lvl = item.get("evidence_level")
    if lvl is not None and lvl not in EVIDENCE_LEVELS:
        problems.append("%s: evidence_level=%r 非法（verbatim/artifact/impression）"
                        % (where, lvl))


def _check_segment(data, problems, strict):
    """seg_N.json：persona/memories 骨架 + 实体簇。"""
    persona = data.get("persona") or {}
    memories = data.get("memories") or {}
    if not isinstance(persona, dict):
        problems.append("persona 不是对象")
    if not isinstance(memories, dict):
        problems.append("memories 不是对象")
    _check_persona_skeleton(persona, problems)
    _check_memories_skeleton(memories, problems)
    for c in data.get("entity_clusters") or []:
        if not isinstance(c, dict):
            problems.append("entity_clusters 含非对象项")
            continue
        if not c.get("entity"):
            problems.append("实体簇缺少 entity")
        if not isinstance(c.get("aliases"), list):
            problems.append("实体簇 %s: aliases 应为列表" % c.get("entity"))


def _check_persona_skeleton(p, problems, strict=False):
    for key, typ in PERSONA_KEYS.items():
        if key not in p:
            problems.append("persona 缺字段: %s" % key)
        elif not isinstance(p[key], typ):
            problems.append("persona.%s 类型应为 %s" % (key, typ.__name__))
    for t in p.get("core_traits") or []:
        if isinstance(t, dict):
            _check_evidence(t, problems, "core_traits")
            if not t.get("trait"):
                problems.append("core_traits 项缺 trait")
    for c in (p.get("expression") or {}).get("catchphrases") or []:
        if isinstance(c, dict):
            _check_evidence(c, problems, "expression.catchphrases")
    for d in (p.get("emotion") or {}).get("day_night") or []:
        if isinstance(d, dict) and not d.get("when"):
            problems.append("day_night 项缺 when（场景规则必须绑定场景）")


def _check_memories_skeleton(m, problems, strict=False):
    for key, typ in MEMORIES_KEYS.items():
        if key not in m:
            problems.append("memories 缺字段: %s" % key)
        elif not isinstance(m[key], typ):
            problems.append("memories.%s 类型应为 %s" % (key, typ.__name__))
    for n in m.get("nodes") or []:
        if not isinstance(n, dict):
            problems.append("nodes 含非对象项")
            continue
        if not n.get("evidence"):
            problems.append("节点「%s」缺原文证据（verbatim 铁律）" % n.get("event", "?"))
        emo = n.get("emotion")
        if emo is not None and not (-10 <= float(emo) <= 10):
            problems.append("节点「%s」情感值 %s 超出 -10~10" % (n.get("event"), emo))
        imp = n.get("importance")
        if imp is not None and not (0 <= float(imp) <= 1):
            problems.append("节点「%s」重要性 %s 超出 0~1" % (n.get("event"), imp))
    for s in m.get("timeline") or []:
        if isinstance(s, dict) and (not s.get("stage")):
            problems.append("timeline 项缺 stage")


def _check_merged(data, problems, strict):
    """merged.json：build.py 的输入，结构必须完整。"""
    persona = data.get("persona") or {}
    memories = data.get("memories") or {}
    _check_persona_skeleton(persona, problems, strict)
    _check_memories_skeleton(memories, problems, strict)
    if "summary" not in data:
        problems.append("merged 缺 summary（SKILL.md description 用）")
    if "entity_clusters" not in data or not isinstance(
            data.get("entity_clusters"), list):
        problems.append("merged 缺 entity_clusters（世界树必需）")
    if strict and not data.get("corpus"):
        problems.append("strict: merged 缺 corpus（原文级记忆承诺，build 会写空）")
    # 情感解码规则（v2 新增维度，可选但校验结构）
    for d in (persona.get("emotion_decoder") or []):
        if isinstance(d, dict):
            if not d.get("cue") or not d.get("meaning"):
                problems.append("emotion_decoder 项缺 cue/meaning")
            _check_evidence(d, problems, "emotion_decoder")


def _check_checkpoint(data, problems, strict):
    segs = data.get("segments")
    if not isinstance(segs, list):
        problems.append("checkpoint 缺 segments 列表")
        return
    valid = {"pending", "running", "done", "failed"}
    for s in segs:
        if s.get("status") not in valid:
            problems.append("checkpoint 段 %s 状态非法: %s"
                            % (s.get("id"), s.get("status")))
    if not isinstance(data.get("merged"), bool):
        problems.append("checkpoint merged 应为 bool")


def main(argv=None):
    ap = argparse.ArgumentParser(description="溯洄 · 段产物 JSON 校验（P0-2）")
    ap.add_argument("distill_dir", help="distill 输出目录（含 seg_*.json/merged.json）")
    ap.add_argument("--strict", action="store_true",
                    help="严格模式（要求 corpus 等完整承诺字段）")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.distill_dir):
        sys.stderr.write("目录不存在: %s\n" % args.distill_dir)
        return 2

    files = list(iter_json_files(args.distill_dir))
    if not files:
        sys.stderr.write("未找到 JSON 文件: %s\n" % args.distill_dir)
        return 2

    total_ok, total_problems = 0, 0
    print("校验目录: %s（%d 个 JSON 文件）" % (args.distill_dir, len(files)))
    for path in files:
        ok, problems = check_json_file(path, args.strict)
        name = os.path.basename(path)
        if ok:
            total_ok += 1
            print("  ✅ %s" % name)
        else:
            total_problems += len(problems)
            print("  ❌ %s" % name)
            for p in problems[:20]:
                print("      - %s" % p)
            if len(problems) > 20:
                print("      - … 另有 %d 个问题" % (len(problems) - 20))

    print("----")
    print("结果：%d/%d 文件通过，共 %d 个问题" % (total_ok, len(files),
                                          total_problems))
    return 0 if total_problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
