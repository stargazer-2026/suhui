#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
download_model.py — 下载中文 embedding 模型（BGE-small-zh，约 90MB，§0.5/§6）

用法：
  python3 download_model.py [--model BAAI/bge-small-zh-v1.5] [--dir models/]
      [--mirror hf-mirror|huggingface]

说明：
  - 模型下载到本地 models/ 目录，供 LanceDB 向量档使用（本地免费隐私）
  - 下载后模型以目录形式存在（safetensors + config.json + tokenizer）
  - 未配置/下载失败时系统自动降级：sqlite3+JSON + 世界树联想引擎（§4.2 零依赖档）
  - 本脚本是助手：环境无网络时跳过即可，不影响管线验证（--offline 蒸馏）
"""
import argparse
import os
import sys
import urllib.request

FILES = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
    "model.safetensors",
    "special_tokens_map.json",
]


def fetch(url, dest):
    print("  下载 %s" % url)
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "suhui-downloader"})
    with urllib.request.urlopen(req, timeout=120) as resp:  # v2.1 P2-17：90MB 模型放宽超时
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
    os.replace(tmp, dest)


def main():
    ap = argparse.ArgumentParser(description="BGE-small-zh embedding 模型下载")
    ap.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    ap.add_argument("--dir", default="models")
    ap.add_argument("--mirror", choices=["hf-mirror", "huggingface"],
                    default="hf-mirror")
    args = ap.parse_args()

    base = ("https://hf-mirror.com/" if args.mirror == "hf-mirror"
            else "https://huggingface.co/") + args.model + "/resolve/main/"
    os.makedirs(args.dir, exist_ok=True)

    ok, fail = 0, 0
    for fn in FILES:
        dest = os.path.join(args.dir, fn)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            print("已存在: %s" % dest)
            ok += 1
            continue
        try:
            fetch(base + fn, dest)
            ok += 1
        except Exception as e:
            print("  失败: %s（%s）—— 可跳过，系统将使用世界树零依赖档" % (fn, e))
            fail += 1
    print("完成：成功 %d / 失败 %d" % (ok, fail))
    if ok >= 3 and os.path.isfile(os.path.join(args.dir, "model.safetensors")):
        print("模型就绪：%s（可在存储层配置中启用向量档）" % args.dir)
    else:
        print("模型未完整下载（网络受限时正常）—— 零依赖档由世界树联想引擎提供检索与竞争性干扰（§4.2）")
    return 0 if ok >= 3 else 1


if __name__ == "__main__":
    sys.exit(main())
