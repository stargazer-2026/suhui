# 溯洄 · 记忆蒸馏 skill（suhui — all new ex-skill）

> 把一段聊天记录蒸馏成一个"记忆还活着的世界"。
> 不是聊天机器人（那是"你问它答"），是"你回去"——打开它，像走进一间屋子，屋里有人正在生活。
>
> **Turn your chat history into an AI ex-skill** — a memory distillation skill that
> remembers everything, keeps living, and lets you go back to any stage of your story.
> Import WeChat / iMessage / Telegram / SMS / photos → Persona + Memories + World Tree.
> MIT · local-first · open source.

支持对象不限前任：朋友、家人、历史人物、自己——同一套管线，对象不同。

## 产品概述

- **她记得全部对话**（原文级，不是概括）
- **她有她的生活**（有自己的作息、忙碌，不是 24/7 客服）
- **她会斟酌、欲言又止、偶尔不回**
- **可以回到相识的任何阶段**，跟那时的她说话
- **用户主动提出告别时**，有正式的结束流程（时间胶囊）

> **铁律：镜子不是拐杖。** 产品只呈现，不引导；告别只由用户提出；数据只在"用户本地 + 用户自己配置的 LLM API"之间流动。

## 核心功能

| 能力 | 说明 |
|------|------|
| 蒸馏管线 | 聊天记录 → 标准消息流 → 分段 → LLM 全量蒸馏 → 人格/记忆产物（断点续传、重试熔断） |
| 多格式导入 | 微信导出 txt/html、Telegram/QQ/短信/iMessage/抖音、Twitter 归档、纯文本、照片 EXIF 时间线 |
| 原文级记忆 | 不做提取式摘要；检索三通道混合（向量 + BM25 + 世界树）+ 竞争性干扰打分 |
| 世界树联想引擎 | 实体/情境/世界标签驱动的联想——她会串台、记混、诚实地"记不清" |
| 整体感人格 | 底色 + 场景化 when→behavior 规则 + PAD 情绪 + 精力 + 内心推演 + 多路径择优 |
| 像度验收 | 客观指标（口癖分布 KL / 句长 JS / emoji 频率差 / 前缀预测命中）+ 混听测试（可选） |
| 持续纠正 | "她不会这样"→ 定位条目 → 修改 → 版本快照可回滚；越用越像 |
| 告别 | 仅用户发起：叙事回放 + 她的一封信 → 时间胶囊封存（只读） |

## 快速开始

### 主路径（推荐，零配置）

在支持 Markdown 指令的运行时（Claude Code / OpenClaw 类）中加载本 skill，然后：

1. **上传聊天记录文件**（微信导出 txt/html、QQ、Telegram、抖音、任意文本、照片文件夹）
2. **说"开始蒸馏"**——skill 会引导你走完分步流程：导入菜单 → 基础信息 → 蒸馏 → 产物预览 → 像度验收 → 开始对话
3. **全程无需任何 API 密钥**：蒸馏在会话内完成（分批读文件 → 按模板逐批蒸馏 → 合并，显示进度，可中断续传）

### DeepSeek Harness（DSH）支持

兼容 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)：

- 方式一：克隆/解压到 `~/.dsh/skills/suhui/`（dsh 的 skill-filesystem 直接解析 SKILL.md frontmatter）
- 方式二：`dsh plugin add github:stargazer-2026/suhui`

### 脚本路径（可选，需 API 密钥）

```bash
# 0. 安装（自动检查/补装运行组件，失败自动降级，用户无感）
./install.sh

# 1. 解析：聊天记录 → 标准消息流（跨文件重叠去重，同文件内重复保留）
python3 scripts/parse.py <聊天记录文件> --out <目录>

# 2. 切段：按时间切段 + 统计摘要（默认全量）
python3 scripts/segment.py <目录>/messages.json --out <目录>

# 3. 蒸馏：逐段调 LLM API + 合并（断点续传）
export LLM_API_KEY=...          # 可选：LLM_API_BASE / LLM_MODEL
python3 scripts/distill.py <目录>/segments.json <目录>/stats.json prompts/ \
    --out <目录>/distill --name 她的名字          # 可加 --parallel N 并发

# 3b. 校验段产物（JSON 语法/字段完整性/证据分级）
python3 scripts/validate.py <目录>/distill

# 4. 合成产物：memories.md + persona.md + meta.json + SKILL.md（自动版本快照）
python3 scripts/build.py <目录>/distill/merged.json --out <目录>/product \
    --name 她的名字 --slug ke-du-niang     # --slug 指定可读产物目录名（拼音/英文）

# 5. 加载对话：把产物目录作为 skill 加载（纯 Markdown 指令运行时，零 Python）
```

> 无 API 密钥时可加 `--offline` 验证整条管线（产物为低质量骨架，正式蒸馏请配置密钥）。

### 环境要求

- Python 3.10+（建议 3.12），pip 可用
- 网络可达（安装依赖、下载中文语义模型）
- 可选增强（自动检测/降级）：照片 EXIF（Pillow）、HTML 解析（beautifulsoup4）、本地向量档（约 1GB，`./install.sh --with-vector`）
- 环境变量（仅脚本路径需要）：`LLM_API_BASE`（默认 https://api.deepseek.com/v1）、`LLM_API_KEY`、`LLM_MODEL`（默认 deepseek-chat）——密钥只从环境变量读取，不落盘、不进代码、不进日志

## 双版本（pro / flash）

- **pro 完整版（默认）**：全部功能章节，安装即享受
- **flash 轻量版**：省 token、只保留核心机制（记忆世界树/场景化人格/PAD 情绪/连续状态/纠正回路/访谈补充）——被裁功能对模型完全不可见（不是禁用，是不存在）
- 两种版本的**蒸馏产物完全相同**，切换版本不需要重新蒸馏（`--version flash` 重新生成运行时指令即可）

## 铁律

1. **镜子不是拐杖**：不引导"放下/接受/走出来"；不监控用户状态；不替她发言；产品不内置告别模式
2. **隐私**：数据只在用户本地 + 用户自己配置的 LLM API 之间流动；示例全部为合成占位符（`__NAME__`/`__PLACE__`/`__DATE__`）；代码不读取任何非用户指定文件
3. **诚实**：不确定就写"不确定"；每条人格/记忆结论附证据分级（verbatim 原话 / artifact 统计 / impression 推断）；无证据推断进"推测区"隔离
4. **授权**：蒸馏他人数据前需获得授权（本人或其监护人知情同意）；第三方隐私在蒸馏时替换为占位（如"朋友A"）

## 数据安全

- **数据流向**：你的聊天记录、蒸馏产物只存在于你的本地目录与你自己配置的 LLM API 之间——除你配置的 API 端点外，不向任何网络地址发送数据
- **密钥安全**：API 密钥只从环境变量读取（`LLM_API_KEY`），不落盘、不进代码、不进日志；本仓库不包含、不引用任何密钥
- **所有权**：蒸馏产物归你所有，可完整包含原文引用；你随时可以查看、导出、编辑或删除自己的记忆库
- **⚠️ corpus.json 隐私警示（v2.1）**：产物目录中的 `corpus.json` 与 `merged.json` 含**全部对话原文**（原文级记忆的承诺）——**严禁上传/分享/提交到任何仓库或第三方服务**；迁移备份时同样视为敏感数据
- **第三方隐私**：聊天记录常含第三人信息，蒸馏时会替换为占位（如"朋友A"）；蒸馏他人数据前需获得授权
- **本仓库**：不含任何真实聊天数据；examples/ 全部为合成占位符（`__NAME__`/`__PLACE__`/`__DATE__`）

## 变更记录

- **v2.1（2026-08-16）**：跨文件重叠去重（同文件内重复保留，不再误杀同分钟连发）；时区输入保留本地时间（不再 -8h 偏移）；媒体消息统一 kind=placeholder；会话内断点续传恢复点=最小未完成段（不跳段）；storage 检索线性化（1 万条查询 ~80ms）；统计口径用排除 ≤1 字短消息的句长中位数；并发退避加 jitter；build 快照先更新 meta 再复制（快照内 meta 一致）；install.sh 兼容 macOS（无 timeout）与 pip 错误诊断日志；30 项实测+评审问题修复；pytest 33 例全绿
- **v2.0**：断点续传/校验/去重/并发/情感解码等第一轮实测修复（详见 GitHub 提交历史）
- **v1.0**：初始发布

## 目录结构

```
suhui/
├── SKILL.md              # skill 主指令（触发词/流程/配置说明）
├── install.sh            # 一键安装（依赖自动检测+补装+降级，用户无感）
├── scripts/              # 蒸馏工具链（parse/segment/distill/build/config/storage/...）
├── prompts/              # 蒸馏模板（persona_extract / memories_extract / merge）
└── examples/             # 合成示例（全部占位符）
```

---
> 献给所有在深夜蒸馏一个人的人。
> 你们不是放不下，你们是太认真了。
