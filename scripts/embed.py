#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
embed.py — 真实语义 embedding 助手（§6 像度档位·本地优先）

用法：
  python3 embed.py init <models_dir>       # 加载本地 BGE（需已下载模型）
  python3 embed.py encode <models_dir> "文本..." ["文本2"...]

档位：
  1. 本地模型档（默认，隐私最好）：models/ 下有 BGE-small-zh（download_model.py 下载），
     需要 sentence-transformers/torch（未安装时本脚本明确提示，不假装可用）
  2. API embedding 档（可选）：环境变量 SUHUI_EMBEDDING=<OpenAI兼容端点> 时走 API

⚠️ 铁律（§6）：不提供降级嵌入——语义检索是"像"的底线，不设 n-gram 伪装。
  未配置时 storage.py 自动关闭向量通道，由 词面+世界树 提供检索（§4.2 零依赖档）。
"""
import argparse
import json
import os
import sys
import urllib.request

_MODEL = None


def load_local(models_dir):
    global _MODEL
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception:
        return None, ("未安装语义联想组件——本地向量档不可用；"
                      "可运行 ./install.sh --with-vector 一键补装，或用 API 档"
                      "（SUHUI_EMBEDDING），或继续使用基础联想模式")
    # 目录解析：直接使用传入目录；若传入的是 scripts/ 之类则锚定 ../models
    model_dir = models_dir
    if not os.path.isfile(os.path.join(model_dir, "model.safetensors")):
        here = os.path.dirname(os.path.abspath(__file__))
        cand = os.path.join(here, "..", "models")
        if os.path.isfile(os.path.join(cand, "model.safetensors")):
            model_dir = cand
    if not os.path.isfile(os.path.join(model_dir, "model.safetensors")):
        return None, "models/ 下无模型文件，先运行 scripts/download_model.py"
    _MODEL = SentenceTransformer(model_dir)
    return _MODEL, None


def encode_local(texts):
    return _MODEL.encode(texts, normalize_embeddings=True).tolist()


def encode_api(texts):
    ep = os.environ.get("SUHUI_EMBEDDING", "").strip()
    if not ep:
        raise RuntimeError("SUHUI_EMBEDDING 未配置")
    # v2.1（P2-18）：端点需自带鉴权（如 URL 内嵌 key 或网关层认证）——
    # 本脚本不注入任何密钥；请求头不含 Authorization
    body = {"input": texts}
    req = urllib.request.Request(
        ep, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [d["embedding"] for d in data.get("data", [])]


def main(argv=None):
    ap = argparse.ArgumentParser(description="真实语义 embedding 助手（§6）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("models_dir")
    p = sub.add_parser("encode")
    p.add_argument("models_dir")
    p.add_argument("texts", nargs="+")
    args = ap.parse_args(argv)

    if args.cmd == "init":
        model, err = load_local(args.models_dir)
        if model is None:
            sys.stderr.write("本地档不可用：%s\n" % err)
            return 1
        print("本地模型已加载（%s）" % args.models_dir)
        return 0

    # encode
    model, err = load_local(args.models_dir)
    if model is not None:
        vecs = encode_local(args.texts)
    else:
        try:
            vecs = encode_api(args.texts)
        except Exception as e:
            sys.stderr.write("embedding 不可用：%s；%s\n" % (err, e))
            return 1
    for t, v in zip(args.texts, vecs):
        print("%s\t(dim=%d, 前3维=%.4f %.4f %.4f)" % (t[:20], len(v), v[0], v[1], v[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
