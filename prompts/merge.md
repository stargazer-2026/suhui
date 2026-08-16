# 合并与冲突裁决模板（merge.md，§5.4）

你是记忆蒸馏引擎的合并器。任务：把各段蒸馏产物（persona 骨架 + memories 骨架 + 世界树实体簇）**合并成一份最终产物**，并裁决冲突。

## 输入

### 各段人格骨架（persona_segments）
```json
{{PERSONA_SEGMENTS}}
```

### 各段记忆骨架（memories_segments）
```json
{{MEMORIES_SEGMENTS}}
```

### 各段实体簇（entity_clusters）
```json
{{ENTITY_CLUSTERS}}
```

### 各段冲突（conflicts）
```json
{{CONFLICTS}}
```

### 全局统计摘要
```
{{STATS}}
```

## 输出要求

只输出一个 JSON 对象（不要输出任何其他文字），这是最终产物骨架（build.py 的输入），结构如下：

```json
{
  "summary": "一句话：她是谁（SKILL.md 的 description 用，≤40字）",
  "persona": {
    "core_traits": [
      {"trait": "可驱动每一句的元规则（3-5条，Layer 0）", "evidence_level": "verbatim|artifact|impression"}
    ],
    "expression": {
      "catchphrases": [{"phrase": "口癖", "freq": 数值, "when": "场景", "examples": ["[原文]"],
                        "evidence_level": "verbatim|artifact"}],
      "classic_quotes": [{"quote": "经典语录（低频完整句）", "count": 数值,
                          "when": "场景"}],
      "sentence_length": {"median_chars": 数值, "percentiles": {"50": 数值}, "style": "描述",
                          "evidence_level": "artifact"},
      "punctuation": ["高频标点"],
      "emoji_pattern": {"rate": "每百字频率", "preferred": ["常用emoji"], "style": "描述"}
    },
    "emotion_decoder": [
      {"cue": "反话/试探/省略的表达", "meaning": "真实意图", "when": "场景",
       "evidence": "原文或空", "evidence_level": "分级"}
    ],
    "emotion": {
      "triggers": [{"when": "场景条件", "behavior": "行为", "evidence": "原文或空", "evidence_level": "分级"}],
      "expression_style": "描述",
      "day_night": [
        {"when": "深夜", "behavior": "描述", "evidence_level": "artifact"},
        {"when": "白天", "behavior": "描述", "evidence_level": "artifact"}
      ]
    },
    "relationship": {
      "exclusive_behavior": [{"behavior": "专属行为", "evidence": "原文"}],
      "stage_changes": [{"stage": "阶段", "change": "变化", "evidence": "原文"}],
      "active_rate": "描述"
    },
    "platform_style": [{"platform": "wechat", "style": "描述", "evidence": "示例"}],
    "values": [{"value": "立场", "evidence": "[原文]", "evidence_level": "verbatim"}],
    "decision_weights": [{"dilemma": "两难", "prefers": "她优先保的", "evidence": "原文"}],
    "knowledge_boundary": ["她知道/聊过的话题"],
    "language_fingerprint": {"typos": ["口误模式"], "habits": ["打字习惯"]},
    "speculative": [{"inference": "无证据推断（隔离区）"}],
    "first_mes": "她的典型开场白（首次对话用，从记录提取，verbatim 优先；没有则给一句符合她风格的）",
    "alternate_greetings": {"深夜": "深夜开场", "白天": "白天开场", "久别后": "久别后的开场"}
  },
  "memories": {
    "timeline": [
      {"stage": "阶段名", "start": "起", "end": "止", "temperature": "温度描述",
       "events": ["代表性事件（带原文）"]}
    ],
    "nodes": [
      {"event": "事件一句话", "ts": "时间", "emotion": -10到10, "importance": 0到1,
       "evidence": "完整原文（verbatim）",
       "tags": {"entities": ["实体"], "situations": ["情境"], "world": "世界归属", "platform": "wechat"}}
    ],
    "daily_patterns": [{"pattern": "固定仪式/频率/中断", "evidence_level": "分级"}],
    "unfinished": [{"promise": "未兑现的约定", "ts": "时间", "evidence": "原文"}]
  },
  "entity_clusters": [
    {"entity": "实体名", "aliases": ["别名（含指代归并后的同义）"], "world": "世界归属",
     "situations": ["关联情境"], "platform": "wechat",
     "memories": [{"event": "相关记忆一句话", "evidence": "原文截断", "emotion": 数值, "importance": 数值}]}
  ],
  "conflicts": [
    {"issue": "无法裁决的矛盾", "versions": ["可能版本"], "note": "写入最终 conflicts.md，不删除不掩盖"}
  ],
  "corrections": []
}
```

## 硬规则（必须遵守）

1. **冲突裁决按证据强度**：verbatim > artifact > impression。同证据级别且无法裁决 → 写入 conflicts（不删除、不掩盖）。
2. **verbatim 冲突裁决时间表**：两条原文冲突时，时间戳较近者优先（记录在演进）。
3. **归并仅限同场景去重**：仅当「同一场景下同态度」出现重复变体时才合并（如深夜话多出现 5 条变体 → 合成一条）；跨场景差异全部保留（深夜 vs 白天不合并；不同平台风格不合并——那是她的多面性）。
4. **无证据推断全部进 speculative（推测区）**，隔离存放，不混入有证据条目。
5. **默认完整**：用户自己的数据，完整引用是质量要求；节点宁缺毋滥，但收了的必须带原文。
6. 不确定写"不确定"。
7. 实体簇按「同义归一、指代归并」合并各段产物；同一实体出现在不同世界时保留两个簇（标注各自世界）。
8. **情感解码规则（v2）**：各段 emotion_decoder 合并去重——同一 cue 同一 meaning 合并为一条；cue 相同但 meaning 冲突 → 写入 conflicts（不掩盖）。
9. **经典语录（v2）**：classic_quotes 跨段合并（同句合并计数）；口癖与经典语录保持两类，不混入。
10. 输出 JSON 必须合法（这是最终产物的唯一输入）。
