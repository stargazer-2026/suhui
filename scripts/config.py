#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py — 本地配置读写（可选项设置，§5.3 字段与 4.10/4.44/屏蔽机制完全对齐）
标准库实现；同时提供 CLI 供用户查看/修改配置与备份迁移。

用法：
  python3 config.py init [--dir <配置目录>] [--version pro|flash]
  python3 config.py get [--dir <配置目录>] [key]
  python3 config.py set <key> <value> [--dir <配置目录>]
  python3 config.py switch pro|flash [--dir <配置目录>]     # flash<->pro 切换
  python3 config.py export-backup <out.json> [--dir <配置目录>]  # 设置备份/迁移
  python3 config.py import-backup <in.json> [--dir <配置目录>]
"""
import argparse
import json
import os
import sys

CONFIG_FILE = "config.json"

# §5.3 默认值（与 4.10 七项 + 4.44 连续状态 + 版本机制对齐）
DEFAULTS = {
    "version": "pro",                 # pro / flash
    "enabled_features": ["all"],      # flash 为启用功能清单；pro 为 ["all"]
    "mode": "real",                   # 4.10-1 真实她（默认）/ 理想她
    "healing_curve": False,           # 4.10-2 疗愈曲线（默认关）
    "inner_monologue": False,         # 4.10-3 内心独白（默认关）
    "advanced_cadence": False,        # 4.10-4 高级节奏（默认关）
    "watch_you_grow": False,          # 4.10-5 她看着你长大（默认关）
    "uncertain_truth": False,         # 4.10-6 不确定的真实（默认关）
    "continuous_state": True,         # 4.44 连续状态（默认开，设置中可关）
    "selection_level": 2,             # 4.10-7 择优强度 0-3（默认 2 中）
    "discriminator_level": 2,         # 判别器 0-2（默认 2 重）
}

# flash 版保留的核心机制（对应 §3 双版本策略「flash 保留」清单）
FLASH_CORE = [
    "memory_worldtree", "memory_competitive", "memory_3channel",
    "persona_core", "persona_scene_rules", "persona_implicit_measure",
    "persona_decision_weights", "persona_language_fingerprint",
    "emotion_pad", "behavior_cadence", "behavior_break", "behavior_imperfect",
    "behavior_shared_silence", "continuous_state", "time_sense",
    "expression_catchphrases", "expression_evidence", "expression_knowledge_boundary",
    "expression_correction_loop", "flow_steps", "flow_init_protocol",
    "flow_in_session_distill", "flow_farewell", "flow_time_capsule",
    "interview_supplement",
]

# flash 可裁的外围增强（§3 列表）
FLASH_CUT = [
    "time_traveler", "her_life", "attribution_analysis", "shared_future",
    "counterfactual", "time_eye", "artifacts", "memory_visualization",
    "evocation", "rituals", "her_dreams", "impression_evolution",
    "narrative_rebuild", "voice_identity", "sensory_memory",
    "her_plan_timeline", "strategic_sleep", "multi_session_dynamics",
    "shared_memory", "ideal_self", "regret_list", "self_reconciliation",
]


def load_config(config_dir):
    """读配置；文件不存在或损坏时返回默认值（不抛异常）。"""
    cfg = dict(DEFAULTS)
    path = os.path.join(config_dir, CONFIG_FILE)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
        except (OSError, ValueError) as e:
            sys.stderr.write("warning: config 读取失败（%s），使用默认值\n" % e)
    return cfg


def save_config(config_dir, cfg):
    os.makedirs(config_dir, exist_ok=True)
    path = os.path.join(config_dir, CONFIG_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def _parse_value(raw):
    """把命令行字符串解析为 JSON 类型（bool/int/float/str/list）。"""
    v = raw.strip()
    if v in ("true", "True"):
        return True
    if v in ("false", "False"):
        return False
    if v in ("null", "None"):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    if v.startswith("[") and v.endswith("]"):
        try:
            return json.loads(v)
        except ValueError:
            pass
    return v


def enabled_features(version):
    """按版本返回启用功能清单（pro 全量 / flash 核心）。"""
    if version == "flash":
        return list(FLASH_CORE)
    return ["all"]


def cmd_init(args):
    cfg = load_config(args.dir)
    cfg["version"] = args.version
    cfg["enabled_features"] = enabled_features(args.version)
    path = save_config(args.dir, cfg)
    print("配置已初始化: %s" % path)
    print(json.dumps(cfg, ensure_ascii=False, indent=2))


def cmd_get(args):
    cfg = load_config(args.dir)
    if args.key:
        if args.key not in cfg:
            sys.stderr.write("未知配置项: %s\n" % args.key)
            return 1
        print(json.dumps(cfg[args.key], ensure_ascii=False))
    else:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


def cmd_set(args):
    cfg = load_config(args.dir)
    if args.key not in DEFAULTS:
        sys.stderr.write("未知配置项: %s（合法项: %s）\n"
                         % (args.key, ", ".join(sorted(DEFAULTS))))
        return 1
    cfg[args.key] = _parse_value(args.value)
    if args.key == "version":
        # 切换版本：同步 enabled_features 清单
        cfg["enabled_features"] = enabled_features(cfg["version"])
    save_config(args.dir, cfg)
    print("已设置 %s = %s" % (args.key, json.dumps(cfg[args.key], ensure_ascii=False)))
    return 0


def cmd_switch(args):
    if args.version not in ("pro", "flash"):
        sys.stderr.write("version 必须是 pro 或 flash\n")
        return 1
    cfg = load_config(args.dir)
    cfg["version"] = args.version
    cfg["enabled_features"] = enabled_features(args.version)
    save_config(args.dir, cfg)
    print("已切换为 %s（产物不变，运行时功能集已更新）" % args.version)
    return 0


def cmd_export_backup(args):
    """v2.1（P2-24）：配置 + 产物整体导出（SKILL.md 承诺）。
    备份 = zip（config.json + 可选 products/ 目录）。"""
    import zipfile
    cfg = load_config(args.dir)
    backup_path = args.out
    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("config.json", json.dumps(cfg, ensure_ascii=False, indent=2))
        if args.products:
            for prod in args.products:
                if not os.path.isdir(prod):
                    sys.stderr.write("跳过（非目录）: %s\n" % prod)
                    continue
                for root, _dirs, files in os.walk(prod):
                    for fn in files:
                        full = os.path.join(root, fn)
                        zf.write(full, os.path.join(
                            "products", os.path.basename(prod),
                            os.path.relpath(full, prod)))
    print("备份已写入: %s（含 config + %d 个产物目录）"
          % (backup_path, len(args.products or [])))
    return 0


def cmd_import_backup(args):
    """恢复备份（config + 可选产物）。"""
    import zipfile
    if not zipfile.is_zipfile(args.infile):
        sys.stderr.write("备份文件格式异常（应为 zip）\n")
        return 1
    with zipfile.ZipFile(args.infile, "r") as zf:
        if "config.json" in zf.namelist():
            cfg = json.loads(zf.read("config.json").decode("utf-8"))
            if isinstance(cfg, dict):
                merged = dict(DEFAULTS)
                merged.update({k: v for k, v in cfg.items() if k in DEFAULTS})
                save_config(args.dir, merged)
        if args.restore_products:
            os.makedirs(args.restore_products, exist_ok=True)
            for name in zf.namelist():
                if name.startswith("products/"):
                    zf.extract(name, args.restore_products)
    print("备份已导入: %s" % os.path.join(args.dir, CONFIG_FILE))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="溯洄 · 本地配置读写（§5.3）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="初始化默认配置")
    p.add_argument("--version", choices=["pro", "flash"], default="pro")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("get", help="读取配置（可指定单项）")
    p.add_argument("key", nargs="?", default=None)
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("set", help="设置配置项")
    p.add_argument("key")
    p.add_argument("value")
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("switch", help="flash <-> pro 切换（产物不变）")
    p.add_argument("version", choices=["pro", "flash"])
    p.set_defaults(fn=cmd_switch)

    p = sub.add_parser("export-backup", help="配置+产物备份（zip，迁移用）")
    p.add_argument("out")
    p.add_argument("--products", nargs="*", default=[],
                   help="额外打包的产物目录（可多个）")
    p.set_defaults(fn=cmd_export_backup)

    p = sub.add_parser("import-backup", help="恢复备份")
    p.add_argument("infile")
    p.add_argument("--restore-products", default="",
                   help="恢复产物到指定目录（备份含产物时）")
    p.set_defaults(fn=cmd_import_backup)

    for name in ("init", "get", "set", "switch", "export-backup", "import-backup"):
        sub.choices[name].add_argument("--dir", default=".",
                                       help="配置目录（默认当前目录）")

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
