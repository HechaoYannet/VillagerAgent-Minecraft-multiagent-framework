# VillagerAgent 2.0 — Production Docker Image
# 构建: docker build -t villager-agent .
# 运行: docker-compose -f docker/docker-compose.yml up

FROM python:3.11-slim

LABEL maintainer="VillagerAgent"
LABEL description="Minecraft AI Companion System — Phase 0-8"

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Node.js dependencies (Mineflayer bridge)
COPY js_bridge/package.json js_bridge/
RUN cd js_bridge && npm install --production && cd ..

# Application code
COPY . .

# Directories
RUN mkdir -p logs data/world data/memory .cache config

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

ENTRYPOINT ["python", "main.py"]
