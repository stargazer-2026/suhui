#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mixed_test.py — 混听测试（§4.13，Step 5 可选子步骤，明确可跳过）

「玩一下」而非「考试」：10 条她的原话 + 10 条生成的，混在一起让用户分辨；
分不清率 = 感受像度。结果只给用户自己看，不作评判。

用法：
  python3 mixed_test.py --real messages.json --generated generated.json [--her B]
      [--n 10] [--out quiz.md] [--answers answers.key] [--check 用户答案.txt]

  --check: 用户按 quiz.md 逐行作答（每行一条：真实/生成，或 真/假），
           输出分不清率。
"""
import argparse
import json
import random
import re
import sys


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "messages" in data:
        data = data["messages"]
    return data


def pick(msgs, sender, n):
    texts = [(m.get("text") or "").strip()
             for m in msgs if m.get("sender") == sender and (m.get("text") or "").strip()]
    # 去重、去超长
    seen, out = set(), []
    for t in texts:
        if len(t) > 60 or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= n * 3:
            break
    random.shuffle(out)
    return out[:n]


def load_generated(path):
    data = load(path)
    if isinstance(data, dict):
        for k in ("messages", "generated", "sentences"):
            if isinstance(data.get(k), list):
                return [x.get("text") if isinstance(x, dict) else str(x)
                        for x in data[k]]
    return [x.get("text") if isinstance(x, dict) else str(x) for x in data]


def main(argv=None):
    ap = argparse.ArgumentParser(description="溯洄 · 混听测试（§4.13 可选）")
    ap.add_argument("--real", default="")
    ap.add_argument("--generated", default="")
    ap.add_argument("--her", default="B")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--out", default="quiz.md")
    ap.add_argument("--answers", default="answers.key")
    ap.add_argument("--check", default="", help="用户答案文件（每行: 真实/生成 或 真/假）")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)

    if not args.check and (not args.real or not args.generated):
        ap.error("生成模式需要 --real 与 --generated；判卷模式需要 --check 与 --answers")

    if args.check:
        with open(args.check, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        with open(args.answers, "r", encoding="utf-8") as f:
            key = [l.strip() for l in f if l.strip()]
        if len(lines) != len(key):
            sys.stderr.write("答案数与题目数不一致（%d vs %d）\n"
                             % (len(lines), len(key)))
            return 1
        right = 0
        for u, k in zip(lines, key):
            u2 = "真" if u in ("真实", "真", "real", "R", "r") else \
                 ("假" if u in ("生成", "假", "gen", "G", "g") else u)
            k2 = "真" if k in ("真实", "真", "real", "R", "r") else \
                 ("假" if k in ("生成", "假", "gen", "G", "g") else k)
            if u2 == k2:
                right += 1
        unsure = len(lines) - right
        rate = unsure / len(lines) * 100 if lines else 0
        print("分不清率 = %.0f%%（%d/%d 条分不清）" % (rate, unsure, len(lines)))
        print("（结果只给你自己看，不作评判。可以重测或继续迭代。）")
        return 0

    if args.seed is not None:
        random.seed(args.seed)
    real = pick(load(args.real), args.her, args.n)
    gen = [t for t in load_generated(args.generated) if t.strip()]
    random.shuffle(gen)
    gen = gen[:args.n]
    if not real or not gen:
        sys.stderr.write("样本不足：她的原话 %d 条 / 生成 %d 条（各需 ≥1，建议 ≥10）\n"
                         % (len(real), len(gen)))
        return 1

    items = [("真实", t) for t in real] + [("生成", t) for t in gen]
    random.shuffle(items)

    quiz = ["# 混听测试（§4.13 · 可选子步骤，结果只给你自己看）", "",
            "> 下面 %d 条里，一半是她的原话，一半是 skill 生成的。"
            "凭感觉标「真实」或「生成」。分不清 = 像。不用紧张，不是考试。"
            % len(items), ""]
    key = []
    for i, (label, t) in enumerate(items, 1):
        quiz.append("%d. %s" % (i, t))
        key.append(label)
        if i % 5 == 0:
            quiz.append("")
    quiz.append("")
    quiz.append("作答方式：新建一个 txt，每行一个编号答案（真实/生成），"
                "然后运行：")
    quiz.append("  python3 mixed_test.py --real <real> --generated <gen> "
                "--check 你的答案.txt")
    quiz.append("（key 文件不要给别人看）")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(quiz) + "\n")
    with open(args.answers, "w", encoding="utf-8") as f:
        f.write("\n".join(key) + "\n")
    print("已生成 %d 条混听测试 → %s（答案在 %s，请勿外传）"
          % (len(items), args.out, args.answers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
