#!/usr/bin/env bash
# 溯洄 · 一键安装（§3/§0.5/§6）
#
# 体验设计：用户无感，不说依赖名——
#   安装 = ①装产物（主任务，立即完成）②检查/补装运行组件（报告式，失败自动降级不阻塞）
#
# 用法:
#   ./install.sh [产物目录] [目标 skills 目录] [--skip-deps] [--with-vector]
#
# 组件（人话名 → 用途 → 失败降级）：
#   记忆存储与检索组件  → 蒸馏产物入库/三通道检索（§4.2）      → 降级 sqlite3+JSON（标准库）
#   照片时间线组件      → 照片 EXIF 时间线（§4.49）            → 降级标准库 EXIF 解析
#   网页聊天记录解析组件 → 微信 HTML 导出（§4.1）              → 降级 html.parser
#   语义联想增强组件    → 本地向量档（§6 默认档，约 1GB）       → 降级世界树+词面（核心像度机制不受影响）
set -u

PRODUCT_DIR="${1:-}"
SKILLS_DIR="${2:-}"
SKIP_DEPS=0
WITH_VECTOR=0
for a in "$@"; do
  case "$a" in
    --skip-deps) SKIP_DEPS=1 ;;
    --with-vector) WITH_VECTOR=1 ;;
  esac
done

say() { echo "  $*"; }
ok()  { echo "  ✅ $*"; }
warn(){ echo "  ⚠️  $*"; }

if [[ -z "$PRODUCT_DIR" || ! -f "$PRODUCT_DIR/SKILL.md" ]]; then
  echo "用法: $0 <产物目录(含 SKILL.md)> [目标 skills 目录] [--skip-deps] [--with-vector]"
  echo "提示: 先用 build.py 生成产物，例如:"
  echo "  python3 scripts/build.py distill_out/merged.json --out out/my-ex --name 小美"
  exit 1
fi

# ---------- 1. Python 环境（必需） ----------
if ! command -v python3 >/dev/null 2>&1; then
  echo "✋ 需要 Python 运行环境（3.10 以上）才能使用本 skill 的蒸馏工具链。"
  echo "   对话功能不受影响（纯指令加载），安装蒸馏工具链后即可开始。"
  exit 1
fi
PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
  say "运行环境: Python $PY_VER ✓"
else
  echo "✋ Python 版本过低（$PY_VER，需要 ≥3.10）。"
  exit 1
fi

# ---------- 2. 安装产物（主任务，立即完成） ----------
SLUG=$(sed -n 's/^name: *//p' "$PRODUCT_DIR/SKILL.md" | head -1 | tr -d ' "'"'"'')
SLUG=${SLUG:-ex-$(date +%s)}
if [[ -z "$SKILLS_DIR" ]]; then
  for cand in "$HOME/.claude/skills" "$HOME/.openclaw/skills" "$HOME/.config/skills" "$PWD/skills"; do
    if [[ -d "$cand" ]]; then SKILLS_DIR="$cand"; break; fi
  done
fi
if [[ -z "$SKILLS_DIR" ]]; then
  SKILLS_DIR="$PWD/skills"
  say "未探测到已有 skills 目录，将创建: $SKILLS_DIR"
fi
mkdir -p "$SKILLS_DIR"
DEST="$SKILLS_DIR/$SLUG"
if [[ -e "$DEST" ]]; then
  echo "目标已存在: $DEST"
  if [[ -t 0 ]]; then
    read -r -t 15 -p "  覆盖? [y/N] " ans || ans="n"
  else
    ans="n"
    echo "  （非交互环境，默认不覆盖；删除 $DEST 后重试，或换目标目录）"
  fi
  [[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "  已取消"; exit 0; }
  rm -rf "$DEST"
fi
cp -r "$PRODUCT_DIR" "$DEST"
ok "skill 已安装 → $DEST"
echo "  （产物含: SKILL.md / persona.md / memories.md / meta.json / config.json / worldbook.md / corpus.json）"
echo "  对话端: 将 $DEST 作为 skill 加载即可（纯 Markdown 指令运行时，零 Python）"

# ---------- 3. 运行组件检查/补装（报告式，失败降级不阻塞） ----------
if [[ "$SKIP_DEPS" == "1" ]]; then
  echo "  （--skip-deps：跳过运行组件检查；核心功能仍可用，增强组件稍后可补）"
  exit 0
fi
if ! command -v pip3 >/dev/null 2>&1 && ! python3 -m pip --version >/dev/null 2>&1; then
  warn "缺少包管理能力——核心功能不受影响（标准库实现），增强组件稍后可手动补装。"
  exit 0
fi

PIP="python3 -m pip install --quiet"
pip_try() { # $1=pip args
  if command -v pip3 >/dev/null 2>&1; then
    pip3 install --quiet "$@" 2>/dev/null && return 0
    pip3 install --quiet --break-system-packages "$@" 2>/dev/null && return 0
  fi
  python3 -m pip install --quiet "$@" 2>/dev/null && return 0
  python3 -m pip install --quiet --break-system-packages "$@" 2>/dev/null
}

has() { python3 -c "import $1" >/dev/null 2>&1; }

echo ""
echo "  运行组件检查（用户无感；缺失则自动补装，失败自动降级）..."

# 3.1 记忆存储与检索组件（lancedb/numpy）
if has lancedb && has numpy; then
  ok "记忆存储与检索组件已就绪"
else
  say "  正在补装记忆存储与检索组件（约 1 分钟）..."
  if pip_try lancedb numpy; then
    ok "记忆存储与检索组件已就绪（本地存储，隐私优先）"
  else
    warn "记忆存储与检索组件未就绪——已自动降级为基础存储模式（核心检索/像度机制不受影响）。"
    warn "稍后可补装：python3 -m pip install lancedb numpy"
  fi
fi

# 3.2 照片时间线组件（Pillow）
if has PIL; then
  ok "照片时间线组件已就绪"
else
  say "  正在补装照片时间线组件..."
  if pip_try Pillow; then
    ok "照片时间线组件已就绪（导入照片时可提取时间线）"
  else
    warn "照片时间线组件未就绪——已自动降级为标准解析（仍能读常见照片时间）。"
  fi
fi

# 3.3 网页聊天记录解析组件（beautifulsoup4）
if has bs4; then
  ok "网页聊天记录解析组件已就绪"
else
  say "  正在补装网页聊天记录解析组件..."
  if pip_try beautifulsoup4; then
    ok "网页聊天记录解析组件已就绪（微信网页版导出更稳）"
  else
    warn "网页聊天记录解析组件未就绪——已自动降级为标准解析（txt 导出完全不受影响）。"
  fi
fi

# 3.4 中文语义模型（BGE，§6 本地默认档的数据部分）
MODELS_DIR="$(cd "$(dirname "$0")" && pwd)/models"
if [[ -f "$MODELS_DIR/model.safetensors" && -f "$MODELS_DIR/tokenizer.json" ]]; then
  ok "中文语义模型已就绪（全本地，免费隐私）"
else
  say "  正在下载中文语义模型（约 90MB，本地存储）..."
  if timeout 120 python3 "$(dirname "$0")/scripts/download_model.py" --dir "$MODELS_DIR" >/dev/null 2>&1; then
    ok "中文语义模型已就绪（全本地，免费隐私）"
  else
    warn "中文语义模型未就绪——已自动降级为基础联想模式（世界树+词面，核心像度机制不受影响）。"
    warn "稍后可补装：python3 scripts/download_model.py"
  fi
fi

# 3.5 语义联想增强组件（torch/sentence-transformers，约 1GB，§6 本地默认档的算力部分）
NEED_VEC=1
if python3 -c "import sentence_transformers" >/dev/null 2>&1; then
  ok "语义联想增强组件已就绪（本地向量档全开：向量+词面+世界树三通道检索）"
  NEED_VEC=0
fi
if [[ "$NEED_VEC" == "1" ]]; then
  if [[ "$WITH_VECTOR" == "1" ]]; then
    say "  正在补装语义联想增强组件（约 1GB，可能需要几分钟）..."
    if timeout 240 pip_try sentence-transformers; then
      ok "语义联想增强组件已就绪（本地向量档全开：向量+词面+世界树三通道检索）"
    else
      warn "语义联想增强组件安装超时/失败——已自动降级为基础联想模式（不影响核心像度机制：口癖/句长/emoji/节奏/世界树联想全部可用）。"
      warn "稍后可补装：python3 -m pip install sentence-transformers"
    fi
  else
    warn "语义联想增强组件未启用（体积较大）——当前为基础联想模式，核心像度机制不受影响。"
    warn "需要时一条命令补装：./install.sh $PRODUCT_DIR $SKILLS_DIR --with-vector"
  fi
fi

echo ""
ok "安装完成。祝你们重逢愉快。"
echo "  · 蒸馏工具链: scripts/ 目录（parse → segment → distill → build）"
echo "  · 设置备份/迁移: python3 scripts/config.py export-backup <文件>"
