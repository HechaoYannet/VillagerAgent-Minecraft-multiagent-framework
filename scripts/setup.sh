#!/usr/bin/env bash
# =============================================
# VillagerAgent — 一键安装 (Linux / macOS / WSL)
# =============================================
# 用法:
#   chmod +x scripts/setup.sh
#   ./scripts/setup.sh              # 基础安装
#   ./scripts/setup.sh --full       # 含 Gemini + ChromaDB + 开发工具
#   ./scripts/setup.sh --dev        # 含开发工具
# =============================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# ── 颜色 ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

echo ""
echo "========================================"
echo "🏰 VillagerAgent — 环境安装"
echo "========================================"
echo ""

# ═══════════════════════════════════════════════════════════════════
# 1. 检查 Python
# ═══════════════════════════════════════════════════════════════════
info "检查 Python..."
PYTHON=""
for cmd in python3.12 python3.11 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=$("$cmd" -c "import sys; print(sys.version_info.major)")
        minor=$("$cmd" -c "import sys; print(sys.version_info.minor)")
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON="$cmd"
            ok "找到 Python $ver ($(which "$PYTHON"))"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    err "需要 Python >= 3.11, 但未找到"
    echo "   安装: https://www.python.org/downloads/"
    echo "   或:   sudo apt install python3.11 python3.11-venv"
    exit 1
fi

# ═══════════════════════════════════════════════════════════════════
# 2. 检查 Node.js
# ═══════════════════════════════════════════════════════════════════
info "检查 Node.js (JSPyBridge 需要)..."
NODE=""
for cmd in node nodejs; do
    if command -v "$cmd" &>/dev/null; then
        NODE="$cmd"
        ver=$("$NODE" --version 2>/dev/null || true)
        if [ -n "$ver" ]; then
            major=$(echo "$ver" | sed 's/v//' | cut -d. -f1)
            if [ "$major" -ge 18 ]; then
                ok "找到 Node.js $ver ($(which "$NODE"))"
            else
                warn "Node.js $ver 版本较旧, 建议 >= 18"
            fi
        fi
        break
    fi
done

if [ -z "$NODE" ]; then
    warn "未找到 Node.js, JSPyBridge 需要 Node.js"
    echo "   安装: https://nodejs.org/"
    echo "   或:   curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash - && sudo apt install nodejs"
    echo ""
    read -p "是否继续安装 (Python 依赖仍可安装)? [y/N] " yn
    case $yn in
        [Yy]*) ;;
        *) exit 1 ;;
    esac
fi

# ═══════════════════════════════════════════════════════════════════
# 3. 创建虚拟环境
# ═══════════════════════════════════════════════════════════════════
VENV_DIR="$PROJECT_DIR/.venv"
if [ -d "$VENV_DIR" ]; then
    info "虚拟环境已存在: $VENV_DIR"
else
    info "创建虚拟环境..."
    "$PYTHON" -m venv "$VENV_DIR"
    ok "虚拟环境已创建"
fi

# 激活
source "$VENV_DIR/bin/activate" 2>/dev/null || source "$VENV_DIR/Scripts/activate" 2>/dev/null
ok "虚拟环境已激活"

# 升级 pip
pip install --upgrade pip --quiet

# ═══════════════════════════════════════════════════════════════════
# 4. 安装 Python 依赖
# ═══════════════════════════════════════════════════════════════════
INSTALL_MODE="${1:-base}"

info "安装 Python 依赖 (模式: $INSTALL_MODE)..."

case "$INSTALL_MODE" in
    --full|-f)
        pip install -e ".[full]" --quiet
        ok "已安装: 核心 + Gemini + ChromaDB + 开发工具"
        ;;
    --dev|-d)
        pip install -e ".[dev,memory]" --quiet
        ok "已安装: 核心 + ChromaDB + 开发工具"
        ;;
    --gemini|-g)
        pip install -e ".[gemini]" --quiet
        ok "已安装: 核心 + Google Gemini"
        ;;
    *)
        pip install -e "." --quiet
        ok "已安装: 核心依赖"
        ;;
esac

# ═══════════════════════════════════════════════════════════════════
# 5. 安装 Node.js 依赖 (JSPyBridge 桥接)
# ═══════════════════════════════════════════════════════════════════
# package.json 源文件在 js_bridge/, 复制到根目录后安装
# (JSPyBridge require() 从工作目录解析 node_modules)
if [ -n "$NODE" ]; then
    info "安装 Node.js 依赖 (Mineflayer)..."
    # 复制 js_bridge/package.json 到根目录
    cp js_bridge/package.json package.json
    # prismarine-viewer / socks5-client 是 optionalDependencies, 编译失败不会阻塞
    npm install --production --silent 2>/dev/null || npm install --production
    ok "Node.js 依赖已安装"
fi

# ═══════════════════════════════════════════════════════════════════
# 6. 配置文件
# ═══════════════════════════════════════════════════════════════════
if [ ! -f "config/secrets.yaml" ]; then
    if [ -f "config/secrets.template.yaml" ]; then
        cp config/secrets.template.yaml config/secrets.yaml
        ok "已创建 config/secrets.yaml (从模板)"
        warn "请编辑 config/secrets.yaml 填入你的 API 密钥!"
    else
        info "未找到 config/secrets.template.yaml, 跳过"
    fi
else
    ok "config/secrets.yaml 已存在"
fi

# ═══════════════════════════════════════════════════════════════════
# 7. 运行时目录
# ═══════════════════════════════════════════════════════════════════
mkdir -p logs data/world data/memory .cache
ok "运行时目录已创建"

# ═══════════════════════════════════════════════════════════════════
# 完成
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "========================================"
echo "✅ 安装完成!"
echo "========================================"
echo ""
echo "下一步:"
echo "  1. 编辑 config/secrets.yaml 填入 API 密钥"
echo "  2. 确保 Minecraft 服务器已启动"
echo "  3. 运行: ./scripts/run.sh"
echo ""
echo "或手动:"
echo "  2. source .venv/bin/activate"
echo "  3. python env/minecraft_server.py -H <MC_HOST> -P 25565 -LP 5000 -U VillagerAgent"
echo "  4. 等桥接就绪后: python main.py"
echo ""
echo "环境变量 (可选):"
echo "  export MINECRAFT_HOST=你的MC服务器IP"
echo "  export LLM_API_KEY=sk-your-api-key"
echo ""
