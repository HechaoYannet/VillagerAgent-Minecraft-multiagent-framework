#!/usr/bin/env bash
# =============================================
# VillagerAgent — 启动 (Linux / macOS / WSL)
# =============================================
# 用法:
#   ./scripts/run.sh                        # 使用默认 config
#   ./scripts/run.sh --agent-only           # 仅 Agent (手动启动桥接)
#   ./scripts/run.sh --web-only             # 仅 Web 后台
#   ./scripts/run.sh --mock                 # MOCK 模式 (无需 MC 服务器)
#
# 环境变量:
#   MINECRAFT_HOST  — MC 服务器地址 (默认 localhost)
#   MINECRAFT_PORT  — MC 服务器端口 (默认 25565)
#   BRIDGE_PORT     — 桥接 Flask 端口 (默认 5000)
#   WEB_PORT        — Web 管理后台端口 (默认 8080)
#   LLM_API_KEY     — LLM API 密钥
# =============================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# ── 颜色 ──
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ── 默认值 ──
MINECRAFT_HOST="${MINECRAFT_HOST:-localhost}"
MINECRAFT_PORT="${MINECRAFT_PORT:-25565}"
BRIDGE_PORT="${BRIDGE_PORT:-5000}"
WEB_PORT="${WEB_PORT:-8080}"
AGENT_USERNAME="${AGENT_USERNAME:-VillagerAgent}"
BRIDGE_MODE="real"

# ── 解析参数 ──
AGENT_ONLY=false
WEB_ONLY=false
MAIN_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent-only) AGENT_ONLY=true; MAIN_ARGS+=("--agent-only"); shift ;;
        --web-only)   WEB_ONLY=true; MAIN_ARGS+=("--web-only"); shift ;;
        --mock)       BRIDGE_MODE="mock"; shift ;;
        --disabled)   BRIDGE_MODE="disabled"; shift ;;
        --debug|-d)   MAIN_ARGS+=("--debug"); shift ;;
        --config|-c)  MAIN_ARGS+=("--config" "$2"); shift 2 ;;
        *)            MAIN_ARGS+=("$1"); shift ;;
    esac
done

# ═══════════════════════════════════════════════════════════════════
# 激活虚拟环境
# ═══════════════════════════════════════════════════════════════════
VENV_DIR="$PROJECT_DIR/.venv"
if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate" 2>/dev/null || source "$VENV_DIR/Scripts/activate" 2>/dev/null
else
    err "虚拟环境未找到, 请先运行: ./scripts/setup.sh"
    exit 1
fi

echo ""
echo "========================================"
echo "🏰 VillagerAgent — 启动"
echo "========================================"
echo "Minecraft:   ${MINECRAFT_HOST}:${MINECRAFT_PORT}"
echo "桥接端口:    ${BRIDGE_PORT}"
echo "Web 端口:    ${WEB_PORT}"
echo "Bridge 模式: ${BRIDGE_MODE}"
echo "Agent 名称:  ${AGENT_USERNAME}"
echo "========================================"
echo ""

# ═══════════════════════════════════════════════════════════════════
# 启动桥接服务器 (除非 web-only / mock / disabled)
# ═══════════════════════════════════════════════════════════════════
BRIDGE_PID=""

cleanup() {
    echo ""
    if [ -n "$BRIDGE_PID" ]; then
        info "正在关闭桥接服务器 (PID: ${BRIDGE_PID})..."
        kill "$BRIDGE_PID" 2>/dev/null || true
        wait "$BRIDGE_PID" 2>/dev/null || true
    fi
    echo "VillagerAgent 已关闭"
}
trap cleanup EXIT SIGTERM SIGINT

if [ "$WEB_ONLY" = false ] && [ "$BRIDGE_MODE" = "real" ]; then
    info "[1/3] 启动 Minecraft 桥接服务器..."
    python env/minecraft_server.py \
        --host "${MINECRAFT_HOST}" \
        --port "${MINECRAFT_PORT}" \
        --local_port "${BRIDGE_PORT}" \
        -U "${AGENT_USERNAME}" &

    BRIDGE_PID=$!

    info "[2/3] 等待桥接服务器就绪..."
    RETRIES=60
    until python -c "
import socket, sys
try:
    s = socket.socket()
    s.settimeout(1)
    s.connect(('localhost', ${BRIDGE_PORT}))
    s.close()
except Exception:
    sys.exit(1)
" 2>/dev/null; do
        RETRIES=$((RETRIES - 1))
        if [ $RETRIES -le 0 ]; then
            err "桥接服务器启动超时 (120s)"
            exit 1
        fi
        sleep 2
    done
    echo -e "${GREEN}[OK]${NC}    桥接服务器就绪"
    echo ""
fi

# ═══════════════════════════════════════════════════════════════════
# 启动主程序
# ═══════════════════════════════════════════════════════════════════
info "[3/3] 启动 VillagerAgent 主系统..."
echo ""

export BRIDGE_MODE
exec python main.py "${MAIN_ARGS[@]}"
