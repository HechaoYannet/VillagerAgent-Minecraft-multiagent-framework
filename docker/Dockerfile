# VillagerAgent Docker 镜像
# 构建: docker build -t villager-agent .
# 运行: docker-compose up

FROM python:3.11-slim

LABEL maintainer="VillagerAgent"
LABEL description="Minecraft AI companion system"

# 安装 Node.js (Mineflayer 桥接需要)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Node.js 依赖
COPY js_bridge/package.json js_bridge/
RUN cd js_bridge && npm install --production

# 复制应用代码
COPY . .

# 创建必要的目录
RUN mkdir -p logs data/world .cache config

# 暴露 Web 管理后台端口
EXPOSE 8080

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# 运行
CMD ["python", "main.py"]
