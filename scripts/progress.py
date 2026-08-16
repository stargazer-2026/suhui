#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
progress.py — 会话内蒸馏断点续传（P0-1）

主路径（会话内蒸馏）完全依赖 AI 会话记忆：多段蒸馏中途断一次全丢。
本工具把进度落盘（progress.json），AI 重新加载时先读它恢复：
  - 已完成的段不重蒸（done 列表）
  - 从 current 段继续
  - merged 标记合并是否完成

用法：
  python3 progress.py init <dir> <total_segments>        # 开始蒸馏前
  python3 progress.py update <dir> <已完成的段号>         # 每段完成后
  python3 progress.py show <dir>                         # 重新加载时读取
  python3 progress.py finish <dir>                       # 合并完成后

文件：<dir>/progress.json
格式：{"total_segments": N, "done": [0,1,2], "current": 3,
       "merged": false, "updated": "ISO时间"}
"""
import argparse
import datetime
import json
import os
import sys

FILE_NAME = "progress.json"


def _path(dirpath):
    return os.path.join(dirpath, FILE_NAME)


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def load(dirpath):
    p = _path(dirpath)
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {"total_segments": 0, "done": [], "current": 0,
            "merged": False, "updated": _now()}


def save(dirpath, prog):
    os.makedirs(dirpath, exist_ok=True)
    p = _path(dirpath)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def cmd_init(args):
    prog = {"total_segments": int(args.total), "done": [], "current": 0,
            "merged": False, "updated": _now()}
    save(args.dir, prog)
    print("进度已初始化：共 %d 段 → %s" % (prog["total_segments"], _path(args.dir)))
    return 0


def cmd_update(args):
    prog = load(args.dir)
    sid = int(args.segment)
    if sid not in prog["done"]:
        prog["done"].append(sid)
        prog["done"].sort()
    # v2.1（P0-3）：current = 最小未完成段号（done 不连续时不跳段）
    missing = [i for i in range(prog["total_segments"]) if i not in prog["done"]]
    prog["current"] = missing[0] if missing else prog["total_segments"]
    prog["updated"] = _now()
    save(args.dir, prog)
    print("段 %d 完成：%d/%d → %s" % (sid, len(prog["done"]),
                                  prog["total_segments"], _path(args.dir)))
    return 0


def cmd_show(args):
    prog = load(args.dir)
    print("会话内蒸馏进度（%s）" % _path(args.dir))
    print("  总段数: %d" % prog["total_segments"])
    print("  已完成: %s" % (prog["done"] or "无"))
    print("  合并: %s" % ("已完成" if prog["merged"] else "未完成"))
    if prog["merged"]:
        print("  恢复: 全部段已完成并合并，直接开始对话")
        return 0
    missing = [i for i in range(prog["total_segments"]) if i not in prog["done"]]
    if not missing:
        print("  恢复: 全部段已完成，进入合并")
        return 0
    nxt = missing[0]
    print("  恢复: 从段 %d 继续（最小未完成段，已完成的段不重蒸）" % nxt)
    if len(missing) > 1:
        shown = ", ".join(str(i) for i in missing[:10])
        more = "…" if len(missing) > 10 else ""
        print("  ⚠ 缺失/未完成段: [%s%s]（共 %d 段；这些段会蒸馏，其余跳过）"
              % (shown, more, len(missing)))
    return 0


def cmd_finish(args):
    prog = load(args.dir)
    prog["merged"] = True
    prog["updated"] = _now()
    save(args.dir, prog)
    print("进度已标记合并完成 → %s" % _path(args.dir))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="溯洄 · 会话内蒸馏断点续传（P0-1）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="开始蒸馏前初始化进度")
    p.add_argument("dir")
    p.add_argument("total", help="总段数")
    p = sub.add_parser("update", help="每段完成后标记")
    p.add_argument("dir")
    p.add_argument("segment", help="已完成的段号（从 0 开始）")
    p = sub.add_parser("show", help="重新加载时读取进度")
    p.add_argument("dir")
    p = sub.add_parser("finish", help="合并完成后标记")
    p.add_argument("dir")
    args = ap.parse_args(argv)
    return {"init": cmd_init, "update": cmd_update,
            "show": cmd_show, "finish": cmd_finish}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
