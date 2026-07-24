#!/usr/bin/env bash
set -e

# =============================================
# VillagerAgent Docker 入口点
# =============================================
# 测试流程:
#   1. 设置环境变量
#   2. 启动 Minecraft 桥接服务器 (Flask :5000)
#   3. 等待桥接服务器就绪
#   4. 启动主 Agent 系统 (FastAPI :8080)
# =============================================

MINECRAFT_HOST="${MINECRAFT_HOST:-localhost}"
MINECRAFT_PORT="${MINECRAFT_PORT:-25565}"
BRIDGE_PORT="${BRIDGE_PORT:-5000}"
WEB_PORT="${WEB_PORT:-8080}"
AGENT_USERNAME="${AGENT_USERNAME:-VillagerAgent}"
BRIDGE_MODE="${BRIDGE_MODE:-real}"

echo "========================================"
echo "🏰 VillagerAgent Docker 容器启动"
echo "========================================"
echo "Minecraft:   ${MINECRAFT_HOST}:${MINECRAFT_PORT}"
echo "桥接服务器:  http://0.0.0.0:${BRIDGE_PORT}"
echo "Web 后台:    http://0.0.0.0:${WEB_PORT}"
echo "Bridge 模式: ${BRIDGE_MODE}"
echo "Agent 名称:  ${AGENT_USERNAME}"
echo "========================================"

if [ "${BRIDGE_MODE}" = "real" ]; then
    echo ""
    echo "[1/3] 启动 Minecraft 桥接服务器..."
    python env/minecraft_server.py \
        --host "${MINECRAFT_HOST}" \
        --port "${MINECRAFT_PORT}" \
        --local_port "${BRIDGE_PORT}" \
        -U "${AGENT_USERNAME}" &

    BRIDGE_PID=$!

    echo "[2/3] 等待桥接服务器就绪 (http://localhost:${BRIDGE_PORT})..."
    # 最多等待 120 秒, 每 2 秒检查一次
    # 使用 Python 检测端口连通性 (比 curl 更可靠, slim 镜像可能无 curl)
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
"; do
        RETRIES=$((RETRIES - 1))
        if [ $RETRIES -le 0 ]; then
            echo "错误: 桥接服务器启动超时 (120s)"
            echo "检查 Minecraft 服务器是否可达: ${MINECRAFT_HOST}:${MINECRAFT_PORT}"
            exit 1
        fi
        sleep 2
    done
    echo "桥接服务器就绪 ✓"

    # 确保桥接服务器退出时清理
    cleanup() {
        echo ""
        echo "正在关闭桥接服务器 (PID: ${BRIDGE_PID})..."
        kill "${BRIDGE_PID}" 2>/dev/null || true
        wait "${BRIDGE_PID}" 2>/dev/null || true
        echo "桥接服务器已关闭"
    }
    trap cleanup EXIT SIGTERM SIGINT
else
    echo ""
    echo "[*] Bridge 模式为 '${BRIDGE_MODE}', 跳过桥接服务器启动"
fi

echo ""
echo "[3/3] 启动 VillagerAgent 主系统..."
exec python main.py "$@"
