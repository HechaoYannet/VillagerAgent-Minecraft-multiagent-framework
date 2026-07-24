"""
Web 管理后台 — Phase 7 FastAPI 应用

提供 Agent 监控、对话、日志、配置的 Web 界面。

路由:
    /                    — 控制台仪表盘
    /agents              — Agent 管理
    /agents/{name}       — Agent 详情
    /chat                — 对话界面
    /logs                — 日志查看器
    /api/agents          — REST API: Agent 列表
    /api/agents/{name}/command — REST API: 发送指令
    /ws/agent/{name}     — WebSocket: Agent 实时状态

启动:
    uvicorn src.web.app:app --host 0.0.0.0 --port 8080
    或 python -m src.web.app
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# 应用初始化
# ═══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="VillagerAgent 管理后台",
    description="Minecraft AI 伙伴系统 — Web 控制台",
    version="2.0.0",
)

# 模板目录
_TEMPLATE_DIR = Path(__file__).parent / "templates"
_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

# 静态文件
_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# 全局控制器引用 (在 main.py 中注入)
_controller: Any = None  # AgentController
_web_sockets: dict[str, list[WebSocket]] = {}  # agent_name → [ws, ...]


def set_controller(controller):
    """注入 AgentController (由 main.py 调用)"""
    global _controller
    _controller = controller


# ═══════════════════════════════════════════════════════════════════════════════
# 页面路由
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """控制台仪表盘"""
    agents = []
    if _controller:
        health = await _controller.health_check()
        agents = list(health.get("agents", {}).values())

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "title": "控制台",
        "agents": agents,
        "agent_count": len(agents),
        "uptime": _format_uptime(time.monotonic() - _controller._started_at) if _controller else "N/A",
    })


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    """Agent 管理页面"""
    agents = []
    if _controller:
        health = await _controller.health_check()
        agents = list(health.get("agents", {}).values())

    return templates.TemplateResponse("agents.html", {
        "request": request,
        "title": "Agent 管理",
        "agents": agents,
    })


@app.get("/agents/{name}", response_class=HTMLResponse)
async def agent_detail(request: Request, name: str):
    """Agent 详情页面"""
    agent_data = {}
    if _controller:
        agent = _controller.get_agent(name)
        if agent:
            agent_data = agent.get_status()

    return templates.TemplateResponse("agent_detail.html", {
        "request": request,
        "title": f"Agent: {name}",
        "agent_name": name,
        "agent": agent_data,
    })


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    """对话界面"""
    agents = _controller.agent_names if _controller else []
    return templates.TemplateResponse("chat.html", {
        "request": request,
        "title": "对话",
        "agents": agents,
    })


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    """日志查看器"""
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "title": "日志",
    })


# ═══════════════════════════════════════════════════════════════════════════════
# REST API
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/agents")
async def api_list_agents():
    """获取所有 Agent 状态"""
    if not _controller:
        return JSONResponse({"error": "Controller 未启动"}, status_code=503)

    health = await _controller.health_check()
    return JSONResponse(health)


@app.get("/api/agents/{name}")
async def api_get_agent(name: str):
    """获取单个 Agent 状态"""
    if not _controller:
        return JSONResponse({"error": "Controller 未启动"}, status_code=503)

    agent = _controller.get_agent(name)
    if not agent:
        return JSONResponse({"error": f"Agent '{name}' 不存在"}, status_code=404)

    return JSONResponse(agent.get_status())


@app.post("/api/agents/{name}/command")
async def api_send_command(name: str, request: Request):
    """向 Agent 发送指令"""
    if not _controller:
        return JSONResponse({"error": "Controller 未启动"}, status_code=503)

    agent = _controller.get_agent(name)
    if not agent:
        return JSONResponse({"error": f"Agent '{name}' 不存在"}, status_code=404)

    body = await request.json()
    message = body.get("message", "")

    if not message:
        return JSONResponse({"error": "消息不能为空"}, status_code=400)
    if len(message) > 2000:
        return JSONResponse({"error": "消息过长 (最大 2000 字符)"}, status_code=400)

    # 通过 EventBus 发送 USER_INPUT
    from src.core.event_bus import make_user_input
    event = make_user_input(message=message, target=name, source="web.dashboard")
    await _controller.event_bus.publish(event)

    return JSONResponse({"status": "sent", "message": message})


@app.get("/api/logs/actions")
async def api_get_action_logs(agent: str = "", limit: int = 50):
    """获取动作日志"""
    # 路径遍历保护
    if agent and (".." in agent or "/" in agent or "\\" in agent):
        return JSONResponse({"error": "非法 agent 名称"}, status_code=400)
    try:
        log_dir = f"logs/agent/{agent or 'default'}"
        path = os.path.join(log_dir, "actions.jsonl")
        if not os.path.exists(path):
            return JSONResponse([])

        loop = asyncio.get_event_loop()

        def _read():
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            return [json.loads(l) for l in lines[-limit:] if l.strip()]

        data = await loop.run_in_executor(None, _read)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/logs/llm")
async def api_get_llm_logs(limit: int = 50):
    """获取 LLM 请求日志"""
    try:
        path = "logs/llm/requests.jsonl"
        if not os.path.exists(path):
            return JSONResponse([])

        loop = asyncio.get_event_loop()

        def _read():
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            return [json.loads(l) for l in lines[-limit:] if l.strip()]

        data = await loop.run_in_executor(None, _read)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ═══════════════════════════════════════════════════════════════════════════════
# WebSocket — Agent 实时状态
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/agent/{name}")
async def ws_agent_stream(websocket: WebSocket, name: str):
    """WebSocket: 实时推送 Agent 状态"""
    await websocket.accept()

    # 注册 WebSocket
    if name not in _web_sockets:
        _web_sockets[name] = []
    _web_sockets[name].append(websocket)

    try:
        # 持续推送状态更新
        while True:
            if _controller:
                agent = _controller.get_agent(name)
                if agent:
                    status = agent.get_status()
                    await websocket.send_json({
                        "type": "status",
                        "data": status,
                    })
            await asyncio.sleep(2.0)

            # 检查客户端是否还在
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        if name in _web_sockets:
            _web_sockets[name].remove(websocket)


# ═══════════════════════════════════════════════════════════════════════════════
# 健康检查
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health_check():
    """健康检查端点"""
    return JSONResponse({
        "status": "ok",
        "controller": _controller is not None,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════════════

def _format_uptime(seconds: float) -> str:
    """格式化运行时间"""
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


# ═══════════════════════════════════════════════════════════════════════════════
# 启动入口
# ═══════════════════════════════════════════════════════════════════════════════

def start_server(host: str = "0.0.0.0", port: int = 8080, controller=None):
    """启动 Web 服务器 (同步)"""
    if controller:
        set_controller(controller)
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


async def start_server_async(host: str = "0.0.0.0", port: int = 8080, controller=None):
    """启动 Web 服务器 (异步)"""
    if controller:
        set_controller(controller)
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    start_server()
