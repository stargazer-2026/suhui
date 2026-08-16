#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
worldtree.py — 世界树联想引擎（§4.2 零依赖档核心，纯标准库）

原理：人的联想是实体/情境/圈子驱动的——不需要语义向量，用显式结构即可实现。
  世界 = 枝干 → 实体簇 = 树杈 → 记忆 = 叶子

- 相关性 = 实体命中 + 情境匹配 + 世界匹配（显式标签计分）
- 干扰项 = 同一「实体簇+世界」内的记忆密度（竞争性遗忘在此结构上直接工作）
- 串台：提"猫"→ 命中实体簇"猫"→ 簇内记忆全部浮出 → 互相竞争 → 赢家浮现
- 别名扩展：检索时按别名表扩展命中，缓解换说法漏召回
- 可解释：她可以说出"因为都提到了猫，我突然想起……"

与 embedding 共存（可选）：有真实语义 embedding 时由外部混合打分，本模块只提供
结构分数；无 embedding 时本模块独立工作（实体级联想 + 别名扩展）。

⚠️ 禁止用 n-gram 哈希冒充语义相似度——本模块不假装做语义，只做显式结构计分。
"""
import json
import math
import re

# 情境标签的常见表述（蒸馏产物中可能出现的变体）
SITUATION_SYNONYMS = {
    "深夜": ["深夜", "晚上", "半夜", "凌晨"],
    "白天": ["白天", "下午", "中午", "上午", "早上"],
    "忙碌": ["忙", "忙碌", "上班", "加班", "赶", "出差"],
    "假期": ["假期", "放假", "旅游", "出去玩", "周末"],
    "压力期": ["压力", "累", "焦虑", "考试", "崩溃", "哭"],
    "纪念日": ["纪念日", "生日", "周年", "节日"],
    "吵架后": ["吵架", "冷战", "生气", "闹别扭"],
}


def norm_text(s):
    """归一化：小写、去空白（保留中文标点）。"""
    return re.sub(r"\s+", "", (s or "").lower())


def tokenize(text):
    """中英文混合分词（粗粒度：连续 CJK 按 2-gram 滑窗 + 英文单词）。"""
    text = norm_text(text)
    tokens = []
    for m in re.finditer(r"[a-z0-9]+|[\u4e00-\u9fff]+", text):
        seg = m.group(0)
        if re.match(r"^[a-z0-9]+$", seg):
            tokens.append(seg)
        else:
            if len(seg) == 1:
                tokens.append(seg)
            else:
                tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))
                tokens.append(seg)
    return tokens


class WorldTree:
    """世界树：world -> entity_cluster -> memories。检索 + 竞争性干扰打分。"""

    def __init__(self, clusters=None):
        # clusters: [{"entity": str, "aliases": [str], "world": str,
        #             "situations": [str], "memories": [str|dict], "platform": str}]
        self.clusters = clusters or []
        self._entity_index = {}   # 归一化实体/别名 -> cluster
        self._world_index = {}    # 世界名 -> [cluster]
        self._rebuild()

    def _rebuild(self):
        self._entity_index = {}
        self._world_index = {}
        for c in self.clusters:
            names = [c.get("entity", "")] + list(c.get("aliases") or [])
            for n in names:
                if n:
                    self._entity_index[norm_text(n)] = c
            w = c.get("world") or "你们的世界"
            self._world_index.setdefault(w, []).append(c)

    # ---- 构建 ----
    @classmethod
    def from_json(cls, path_or_dict):
        if isinstance(path_or_dict, dict):
            data = path_or_dict
        else:
            with open(path_or_dict, "r", encoding="utf-8") as f:
                data = json.load(f)
        clusters = data.get("entity_clusters") if isinstance(data, dict) else data
        return cls(clusters or [])

    def add_cluster(self, cluster):
        self.clusters.append(cluster)
        self._rebuild()

    # ---- 检索 ----
    def hit_entities(self, query):
        """返回命中的实体簇列表（含别名扩展）。"""
        q = norm_text(query)
        hits = []
        for name, cluster in self._entity_index.items():
            if name and (name in q or q in name):
                hits.append(cluster)
        return hits

    def hit_situations(self, query):
        """返回查询中命中的情境标签。"""
        q = norm_text(query)
        found = []
        for sit, syns in SITUATION_SYNONYMS.items():
            for s in syns:
                if s in q:
                    found.append(sit)
                    break
        return found

    def search(self, query, topk=5, alpha=0.3, beta=0.2, gamma=0.5):
        """
        检索 + 竞争性干扰打分（§4.2 公式）：
          浮现分(m) = 相关性(query, m) + α×情感权重(m) + β×重要性(m) − γ×干扰项(m)
        干扰项 = f(竞争者数量, 竞争者相似度)：同一「实体簇+世界」内记忆密度。
        返回 [(score, memory, cluster)]。
        """
        q = norm_text(query)
        qtokens = set(tokenize(query))
        hit_clusters = self.hit_entities(query)
        hit_sits = set(self.hit_situations(query))

        scored = []
        for cluster in self.clusters:
            cname = norm_text(cluster.get("entity", ""))
            world = cluster.get("world") or "你们的世界"
            situations = set(cluster.get("situations") or [])
            platform = cluster.get("platform")

            entity_hit = 1.0 if any(c is cluster for c in hit_clusters) else 0.0
            sit_hit = len(situations & hit_sits) / max(1, len(situations or [1]))
            world_hit = 1.0 if q and (norm_text(world) in q) else 0.0

            # 记忆内文本相似（字面 2-gram 重合，仅作字面补充，不冒充语义）
            mem_texts = []
            for mem in cluster.get("memories") or []:
                if isinstance(mem, dict):
                    mem_texts.append((mem.get("evidence") or mem.get("text") or
                                      mem.get("event") or "", mem))
                else:
                    mem_texts.append((str(mem), {"text": str(mem)}))

            for text, mem in mem_texts:
                mt = norm_text(text)
                mtokens = set(tokenize(text))
                overlap = len(qtokens & mtokens) / max(1, len(qtokens))
                relevance = entity_hit * 2.0 + sit_hit * 1.5 + world_hit * 1.0 + overlap * 0.5
                if relevance <= 0:
                    continue
                emotion = float(mem.get("emotion") or mem.get("emotion_value") or 0)
                importance = float(mem.get("importance") or 0)
                density = max(1, len(mem_texts))          # 簇内记忆密度（竞争者）
                interference = math.log1p(density) * 0.5   # 竞争者越多，干扰越强
                score = (relevance
                         + alpha * (abs(emotion) / 10.0) * 2.0
                         + beta * importance * 3.0
                         - gamma * interference)
                scored.append((score, mem, cluster))

        scored.sort(key=lambda x: -x[0])
        return scored[:topk]

    def density_report(self):
        """各实体簇的记忆密度（竞争性遗忘的结构基础）。"""
        return [{"entity": c.get("entity"), "world": c.get("world"),
                 "count": len(c.get("memories") or [])} for c in self.clusters]


def demo():
    """CLI：python3 worldtree.py <entity_clusters.json> "<查询>" [--topk 5]"""
    import argparse
    ap = argparse.ArgumentParser(description="世界树联想引擎（§4.2 零依赖档）")
    ap.add_argument("clusters")
    ap.add_argument("query")
    ap.add_argument("--topk", type=int, default=5)
    args = ap.parse_args()
    wt = WorldTree.from_json(args.clusters)
    print("命中实体簇:", [c.get("entity") for c in wt.hit_entities(args.query)])
    print("命中情境:", wt.hit_situations(args.query))
    print("--- 检索结果（竞争性干扰打分后）---")
    for score, mem, cluster in wt.search(args.query, topk=args.topk):
        label = mem.get("event") or mem.get("text", "")[:40]
        print("[%.2f] <%s> %s" % (score, cluster.get("entity"), label))


if __name__ == "__main__":
    demo()
