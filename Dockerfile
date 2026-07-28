# VillagerAgent — Minecraft AI 伙伴系统
# 构建: docker build -t villager-agent .
# 运行: docker compose -f docker/docker-compose.yml up
#
# 架构:
#   env/minecraft_server.py  → Flask :5000 (Mineflayer 桥接, 需先启动)
#   main.py                  → FastAPI :8080 (Agent 系统, 依赖桥接服务)
#
# 测试流程:
#   docker run -e MINECRAFT_HOST=... -e LLM_API_KEY=... villager-agent

FROM python:3.11-slim

LABEL maintainer="VillagerAgent"
LABEL description="Minecraft AI Companion System"

# ═══════════════════════════════════════════════════════════════════
# 系统依赖
# ═══════════════════════════════════════════════════════════════════
# - Node.js 22 LTS: JSPyBridge + Mineflayer 桥接
# - build-essential python3-dev: JSPyBridge (javascript 包) 编译
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    git \
    build-essential \
    python3-dev \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ═══════════════════════════════════════════════════════════════════
# Python 依赖 — 先复制 requirements 以利用 Docker 层缓存
# ═══════════════════════════════════════════════════════════════════
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ═══════════════════════════════════════════════════════════════════
# Node.js 依赖 — Mineflayer 桥接 (JSPyBridge)
# ═══════════════════════════════════════════════════════════════════
# 重要: JSPyBridge require() 从工作目录 (/app) 解析 node_modules,
#       因此必须在 /app 下安装, 不能只装在 js_bridge/ 子目录。
# prismarine-viewer / socks5-client 是 optionalDependencies, 编译失败不阻塞
COPY js_bridge/package.json /tmp/package.json
RUN cd /app && cp /tmp/package.json package.json && npm install --production

# ═══════════════════════════════════════════════════════════════════
# 应用代码
# ═══════════════════════════════════════════════════════════════════
COPY . .

# 运行时目录
RUN mkdir -p logs data/world data/memory .cache config

EXPOSE 5000 8080

# 健康检查 — 仅检查主 Agent 的 FastAPI 端点
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# ═══════════════════════════════════════════════════════════════════
# 入口点
# ═══════════════════════════════════════════════════════════════════
# 环境变量:
#   MINECRAFT_HOST  — Minecraft 服务器地址 (默认 localhost)
#   MINECRAFT_PORT  — Minecraft 服务器端口 (默认 25565)
#   BRIDGE_PORT     — 桥接 Flask 端口 (默认 5000)
#   WEB_PORT        — Web 管理后台端口 (默认 8080)
#   LLM_API_KEY     — LLM API 密钥
#   BRIDGE_MODE     — real | mock | disabled (默认 real)
#
# 若 BRIDGE_MODE=real, entrypoint 会在后台启动桥接服务器,
# 等待就绪后再启动 main.py

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
