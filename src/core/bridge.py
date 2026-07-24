"""
Minecraft Bridge — EventBus ↔ Minecraft 服务器桥接层

职责:
- 从 Minecraft 服务器获取事件，注入 EventBus
- 执行 Agent 的工具调用 (通过 Flask HTTP 服务器)
- 三种运行模式: REAL / MOCK / DISABLED

架构:
    Minecraft 服务器 (1.21.1 Fabric)
        ↕ (Mineflayer Bot via JSPyBridge)
    Flask HTTP 服务器 (env/minecraft_server.py)
        ↕ (HTTP REST API)
    MinecraftBridge ←→ EventBus
        ↕
    AsyncBaseAgent + AgentController

用法:
    bridge = MinecraftBridge(event_bus=bus, base_url="http://localhost:5000")
    await bridge.start()
    result = await bridge.execute("mineBlock", {"x": 10, "y": 64, "z": 20})
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional

from src.core.event_bus import (
    Event,
    EventBus,
    EventType,
    make_chat,
    make_interrupt,
    make_timer,
    make_user_input,
    make_world_change,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 运行模式
# ═══════════════════════════════════════════════════════════════════════════════

class BridgeMode(Enum):
    REAL = "real"        # 连接真实 Minecraft 服务器
    MOCK = "mock"        # 模拟 Minecraft 世界 (测试/开发)
    DISABLED = "disabled"  # 纯 LLM 模式


# ═══════════════════════════════════════════════════════════════════════════════
# MOCK 数据
# ═══════════════════════════════════════════════════════════════════════════════

MOCK_WORLD_STATE = {
    "my_position": [100, 64, 200],
    "health": 20,
    "food": 20,
    "timeOfDay": "day",
    "dimension": "overworld",
    "nearby_entities": [
        {"name": "sheep", "position": [105, 64, 205]},
        {"name": "chicken", "position": [98, 64, 195]},
    ],
    "inventory_summary": "oak_planks x32, stick x16, stone_pickaxe x1",
    "held_item": "stone_pickaxe",
    "oxygen": 20,
    "saturation": 5,
}

MOCK_BLOCKS_NEARBY = [
    {"name": "grass_block", "position": [100, 63, 200]},
    {"name": "oak_log", "position": [102, 64, 198]},
    {"name": "stone", "position": [100, 60, 200]},
    {"name": "coal_ore", "position": [101, 58, 201]},
]


# ═══════════════════════════════════════════════════════════════════════════════
# 工具执行结果
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class BridgeResult:
    """Bridge 工具执行结果"""
    tool_name: str
    status: bool
    message: str
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict:
        return {
            "tool": self.tool_name,
            "status": self.status,
            "message": self.message,
            "data": self.data,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# MinecraftBridge
# ═══════════════════════════════════════════════════════════════════════════════

class MinecraftBridge:
    """
    Minecraft ↔ EventBus 桥接层

    三种模式:
        REAL:   HTTP → Flask 服务器 → Mineflayer → Minecraft
        MOCK:   模拟 Minecraft 交互 (测试/开发)
        DISABLED: 纯 LLM 模式 (无 Minecraft 交互)

    REAL 模式下需运行的端点 (env/minecraft_server.py):
        GET  /api/world           → 获取世界状态
        GET  /api/chat/new        → 获取新聊天消息
        POST /api/action          → 执行 Bot 动作
        POST /api/chat/send       → 发送聊天消息
    """

    def __init__(
        self,
        event_bus: EventBus,
        mode: BridgeMode = BridgeMode.DISABLED,
        base_url: str = "http://localhost:5000",
        agent_name: str = "VillagerAgent",
        world_poll_interval: float = 5.0,
        chat_poll_interval: float = 1.0,
    ):
        self.event_bus = event_bus
        self.mode = mode
        self.base_url = base_url.rstrip("/")
        self.agent_name = agent_name
        self.world_poll_interval = world_poll_interval
        self.chat_poll_interval = chat_poll_interval

        # HTTP 客户端 (延迟初始化)
        self._http_client: Any = None  # httpx.AsyncClient
        self._running = False

        # 轮询任务
        self._world_task: Optional[asyncio.Task] = None
        self._chat_task: Optional[asyncio.Task] = None

        # MOCK 状态
        self._mock_world = MOCK_WORLD_STATE.copy()
        self._mock_chat_queue: list[dict] = []

        # LLM 开关
        self._llm_enabled = True

    @property
    def llm_enabled(self) -> bool:
        return self._llm_enabled

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def start(self):
        """启动桥接 — 开始轮询 Minecraft 事件"""
        if self._running:
            return

        self._running = True

        if self.mode == BridgeMode.REAL:
            # 延迟导入 httpx (必需依赖)
            try:
                import httpx
                # 策略超时: 轮询短超时, 动作(寻路/挖掘)需要长超时
                self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(
                    timeout=120.0,  # 默认上限
                    connect=5.0,
                ))
            except ImportError:
                raise ImportError(
                    "REAL 模式需要 httpx。请安装: pip install httpx"
                )

        if self.mode == BridgeMode.REAL or self.mode == BridgeMode.MOCK:
            if self.mode == BridgeMode.REAL:
                self._world_task = asyncio.create_task(
                    self._poll_world_state(), name="bridge-world-poll"
                )
            self._chat_task = asyncio.create_task(
                self._poll_chat(), name="bridge-chat-poll"
            )
            logger.info(f"MinecraftBridge 已启动 ({self.mode.value.upper()} 模式{', ' + self.base_url if self.mode == BridgeMode.REAL else ''})")
        else:
            logger.info("MinecraftBridge 已启动 (DISABLED 模式)")

    async def stop(self):
        """停止桥接"""
        self._running = False
        for task in [self._world_task, self._chat_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        logger.info("MinecraftBridge 已停止")

    # ── HTTP 帮助函数 ────────────────────────────────────────────────

    async def _fetch(self, path: str, method: str = "GET", json_data: dict = None, timeout: float = None) -> dict:
        """发送 HTTP 请求到 Flask 服务器"""
        if self.mode != BridgeMode.REAL or not self._http_client:
            return {}

        url = f"{self.base_url}{path}"
        # 动作路由允许更长的超时（寻路可能需要很久）
        kwargs = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            if method == "GET":
                resp = await self._http_client.get(url, **kwargs)
            else:
                resp = await self._http_client.post(url, json=json_data, **kwargs)
            return resp.json() if resp.status_code == 200 else (resp.json() if resp.content_type == "application/json" else {"status": False, "message": f"HTTP {resp.status_code}"})
        except Exception as e:
            logger.warning(f"HTTP 请求失败 {method} {url}: {e}")
            return {"status": False, "message": str(e)}

    # ── 世界状态轮询 ────────────────────────────────────────────────

    async def _poll_world_state(self):
        """周期性轮询世界状态 → WorldChangeEvent"""
        while self._running:
            try:
                world = await self.get_world_state()
                if world:
                    await self.event_bus.publish(make_world_change("state_update", world))
            except Exception as e:
                logger.warning(f"世界状态轮询错误: {e}")

            await asyncio.sleep(self.world_poll_interval)

    # ── 聊天轮询 ─────────────────────────────────────────────────────

    async def _poll_chat(self):
        """轮询新聊天消息 → ChatEvent / UserInputEvent"""
        while self._running:
            try:
                if self.mode == BridgeMode.REAL:
                    messages = await self._fetch("/api/chat/new")
                    if messages:
                        for msg in messages if isinstance(messages, list) else [messages]:
                            await self._process_chat_message(msg)
                elif self.mode == BridgeMode.MOCK:
                    while self._mock_chat_queue:
                        msg = self._mock_chat_queue.pop(0)
                        await self._process_chat_message(msg)
            except Exception as e:
                logger.warning(f"聊天轮询错误: {e}")

            await asyncio.sleep(self.chat_poll_interval)

    async def _process_chat_message(self, msg: dict):
        """处理聊天消息 → 发布到 EventBus"""
        text = msg.get("text", msg.get("message", ""))
        player = msg.get("player", msg.get("username", "unknown"))

        if not text:
            return

        # ── LLM 开关指令 (始终生效，不受 _llm_enabled 影响) ──
        if text.strip() == "@enable-llm":
            self._llm_enabled = True
            logger.info(f"LLM 已启用 (by {player})")
            await self._send_chat_response(f"[System] LLM 已启用 — @ai 指令现在会处理")
            return

        if text.strip() == "@disable-llm":
            self._llm_enabled = False
            logger.info(f"LLM 已禁用 (by {player})")
            # 中断 Agent 当前任务 (如果正在处理中)
            await self.event_bus.publish(make_interrupt(
                target=self.agent_name,
                reason="LLM 已禁用",
            ))
            await self._send_chat_response(f"[System] LLM 已禁用 — 所有 LLM 调用已停止 (用 @enable-llm 恢复)")
            return

        # ── LLM 禁用时忽略所有 @ai 指令 ──
        if not self._llm_enabled:
            return

        # ── @ai 指令 → USER_INPUT ──
        if text.startswith("@ai"):
            clean_text = text[3:].strip()  # 去掉 "@ai" 前缀
            if clean_text:
                await self.event_bus.publish(make_user_input(
                    message=clean_text,
                    target=self.agent_name,
                    player=player,
                ))

    async def _send_chat_response(self, message: str):
        """发送系统消息到 Minecraft 聊天 (通过 Flask 服务器)"""
        if self.mode == BridgeMode.REAL:
            await self._fetch("/api/chat/send", method="POST", json_data={
                "message": message,
            })
        elif self.mode == BridgeMode.MOCK:
            self._mock_chat_queue.append({
                "player": "System",
                "text": message,
            })

    # ── 世界状态查询 ────────────────────────────────────────────────

    async def get_world_state(self) -> dict:
        """获取当前世界状态"""
        if self.mode == BridgeMode.REAL:
            return await self._fetch("/api/world")
        elif self.mode == BridgeMode.MOCK:
            # 随机微调位置模拟真实移动
            self._mock_world["my_position"][0] += random.randint(-1, 1)
            self._mock_world["my_position"][2] += random.randint(-1, 1)
            return self._mock_world.copy()
        else:
            return {}

    async def get_agent_position(self, agent_name: str = "") -> Optional[list[float]]:
        """获取 Agent 当前位置"""
        world = await self.get_world_state()
        return world.get("my_position")

    async def scan_nearby_blocks(self, radius: int = 8, block_name: str = "") -> list[dict]:
        """扫描附近方块"""
        if self.mode == BridgeMode.MOCK:
            if block_name:
                return [b for b in MOCK_BLOCKS_NEARBY if block_name in b["name"]]
            return MOCK_BLOCKS_NEARBY
        return await self._fetch("/api/blocks/nearby", json_data={
            "radius": radius, "block": block_name,
        })

    # ── 工具执行 ─────────────────────────────────────────────────────

    async def execute(self, tool_name: str, args: dict) -> BridgeResult:
        """
        执行 Minecraft 工具

        将通过 EventBus 调用，由 Bridge 转发到 Minecraft 服务器。

        Args:
            tool_name: 工具名称 (如 mineBlock, placeBlock, moveTo)
            args: 工具参数

        Returns:
            BridgeResult: 执行结果
        """
        if self.mode == BridgeMode.DISABLED:
            return BridgeResult(
                tool_name=tool_name,
                status=True,
                message=f"[DISABLED] {tool_name}({json.dumps(args, ensure_ascii=False)}) — 纯 LLM 模式",
            )

        if self.mode == BridgeMode.MOCK:
            return await self._mock_execute(tool_name, args)

        # REAL 模式 — HTTP 调用 Flask 服务器 (长超时: 寻路可能很慢)
        result = await self._fetch("/api/action", method="POST", json_data={
            "tool": tool_name,
            "args": args,
        }, timeout=120.0)

        success = result.get("status", False)
        message = result.get("message", str(result))

        return BridgeResult(
            tool_name=tool_name,
            status=success,
            message=message,
            data=result.get("data", {}),
        )

    async def _mock_execute(self, tool_name: str, args: dict) -> BridgeResult:
        """模拟工具执行 (测试/开发)"""
        # 模拟延迟 (异步, 不阻塞事件循环)
        await asyncio.sleep(0.1)

        # 更新 MOCK 世界状态
        if tool_name == "moveTo":
            self._mock_world["my_position"] = [
                args.get("x", 0), args.get("y", 64), args.get("z", 0)
            ]
        elif tool_name == "mineBlock":
            block_pos = [args.get("x", 0), args.get("y", 0), args.get("z", 0)]
            # 从 nearby blocks 中移除
            MOCK_BLOCKS_NEARBY[:] = [
                b for b in MOCK_BLOCKS_NEARBY
                if b.get("position") != block_pos
            ]
        elif tool_name == "sendChat":
            self._mock_chat_queue.append({
                "player": self.agent_name,
                "text": args.get("message", ""),
            })

        return BridgeResult(
            tool_name=tool_name,
            status=True,
            message=f"[MOCK] 执行 {tool_name}{args} — 成功",
            data={"mock": True},
        )

    def inject_mock_chat(self, player: str, message: str):
        """注入模拟聊天消息 (测试用)"""
        if self.mode == BridgeMode.MOCK:
            self._mock_chat_queue.append({
                "player": player,
                "text": message,
            })

    def inject_mock_world_change(self, change: dict):
        """注入模拟世界变化 (测试用)"""
        if self.mode == BridgeMode.MOCK:
            self._mock_world.update(change)

    # ── 聊天发送 ─────────────────────────────────────────────────────

    async def send_chat(self, message: str):
        """发送聊天消息到 Minecraft"""
        if self.mode == BridgeMode.REAL:
            await self._fetch("/api/chat/send", method="POST", json_data={
                "message": message,
            })
        elif self.mode == BridgeMode.MOCK:
            self._mock_chat_queue.append({
                "player": self.agent_name,
                "text": message,
            })
