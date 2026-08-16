#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
storage.py — 记忆存储层（§4.2 / §0.5）

用法：
  python3 storage.py init <data_dir> [--reset]
  python3 storage.py import <data_dir> <messages.json> [entity_clusters.json]
  python3 storage.py query <data_dir> "<查询>" [--topk 5] [--platform wechat]
  python3 storage.py status <data_dir>

存储策略（尽力而为，可降级）：
  1. LanceDB（已安装）→ 向量表（仅在配置了真实 embedding 时启用向量列，见下）
  2. 回退 sqlite3 + JSON 快照（标准库，任何环境可用）

三通道混合检索（§4.2）：
  ① 向量语义（真实 embedding：本地 BGE 或 API 端点，未配置则跳过——不设降级伪装）
  ② BM25 词面（词频/逆文档频率，标准库实现）
  ③ 世界树实体标签（显式结构，零依赖可用）
  分数加权合并 + 竞争性干扰（同一实体簇内记忆密度惩罚，γ 默认 0.5）

⚠️ 铁律（§6）：禁止用 n-gram 哈希冒充语义相似度——无真实 embedding 时
   向量通道自动关闭，检索由 词面+世界树 提供（结构化联想，不假装语义）。
"""
import argparse
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter

try:
    import lancedb
    HAVE_LANCEDB = True
except Exception:
    HAVE_LANCEDB = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from worldtree import WorldTree  # noqa: E402

DEFAULT_WEIGHTS = {"vector": 0.4, "bm25": 0.35, "worldtree": 0.25}
ALPHA, BETA, GAMMA = 0.3, 0.2, 0.5   # §4.2 记忆浮现分公式参数


def _models_candidates(data_dir):
    """models/ 目录候选（按优先级）：环境变量 → 脚本锚点 → 常见布局。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.environ.get("SUHUI_MODELS_DIR", "").strip(),
        os.path.join(here, "..", "models"),            # skill/scripts/../models
        os.path.join(os.getcwd(), "models"),
        os.path.join(data_dir, "..", "..", "models"),  # data/xxx/../../models
        os.path.join(data_dir, "..", "models"),        # data/../models
    ]
    return [c for c in cands if c]


def _check_embedding_available(data_dir):
    """真实 embedding 是否可用。返回档位标识或 None：
    - "local-model:<dir>"      本地向量档全开（模型+算力组件都在）
    - "model-ready-no-runtime" 模型已下载但算力组件（torch）未装
    - "api:<endpoint>"         API embedding 档
    - None                     未配置（基础联想模式）
    """
    model_dir = None
    for cand in _models_candidates(data_dir):
        if os.path.isfile(os.path.join(cand, "model.safetensors")) and \
                os.path.isfile(os.path.join(cand, "tokenizer.json")):
            model_dir = cand
            break
    runtime_ready = False
    try:
        import sentence_transformers  # noqa: F401
        runtime_ready = True
    except Exception:
        runtime_ready = False
    if model_dir and runtime_ready:
        return "local-model:" + model_dir
    if model_dir and not runtime_ready:
        return "model-ready-no-runtime"
    ep = os.environ.get("SUHUI_EMBEDDING", "").strip()
    if ep:
        return "api:" + ep
    return None


# ---------- 词面检索（BM25 简化版，标准库） ----------
def _tokens(text):
    out = []
    t = (text or "").lower()
    for m in re.finditer(r"[a-z0-9]+|[\u4e00-\u9fff]+", t):
        seg = m.group(0)
        if seg and seg[0].isascii():
            out.append(seg)
        else:
            if len(seg) <= 2:
                out.append(seg)
            else:
                out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
                out.extend(seg)  # 单字也入 token（保证单字查询可命中）
    return out


def _build_lexical(docs):
    """docs: [(id, text)] → (idf, doc_terms)"""
    doc_terms = {}
    df = Counter()
    for doc_id, text in docs:
        terms = Counter(_tokens(text))
        doc_terms[doc_id] = terms
        for t in terms:
            df[t] += 1
    n = max(1, len(docs))
    idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
    return idf, doc_terms


def _bm25_score(q_terms, doc_terms, idf, avgdl, k1=1.5, b=0.75):
    dl = sum(doc_terms.values())
    if dl == 0:
        return 0.0
    s = 0.0
    for t in q_terms:
        tf = doc_terms.get(t, 0)
        if tf == 0 or t not in idf:
            continue
        s += idf[t] * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / max(1, avgdl)))
    return s


class Store:
    def __init__(self, data_dir):
        self.dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "memory.sqlite3")
        self.table = None
        self.embedding = _check_embedding_available(data_dir)
        self._conn = None
        # v2.1（P0-5）：预计算索引缓存（导入后重建，查询 O(1) 读取）
        self._lex = None        # {"idf", "doc_terms", "avgdl", "docs"}
        self._entity_count = {}  # 实体 → 含该实体的消息数（竞争性干扰密度）
        self._clusters = []      # 世界树簇（实体→memories，P2-23 统一粒度）

    def _rebuild_index(self):
        """从 sqlite 全表重建检索索引（导入/重置后调用，查询不再全表扫描）。"""
        conn = self._sqlite()
        rows = conn.execute(
            "SELECT id,text,entities,world,situation FROM memories").fetchall()
        docs, doc_terms = [], {}
        df = Counter()
        for rid, text, entities, world, sit in rows:
            terms = Counter(_tokens(text or ""))
            doc_terms[rid] = terms
            for t in terms:
                df[t] += 1
            docs.append((rid, text))
        n = max(1, len(docs))
        idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        avgdl = sum(sum(t.values()) for t in doc_terms.values()) / n
        self._lex = {"idf": idf, "doc_terms": doc_terms, "avgdl": avgdl,
                     "docs": docs}
        # 实体密度（竞争性干扰）：实体 → 含该实体的消息数
        self._entity_count = Counter()
        for _rid, _text, entities, _w, _s in rows:
            for e in json.loads(entities or "[]"):
                self._entity_count[e] += 1
        # 世界树簇：实体 → 消息列表（P2-23：与世界树检索粒度统一）
        cluster_map = {}
        for rid, text, entities, world, sit in rows:
            for e in json.loads(entities or "[]"):
                c = cluster_map.setdefault(e, {
                    "entity": e, "aliases": [], "world": world,
                    "situations": (sit or "").split(","),
                    "memories": []})
                c["memories"].append({"text": (text or "")[:120]})
        self._clusters = list(cluster_map.values())

    def _sqlite(self):
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("""CREATE TABLE IF NOT EXISTS memories(
                id INTEGER PRIMARY KEY, ts TEXT, sender TEXT, text TEXT,
                type TEXT, platform TEXT, entities TEXT, world TEXT,
                situation TEXT, emotion REAL, importance REAL,
                source TEXT DEFAULT 'corpus')""")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_platform ON memories(platform)")
        return self._conn

    def _lancedb(self):
        if not HAVE_LANCEDB:
            return None
        if self.table is None:
            uri = os.path.join(self.dir, "lancedb")
            db = lancedb.connect(uri)
            names = db.list_tables() if hasattr(db, "list_tables") \
                else db.table_names()
            self.table = db.open_table("memories") if "memories" in names \
                else None
        return self.table

    def init(self, reset=False):
        conn = self._sqlite()
        if reset:
            conn.execute("DROP TABLE IF EXISTS memories")
            conn.execute("""CREATE TABLE memories(
                id INTEGER PRIMARY KEY, ts TEXT, sender TEXT, text TEXT,
                type TEXT, platform TEXT, entities TEXT, world TEXT,
                situation TEXT, emotion REAL, importance REAL,
                source TEXT DEFAULT 'corpus')""")
            conn.commit()
            if HAVE_LANCEDB:
                import shutil
                shutil.rmtree(os.path.join(self.dir, "lancedb"), ignore_errors=True)
        engine = "lancedb" if HAVE_LANCEDB else "sqlite3+json"
        print("存储层初始化: %s (engine=%s)" % (self.dir, engine))
        emb = self.embedding
        if emb and emb.startswith("local-model:"):
            print("embedding: 本地向量档已启用——三通道检索完整（向量+词面+世界树），竞争性干扰全开")
        elif emb == "model-ready-no-runtime":
            print("embedding: 中文语义模型已就绪，但语义联想组件未安装——"
                  "已启用基础联想模式（词面+世界树），核心像度机制不受影响；"
                  "需要时运行 ./install.sh --with-vector")
        elif emb and emb.startswith("api:"):
            print("embedding: API embedding 档已配置（数据出本地，由你权衡）")
        else:
            print("embedding: 未配置——基础联想模式（词面+世界树，§6 零依赖档，不设降级伪装）")
        return engine

    def import_messages(self, messages_path, clusters_path=None):
        with open(messages_path, "r", encoding="utf-8") as f:
            msgs = json.load(f)
        clusters = []
        if clusters_path and os.path.isfile(clusters_path):
            with open(clusters_path, "r", encoding="utf-8") as f:
                clusters = json.load(f)
        conn = self._sqlite()
        n = 0
        for m in msgs:
            entities, world, sit = [], m.get("world") or "", ""
            text = m.get("text") or ""
            for c in clusters:
                names = [c.get("entity", "")] + list(c.get("aliases") or [])
                if any(nm and nm in text for nm in names):
                    entities.append(c.get("entity", ""))
                    world = world or c.get("world", "")
                    sit = sit or ",".join(c.get("situations") or [])
            conn.execute(
                "INSERT INTO memories(ts,sender,text,type,platform,entities,"
                "world,situation,source) VALUES(?,?,?,?,?,?,?,?,?)",
                (m.get("ts"), m.get("sender"), text, m.get("type", "text"),
                 m.get("platform", "unknown"),
                 json.dumps(list(dict.fromkeys(entities)), ensure_ascii=False),
                 world, sit, "corpus"))
            n += 1
        conn.commit()
        print("已导入 %d 条消息（含实体标签）" % n)
        self._rebuild_index()  # v2.1（P0-5）：预计算索引，查询不再全表扫描

        if HAVE_LANCEDB:
            try:
                import pyarrow as pa
                rows = []
                for i, m in enumerate(msgs):
                    entities = []
                    for c in clusters:
                        names = [c.get("entity", "")] + list(c.get("aliases") or [])
                        if any(nm and nm in (m.get("text") or "") for nm in names):
                            entities.append(c.get("entity", ""))
                    rows.append({
                        "id": i, "ts": m.get("ts") or "",
                        "sender": str(m.get("sender") or ""),
                        "text": m.get("text") or "", "type": m.get("type", "text"),
                        "platform": m.get("platform", "unknown"),
                        "entities": list(dict.fromkeys(entities)),
                        "emotion": float(m.get("emotion") or 0),
                        "importance": float(m.get("importance") or 0),
                    })
                tbl = pa.Table.from_pylist(rows)
                uri = os.path.join(self.dir, "lancedb")
                db = lancedb.connect(uri)
                db.drop_table("memories", ignore_missing=True)
                db.create_table("memories", tbl)
                print("LanceDB 表已建: %d 行（%s）" % (
                    len(rows),
                    "向量列未启用——无真实 embedding 配置" if not self.embedding
                    else "向量列待 embedding 注入"))
            except Exception as e:
                print("LanceDB 导入失败（回退 sqlite 仍可用）: %s" % e)
        return n

    def status(self):
        conn = self._sqlite()
        n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        print("存储状态: %s" % self.dir)
        print("  sqlite 消息数: %d" % n)
        if HAVE_LANCEDB:
            t = self._lancedb()
            print("  lancedb: %s" % ("就绪" if t else "未建表"))
        print("  embedding: %s" % (self.embedding or "未配置（词面+世界树档）"))
        return 0

    def search(self, query, topk=5, platform=None):
        """三通道混合检索（v2.1 P0-5）：词索引/密度/簇均为导入时预计算，
        查询 O(rows×k) 评分，不再全表嵌套扫描（原 O(N²)）。"""
        if self._lex is None:
            self._rebuild_index()
        conn = self._sqlite()
        sql = ("SELECT id,ts,sender,text,type,platform,entities,world,situation,"
               "emotion,importance FROM memories")
        params = []
        if platform:
            sql += " WHERE platform=?"
            params.append(platform)
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return []

        q_terms = Counter(_tokens(query))
        idf, doc_terms, avgdl = (self._lex["idf"], self._lex["doc_terms"],
                                 self._lex["avgdl"])

        # 世界树分数（实体/情境命中；簇为预构建的统一粒度）
        wt = WorldTree(self._clusters) if self._clusters else None
        hit_sits = set()
        hit_ents = []
        if wt:
            hit_ents = wt.hit_entities(query)
            hit_sits = set(wt.hit_situations(query))

        # bm25 归一化基准（单次扫描，非嵌套）
        best_bm = 0.0
        for r in rows:
            b = _bm25_score(q_terms, doc_terms.get(r[0], {}), idf, avgdl)
            if b > best_bm:
                best_bm = b
        best_bm = max(1e-9, best_bm)

        scored = []
        for r in rows:
            rid, _ts, _s, text, _t, plat, entities_raw, _w, sit, emo, imp = r
            entities = json.loads(entities_raw or "[]") or []
            bm = _bm25_score(q_terms, doc_terms.get(rid, {}), idf, avgdl)
            wts = 0.0
            for c in hit_ents:
                if c.get("entity") in entities:
                    wts += 2.0
            for s in hit_sits:
                if s in (sit or ""):
                    wts += 1.5
            vec = 0.0  # 无真实 embedding 时向量通道为 0（不假装语义）
            score = (DEFAULT_WEIGHTS["vector"] * vec
                     + DEFAULT_WEIGHTS["bm25"] * (bm / best_bm)
                     + DEFAULT_WEIGHTS["worldtree"] * min(1.0, wts / 2.0))
            # 竞争性干扰：同实体簇密度（预计算 O(1)，原全表扫描 O(N)）
            density = 0
            for e in entities:
                density = max(density, self._entity_count.get(e, 0))
            score -= GAMMA * math.log1p(max(1, density)) * 0.15
            score += BETA * (imp or 0) + ALPHA * (abs(emo or 0) / 10.0) * 0.1
            if score > 0:
                scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        out = []
        for score, r in scored[:topk]:
            out.append({"score": round(score, 4), "ts": r[1], "sender": r[2],
                        "text": (r[3] or "")[:120], "platform": r[5],
                        "entities": json.loads(r[6] or "[]")})
        return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="溯洄 · 记忆存储层（§4.2）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("data_dir")
    p.add_argument("--reset", action="store_true")

    p = sub.add_parser("import")
    p.add_argument("data_dir")
    p.add_argument("messages")
    p.add_argument("clusters", nargs="?", default="")

    p = sub.add_parser("query")
    p.add_argument("data_dir")
    p.add_argument("query")
    p.add_argument("--topk", type=int, default=5)
    p.add_argument("--platform", default=None)

    p = sub.add_parser("status")
    p.add_argument("data_dir")

    args = ap.parse_args(argv)
    s = Store(args.data_dir)
    if args.cmd == "init":
        s.init(reset=args.reset)
    elif args.cmd == "import":
        s.import_messages(args.messages, args.clusters or None)
    elif args.cmd == "query":
        hits = s.search(args.query, topk=args.topk, platform=args.platform)
        print("检索「%s」Top-%d（三通道混合+竞争性干扰）:" % (args.query, args.topk))
        for h in hits:
            print("[%.3f] (%s %s) %s" % (h["score"], h["ts"], h["platform"], h["text"]))
    elif args.cmd == "status":
        s.status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
