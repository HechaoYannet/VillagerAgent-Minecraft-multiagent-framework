"""
Agent 控制器 — Phase 2 Agent 生命周期管理

替代原有的 GlobalController (pipeline/controller.py)，提供:
- 异步 Agent 启停管理
- 健康检查 (ping/pong)
- 配置热更新
- 定时器调度 (TIMER 事件)
- 多 Agent 并行运行

架构:
    AgentController
    ├── EventBus (共享)
    ├── MinecraftBridge (共享)
    ├── Agent["Bot1"] → asyncio.Task
    ├── Agent["Bot2"] → asyncio.Task
    └── TimerTask → 周期性 TIMER 事件

用法:
    controller = AgentController(event_bus=bus, bridge=bridge)
    await controller.start_agent("伙伴", llm_config, personality)
    await controller.run_all()
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from src.core.agent import AsyncBaseAgent
from src.core.bridge import MinecraftBridge
from src.core.event_bus import Event, EventBus, EventType, make_interrupt, make_timer
from src.core.tools import ToolRegistry
from src.llm.factory import init_language_model

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Agent 配置
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class AgentConfig:
    """单个 Agent 的配置"""
    name: str
    llm: dict = field(default_factory=dict)  # LLM 配置 (api_model, api_key, ...)
    personality: dict = field(default_factory=dict)
    system_prompt: str = ""
    max_tool_steps: int = 15
    enabled: bool = True
    # Phase 4
    world_name: str = "default"
    memory_dir: str = "data/memory"
    planning_enabled: bool = True


# ═══════════════════════════════════════════════════════════════════════════════

class AgentController:
    """
    Agent 生命周期管理器

    负责:
    1. 根据配置创建/启动/停止/重启 Agent
    2. 周期性 TIMER 事件 (环境扫描)
    3. 健康检查
    4. 优雅关闭

    所有 Agent 共享同一个 EventBus 和 MinecraftBridge。
    """

    def __init__(
        self,
        event_bus: EventBus,
        bridge: MinecraftBridge,
        timer_interval: float = 5.0,
    ):
        self.event_bus = event_bus
        self.bridge = bridge
        self.timer_interval = timer_interval

        # Agent 注册表
        self._agents: dict[str, AsyncBaseAgent] = {}
        self._agent_tasks: dict[str, asyncio.Task] = {}
        self._agent_configs: dict[str, AgentConfig] = {}

        # 内部任务
        self._timer_task: Optional[asyncio.Task] = None
        self._running = False

        # 信号处理
        self._shutdown_event = asyncio.Event()

        # 统计
        self._started_at: float = 0.0

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def start(self):
        """启动控制器 — 启动 EventBus + Bridge + 定时器"""
        if self._running:
            return

        self._running = True
        self._started_at = time.monotonic()

        # 启动 EventBus
        await self.event_bus.start()

        # 启动 Bridge
        await self.bridge.start()

        # 启动定时器
        self._timer_task = asyncio.create_task(
            self._timer_loop(), name="controller-timer"
        )

        # 信号处理
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig, lambda: asyncio.create_task(self.shutdown())
                )
            except NotImplementedError:
                # Windows 不支持 add_signal_handler
                pass

        logger.info(
            f"AgentController 已启动 "
            f"(定时器间隔: {self.timer_interval}s)"
        )

    async def shutdown(self):
        """优雅关闭 — 停止所有 Agent + EventBus + Bridge"""
        logger.info("正在关闭 AgentController...")
        self._running = False
        self._shutdown_event.set()

        # 停止所有 Agent
        for name in list(self._agents.keys()):
            await self.stop_agent(name)

        # 停止定时器
        if self._timer_task:
            self._timer_task.cancel()
            try:
                await self._timer_task
            except asyncio.CancelledError:
                pass

        # 停止 Bridge
        await self.bridge.stop()

        # 停止 EventBus
        await self.event_bus.stop()

        logger.info("AgentController 已关闭")

    async def run_forever(self):
        """启动所有已配置的 Agent 并持续运行直到 shutdown"""
        await self.start()

        # 启动所有已配置的 Agent
        for name, config in self._agent_configs.items():
            if config.enabled:
                await self.start_agent(name, config)

        # 等待 shutdown 信号
        await self._shutdown_event.wait()

    # ── Agent 管理 ──────────────────────────────────────────────────

    def configure_agent(self, config: AgentConfig):
        """预配置一个 Agent (不启动)"""
        self._agent_configs[config.name] = config
        logger.info(f"Agent '{config.name}' 已配置")

    async def start_agent(
        self,
        name: str,
        config: Optional[AgentConfig] = None,
    ) -> AsyncBaseAgent:
        """
        启动一个 Agent — 创建 LLM 客户端 + 工具注册表 + asyncio Task

        Args:
            name: Agent 名称
            config: Agent 配置 (如果之前已 configure，可省略)

        Returns:
            AsyncBaseAgent 实例
        """
        if name in self._agents:
            logger.warning(f"Agent '{name}' 已在运行中，先停止")
            await self.stop_agent(name)

        if config:
            self._agent_configs[name] = config
        elif name not in self._agent_configs:
            raise ValueError(f"Agent '{name}' 未配置。请先调用 configure_agent()。")

        cfg = self._agent_configs[name]

        # 初始化 LLM 客户端
        llm = init_language_model(cfg.llm)

        # Phase 4: 初始化持久化记忆
        from src.core.world_config import WorldConfig
        from src.core.long_term_memory import LongTermMemory
        from src.core.planning import TaskPlanner

        world_config = WorldConfig(world_name=cfg.world_name)
        await world_config.load()

        long_term_memory = LongTermMemory(world_name=cfg.world_name, data_dir=cfg.memory_dir)
        await long_term_memory.load()

        # 初始化工具注册表 (加载预定义 Minecraft 工具)
        tools = ToolRegistry()
        # 注册 Minecraft 工具 Schema (实际执行由 Agent → Bridge 完成)
        from src.core.tools import MINECRAFT_TOOL_DEFINITIONS
        for tool_def in MINECRAFT_TOOL_DEFINITIONS:
            tools.register(
                name=tool_def.name,
                description_zh=tool_def.description,
                parameters=list(tool_def.parameters),
                category="minecraft",
                # handler=None: Agent 通过 bridge.execute() 直接执行
                handler=lambda **kwargs: {"status": True, "message": "executed via bridge"},
            )

        # Phase 4: 创建规划器 (需要先有 ConversationMemory, 先创建 agent 再设 planner)
        # 简化: 在 Agent 内创建 planner

        # 创建 Agent
        agent = AsyncBaseAgent(
            name=name,
            event_bus=self.event_bus,
            llm=llm,
            tools=tools,
            bridge=self.bridge,
            personality=cfg.personality,
            system_prompt=cfg.system_prompt,
            max_tool_steps=cfg.max_tool_steps,
            world_config=world_config,
            long_term_memory=long_term_memory,
        )

        # Phase 4: 创建规划器 (需要 agent.memory)
        if cfg.planning_enabled:
            planner = TaskPlanner(
                llm=llm,
                memory=long_term_memory,
                world_config=world_config,
                bridge=self.bridge,
                conversation=agent.memory,
            )
            agent.planner = planner

        # 启动 Agent (创建 asyncio Task)
        task = asyncio.create_task(agent.run(), name=f"agent-{name}")
        self._agents[name] = agent
        self._agent_tasks[name] = task

        logger.info(f"Agent '{name}' 已启动")
        return agent

    async def stop_agent(self, name: str):
        """停止一个 Agent — 发送 INTERRUPT → 取消 Task"""
        if name not in self._agents:
            logger.warning(f"Agent '{name}' 不存在")
            return

        agent = self._agents[name]

        # 发送中断事件
        await self.event_bus.publish(make_interrupt(name, "Controller 停止"))

        # 停用 Agent
        await agent.stop()

        # 取消 Task
        task = self._agent_tasks.get(name)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        del self._agents[name]
        del self._agent_tasks[name]
        logger.info(f"Agent '{name}' 已停止")

    async def restart_agent(self, name: str):
        """重启 Agent — 保留配置"""
        await self.stop_agent(name)
        await self.start_agent(name)

    # ── 健康检查 ────────────────────────────────────────────────────

    async def health_check(self) -> dict:
        """所有 Agent 的状态报告"""
        agents = {}
        for name, agent in self._agents.items():
            agents[name] = agent.get_status()

        return {
            "controller": {
                "running": self._running,
                "uptime": time.monotonic() - self._started_at,
                "agent_count": len(self._agents),
            },
            "event_bus": {
                "running": self.event_bus.is_running,
                "subscribers": self.event_bus.subscriber_count,
            },
            "agents": agents,
        }

    # ── 定时器 ──────────────────────────────────────────────────────

    async def _timer_loop(self):
        """周期性 TIMER 事件循环"""
        while self._running:
            await asyncio.sleep(self.timer_interval)
            if not self._running:
                break

            await self.event_bus.publish(make_timer(
                interval=self.timer_interval,
                label="world_scan",
            ))

    # ── 查询 ──────────────────────────────────────────────────────────

    def get_agent(self, name: str) -> Optional[AsyncBaseAgent]:
        """获取 Agent 实例"""
        return self._agents.get(name)

    @property
    def agent_names(self) -> list[str]:
        return list(self._agents.keys())

    @property
    def agent_count(self) -> int:
        return len(self._agents)


# ═══════════════════════════════════════════════════════════════════════════════
# 便捷启动函数
# ═══════════════════════════════════════════════════════════════════════════════

async def create_controller_from_config(
    yaml_config: dict,
    bridge_mode: str = "disabled",
) -> AgentController:
    """
    从 YAML 配置一键创建并启动 Controller

    用法:
        config = yaml.safe_load(open("config/default.yaml"))
        controller = await create_controller_from_config(config)
        await controller.run_forever()
    """
    from src.core.bridge import BridgeMode

    bus = EventBus()
    bridge = MinecraftBridge(
        event_bus=bus,
        mode=BridgeMode(bridge_mode),
        base_url=f"http://{yaml_config['minecraft']['host']}:5000",
        agent_name=yaml_config.get("agents", {}).get("default_name_prefix", "伙伴"),
    )

    controller = AgentController(event_bus=bus, bridge=bridge)

    # 从配置创建 Agent
    agent_count = yaml_config.get("agents", {}).get("default_count", 1)
    name_prefix = yaml_config.get("agents", {}).get("default_name_prefix", "伙伴")

    for i in range(agent_count):
        name = f"{name_prefix}{i + 1}" if agent_count > 1 else name_prefix
        controller.configure_agent(AgentConfig(
            name=name,
            llm=yaml_config.get("llm", {}),
            personality={},
        ))

    return controller
