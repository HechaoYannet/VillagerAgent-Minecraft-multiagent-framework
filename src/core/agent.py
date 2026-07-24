"""
异步 Agent — Phase 2 核心组件

基于事件驱动的全双工 Agent 实现，替代原有的同步 BaseAgent (pipeline/agent.py)。

特性:
- Agent 状态机: IDLE → LISTENING → THINKING → ACTING → REFLECTING → IDLE
- Phase 3 原生工具调用 (ToolCallLoop 模式)
- 流式思考过程回调 (实时 UI 展示)
- 中断处理 (任意状态下安全停止)
- 主动行为检测 (TIMER 触发健康/饥饿检查)
- 性格系统集成

架构:
    EventBus ←→ AsyncBaseAgent ←→ LLM (Phase 3 OpenAICompatClient)
                     ↕
    MinecraftBridge (工具执行)
                     ↕
    ConversationMemory (短期记忆)

用法:
    agent = AsyncBaseAgent(
        name="伙伴",
        event_bus=bus,
        llm=client,
        tools=registry,
        bridge=bridge,
    )
    await agent.run()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from enum import Enum, auto
from typing import Any, Callable, Optional

from src.core.bridge import BridgeResult, MinecraftBridge
from src.core.conversation import ConversationMemory
from src.core.event_bus import Event, EventBus, EventType
from src.core.tools import ToolRegistry
from src.llm.base import (
    AssistantMessage,
    ChatResult,
    Message,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from src.llm.openai_compat import OpenAICompatClient

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Agent 状态机
# ═══════════════════════════════════════════════════════════════════════════════

class AgentState(Enum):
    """
    Agent 状态机

    IDLE      — 等待事件 (默认状态)
    LISTENING — 收到用户输入，准备处理
    THINKING  — LLM 推理中 (可被 INTERRUPT)
    ACTING    — 执行工具中 (可被 INTERRUPT)
    REFLECTING— 任务完成后反思评估
    STOPPED   — 已停止
    """
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    ACTING = auto()
    REFLECTING = auto()
    STOPPED = auto()


# 可中断状态: 收到 INTERRUPT 时强制回到 IDLE
INTERRUPTIBLE_STATES = {AgentState.THINKING, AgentState.ACTING}


# ═══════════════════════════════════════════════════════════════════════════════
# 流式回调类型
# ═══════════════════════════════════════════════════════════════════════════════

StreamCallback = Callable[[str, str], Any]
"""
流式回调: async def callback(chunk_type: str, content: str) -> None

chunk_type:
    "reasoning"  — DeepSeek 思考链
    "content"    — LLM 输出文本
    "tool_call"  — 工具调用名称
    "tool_result"— 工具执行结果
    "state"      — 状态变更
"""


# ═══════════════════════════════════════════════════════════════════════════════
# AsyncBaseAgent
# ═══════════════════════════════════════════════════════════════════════════════

class AsyncBaseAgent:
    """
    异步 Agent — 事件驱动的 Minecraft AI 伙伴

    Attributes:
        name: Agent 名称
        state: 当前状态 (AgentState)
        is_processing: 是否正在处理事件
    """

    def __init__(
        self,
        name: str,
        event_bus: EventBus,
        llm: OpenAICompatClient,
        tools: ToolRegistry,
        bridge: MinecraftBridge,
        personality: Optional[dict] = None,
        system_prompt: str = "",
        max_tool_steps: int = 15,
        stream_callback: Optional[StreamCallback] = None,
        log_dir: str = "logs",
        # Phase 4: 持久化记忆与规划
        world_config = None,      # WorldConfig
        long_term_memory = None,  # LongTermMemory
        planner = None,           # TaskPlanner
        # Phase 5: 情绪与交互
        emotion_engine = None,    # EmotionEngine
        interaction_manager = None,  # InteractionManager
    ):
        self.name = name
        self.event_bus = event_bus
        self.llm = llm
        self.tools = tools
        self.bridge = bridge
        self.max_tool_steps = max_tool_steps
        self.stream_callback = stream_callback

        # Phase 4: 持久化记忆与规划
        self.world_config = world_config
        self.long_term_memory = long_term_memory
        self.planner = planner

        # Phase 5: 情绪与交互
        self.emotion_engine = emotion_engine
        self.interaction = interaction_manager

        # 状态
        self.state = AgentState.IDLE
        self._interrupt_flag = asyncio.Event()
        self._stopped = asyncio.Event()
        self._current_task: Optional[asyncio.Task] = None
        self._last_active_at = time.monotonic()

        # 短期记忆
        self.memory = ConversationMemory(
            system_prompt=system_prompt,
            personality=personality or {},
            agent_name=name,
        )

        # 将工具描述注入系统提示词
        self.memory.update_tool_descriptions(
            self.tools.to_tool_descriptions_text(lang="zh")
        )

        # 日志
        self.log_dir = log_dir

        # 统计
        self.stats = AgentStats()

    # ── 属性 ──────────────────────────────────────────────────────────

    @property
    def is_idle(self) -> bool:
        return self.state == AgentState.IDLE

    @property
    def is_processing(self) -> bool:
        return self.state in (AgentState.LISTENING, AgentState.THINKING, AgentState.ACTING)

    @property
    def last_active_at(self) -> float:
        return self._last_active_at

    # ── 主循环 ──────────────────────────────────────────────────────

    async def run(self):
        """
        启动 Agent — 订阅事件总线并进入 IDLE 状态

        作为 asyncio.Task 运行。持续监听事件直到收到 STOPPED 信号。
        """
        # 订阅事件
        self.event_bus.subscribe(
            EventType.USER_INPUT,
            self._handle_user_input,
            target=self.name,
        )
        self.event_bus.subscribe(
            EventType.INTERRUPT,
            self._handle_interrupt,
            target=self.name,
        )
        self.event_bus.subscribe(
            EventType.CHAT,
            self._handle_chat,
        )
        self.event_bus.subscribe(
            EventType.TIMER,
            self._handle_timer,
        )

        self.state = AgentState.IDLE
        self._log_state("Agent 已启动，等待事件...")

        if self.stream_callback:
            await self.stream_callback("state", f"{self.name} 就绪")

        # 保持运行直到 STOPPED (事件驱动, 无忙轮询)
        await self._stopped.wait()

    async def stop(self):
        """停止 Agent"""
        self.state = AgentState.STOPPED
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
        # 取消所有事件订阅 (防止 restart 时重复注册)
        self.event_bus.unsubscribe(EventType.USER_INPUT, self._handle_user_input)
        self.event_bus.unsubscribe(EventType.INTERRUPT, self._handle_interrupt)
        self.event_bus.unsubscribe(EventType.CHAT, self._handle_chat)
        self.event_bus.unsubscribe(EventType.TIMER, self._handle_timer)
        self._stopped.set()
        self._log_state("Agent 已停止")

    # ── 事件处理器 ──────────────────────────────────────────────────

    async def _handle_user_input(self, event: Event):
        """处理用户指令 — 进入 LISTENING 状态"""
        if self.is_processing:
            # 已在处理中 → 先中断当前任务
            await self._interrupt_current_task("新指令到达")

        self._last_active_at = time.monotonic()
        self._interrupt_flag.clear()

        message = event.data.get("message", "")
        player = event.data.get("player", "玩家")

        self._log_state(f"收到指令: [{player}] {message}")
        self.state = AgentState.LISTENING

        # 异步处理 (不阻塞事件循环)
        self._current_task = asyncio.create_task(
            self._process_user_input(event),
            name=f"agent-{self.name}-process",
        )

    async def _handle_chat(self, event: Event):
        """处理聊天事件 — IDLE 状态下被动监听"""
        if not self.is_idle:
            return

        player = event.data.get("player", "")
        # 过滤自己的消息 (防止 MOCK 模式下的反馈回路)
        if player == self.name or player == self.bridge.agent_name:
            return

        message = event.data.get("message", "")

        # 检测是否需要响应 (提到 Agent 名字等)
        if self._should_respond_to_chat(player, message):
            self._log_state(f"聊天触发响应: [{player}] {message[:50]}")
            await self.event_bus.publish(Event(
                type=EventType.USER_INPUT,
                source="agent.chat_trigger",
                target=self.name,
                data={
                    "message": f"[聊天中 {player} 说]: {message}\n你想回复吗？如果不需要回复，忽略即可。",
                    "player": player,
                },
            ))

    async def _handle_interrupt(self, event: Event):
        """处理中断 — 安全停止当前操作"""
        reason = event.data.get("reason", "未知原因")
        self._log_state(f"收到中断信号: {reason}")

        await self._interrupt_current_task(reason)
        self.state = AgentState.IDLE

        if self.stream_callback:
            await self.stream_callback("state", f"⏹ 已停止 ({reason})")

    async def _handle_timer(self, event: Event):
        """处理定时器 — 环境扫描 + 主动行为检测"""
        # 更新世界状态
        try:
            world = await self.bridge.get_world_state()
            if world:
                self.memory.update_world_state(world)
        except Exception as e:
            logger.debug(f"世界状态更新失败: {e}")
            return

        # Phase 5: 情绪自然衰减
        if self.emotion_engine:
            self.emotion_engine.update()
            # 低生命值 → 担忧
            ws = self.memory.world_state
            if ws and ws.health < 10:
                self.emotion_engine.on_danger_detected(severity=0.6)

        # 仅在 IDLE 状态检测主动行为
        if not self.is_idle:
            return

        # Phase 5: 主动对话检查 (长时间空闲)
        if self.interaction and self.interaction.config.proactive_chat:
            idle_sec = time.monotonic() - self._last_active_at
            if self.emotion_engine:
                self.emotion_engine.on_long_idle()
            ws = self.memory.world_state
            nearby = len(ws.nearby_entities) if ws else 0
            msg = self.interaction.check_proactive(
                idle_seconds=idle_sec,
                world_time=ws.time_of_day if ws else "day",
                nearby_players=nearby,
            )
            if msg:
                await self.bridge.send_chat(msg)

        # 主动行为检测
        action = self._detect_proactive_action()
        if action:
            self._log_state(f"主动行为触发: {action}")
            await self.event_bus.publish(Event(
                type=EventType.USER_INPUT,
                source="timer.proactive",
                target=self.name,
                data={"message": action},
            ))

    # ── 核心处理循环 ────────────────────────────────────────────────

    async def _process_user_input(self, event: Event):
        """
        完整的 THINKING → ACTING 循环

        使用 Phase 3 的 ToolCallLoop 模式:
        1. 发送消息 + 工具定义 → LLM
        2. LLM 返回 text → 结束 (→ REFLECTING)
        3. LLM 返回 tool_calls → 执行 → 回到 1
        """
        user_message = event.data.get("message", "")
        player = event.data.get("player", "")

        # Phase 4: 预规划 (LLM 推理任务步骤)
        task_plan = None
        if self.planner and self.planner.planning_enabled:
            try:
                task_plan = await self.planner.plan(user_message)
                if task_plan and task_plan.steps:
                    self._log_state(f"计划: {task_plan.steps[0][:60]}...")
            except Exception as e:
                logger.debug(f"规划跳过: {e}")

        # Phase 4: 注入世界知识 + 计划到系统提示词
        world_context = ""
        if self.world_config and self.world_config.is_loaded:
            world_context = self.world_config.to_system_prompt()
        if self.long_term_memory and self.long_term_memory.is_loaded:
            mem_context = self.long_term_memory.to_system_prompt_context()
            if mem_context:
                world_context += "\n\n" + mem_context

        messages = self.memory.build_messages(
            user_message + ("\n\n" + task_plan.to_text() if task_plan else "")
        )

        # 注入世界知识 + 情绪状态到系统提示词
        extra_context = world_context
        if self.emotion_engine:
            emotion_fragment = self.emotion_engine.to_prompt_fragment()
            extra_context = emotion_fragment + "\n\n" + extra_context
        if self.interaction:
            mode = self.interaction.choose_response_mode(
                is_command=user_message.strip().startswith(("/", "@", "!")),
                emotion_level=self.emotion_engine.mood_intensity if self.emotion_engine else 0.0,
            )
            extra_context += f"\n回复风格: {self.interaction.get_response_instruction(mode)}"

        if extra_context:
            sys_msg = messages[0]
            if isinstance(sys_msg, SystemMessage):
                messages[0] = SystemMessage(
                    content=sys_msg.content + "\n\n" + extra_context
                )

        self.stats.tasks_started += 1
        tool_steps = []

        try:
            for step in range(self.max_tool_steps):
                # 中断检查
                if self._interrupt_flag.is_set():
                    self._log_state(f"在第 {step + 1} 步被中断")
                    return

                # ── THINKING ──────────────────────────────
                self.state = AgentState.THINKING
                self._last_active_at = time.monotonic()

                result = await self.llm.chat_with_tools(
                    messages=messages,
                    tools=self.tools.get_openai_tools(),
                    temperature=0.0,
                )

                self.stats.llm_calls += 1
                self.stats.total_prompt_tokens += result.usage.prompt_tokens
                self.stats.total_completion_tokens += result.usage.completion_tokens

                # 流式回调: 思考过程
                if self.stream_callback and result.reasoning:
                    await self.stream_callback("reasoning", result.reasoning)

                # ── 文本回复 → REFLECTING → IDLE ──────
                if result.has_content and not result.has_tool_calls:
                    reply = result.content or ""

                    # 流式回调: 回复内容
                    if self.stream_callback:
                        await self.stream_callback("content", reply)

                    # 记录回复
                    self.memory.add_assistant_message(
                        content=reply,
                        reasoning=result.reasoning,
                    )

                    # REFLECTING
                    self.state = AgentState.REFLECTING
                    await self._reflect_and_respond(event, reply, tool_steps)

                    # Phase 5: 情绪触发
                    if self.emotion_engine:
                        self.emotion_engine.on_task_success(difficulty=len(tool_steps) / 10)

                    # Phase 5: 交互格式化
                    if self.interaction:
                        self.interaction.record_task(user_message, True, len(tool_steps))
                        reply = self.interaction.format_response(reply, recipient=player)

                    # Phase 4: 记录任务完成事件
                    if self.long_term_memory:
                        await self.long_term_memory.record_event(
                            f"完成任务: {user_message[:100]} → {reply[:100]}",
                            tags=["task", "completed"],
                            importance=3,
                        )

                    self.state = AgentState.IDLE
                    self.stats.tasks_completed += 1
                    self._log_state(f"任务完成 ({step + 1} 步)")
                    return

                # ── 工具调用 → ACTING ──────────────────
                if result.has_tool_calls:
                    # 记录 assistant 消息 (本地列表 + 持久记忆)
                    messages.append(AssistantMessage(
                        content=result.content,
                        tool_calls=result.tool_calls,
                    ))
                    self.memory.add_assistant_message(
                        content=result.content,
                        tool_calls=result.tool_calls,
                    )

                    for tc in result.tool_calls:
                        # 中断检查
                        if self._interrupt_flag.is_set():
                            self._log_state(f"工具执行前被中断")
                            return

                        self.state = AgentState.ACTING

                        # 流式回调: 工具调用
                        if self.stream_callback:
                            await self.stream_callback(
                                "tool_call",
                                f"{tc.name}({json.dumps(tc.arguments, ensure_ascii=False)})"
                            )

                        # 执行工具
                        bridge_result = await self.bridge.execute(tc.name, tc.arguments)
                        tool_steps.append({
                            "tool": tc.name,
                            "args": tc.arguments,
                            "result": bridge_result.to_dict(),
                        })

                        # 流式回调: 工具结果
                        if self.stream_callback:
                            await self.stream_callback(
                                "tool_result",
                                bridge_result.message[:200]
                            )

                        # 记录工具结果 (本地列表 + 持久记忆)
                        result_dict = bridge_result.to_dict()
                        messages.append(ToolMessage(
                            tool_call_id=tc.id,
                            name=tc.name,
                            content=json.dumps(result_dict, ensure_ascii=False),
                        ))
                        self.memory.add_tool_result(tc.id, tc.name, result_dict)

                        # Phase 4: 重要工具调用记录到长期记忆
                        if self.long_term_memory and bridge_result.status:
                            if tc.name in ("moveTo", "mineBlock", "placeBlock", "craftItem"):
                                await self.long_term_memory.record_event(
                                    f"执行 {tc.name}: {bridge_result.message[:100]}",
                                    tags=["action", tc.name],
                                    importance=2,
                                )

                    self.stats.tool_calls += len(result.tool_calls)
                    continue  # 回到 THINKING

                # ── 既无内容也无工具调用 ──
                self._log_state(f"LLM 返回空响应 (step {step + 1})")
                break

            # max_steps 耗尽
            timeout_msg = f"任务在 {self.max_tool_steps} 步内未完成。"
            self._log_state(timeout_msg)
            await self._respond(event, timeout_msg, success=False)
            self.state = AgentState.IDLE

        except asyncio.CancelledError:
            self._log_state("任务被取消")
            self.state = AgentState.IDLE
            raise
        except Exception as e:
            logger.exception(f"Agent {self.name} 处理异常: {e}")
            # Phase 5: 情绪触发
            if self.emotion_engine:
                self.emotion_engine.on_task_failure()
            await self._respond(event, f"处理出错: {e}", success=False)
            self.state = AgentState.IDLE
            self.stats.tasks_failed += 1

    # ── 反思与回复 ──────────────────────────────────────────────────

    async def _reflect_and_respond(
        self,
        event: Event,
        reply: str,
        tool_steps: list[dict],
    ):
        """任务完成后的反思评估"""
        # 简要反思: 任务是否真的完成？
        # Phase 4 将在此接入完整的预规划系统
        success = True
        for step in tool_steps:
            if not step["result"].get("status", False):
                success = False
                break

        await self._respond(event, reply, success=success)

    async def _respond(self, event: Event, message: str, success: bool = True):
        """回复用户"""
        # 发送到 Minecraft 聊天
        prefix = "" if success else "❌ "
        await self.bridge.send_chat(f"[{self.name}] {prefix}{message}")

        # 如果有 request id，响应回 EventBus
        if event.requires_response:
            await self.event_bus.respond_to(event, {
                "reply": message,
                "success": success,
            })

    # ── 中断处理 ────────────────────────────────────────────────────

    async def _interrupt_current_task(self, reason: str):
        """中断当前正在执行的任务"""
        if self._current_task and not self._current_task.done():
            self._interrupt_flag.set()
            self._current_task.cancel()
            try:
                await self._current_task
            except asyncio.CancelledError:
                pass
            self._current_task = None

        self._interrupt_flag.clear()
        self._log_state(f"已中断: {reason}")

    # ── 主动行为检测 ────────────────────────────────────────────────

    def _detect_proactive_action(self) -> Optional[str]:
        """
        检测是否需要主动行为

        在 TIMER 事件中调用。基于世界状态判断是否需要主动帮助。
        Phase 5 将扩展情绪/上下文感知。
        """
        ws = self.memory.world_state
        if ws is None:
            return None

        # 低生命值 → 提醒
        if ws.health < 10:
            return "玩家的生命值很低！检查是否有食物或药水可以提供，或者提醒玩家注意安全。"

        # 低饱食度 → 提供食物
        if ws.food < 6:
            return "玩家的饱食度很低！询问玩家是否需要食物，或者主动寻找食物。"

        # 夜间 → 提醒睡觉
        if ws.time_of_day in ("night", "midnight"):
            return "现在是夜晚，怪物很多。询问玩家是否需要你守卫，或者提醒玩家睡觉。"

        return None

    # ── 聊天检测 ────────────────────────────────────────────────────

    def _should_respond_to_chat(self, player: str, message: str) -> bool:
        """判断是否应该响应聊天消息"""
        # 响应直接提到 Agent 名字的消息
        if self.name.lower() in message.lower():
            return True
        # 响应求助类关键词
        help_keywords = ["帮忙", "帮帮我", "help", "救", "救命", "怎么做"]
        if any(kw in message.lower() for kw in help_keywords):
            return True
        return False

    # ── 辅助方法 ────────────────────────────────────────────────────

    def _log_state(self, msg: str):
        """记录状态变更"""
        logger.info(f"[{self.name}][{self.state.name}] {msg}")

    async def say(self, message: str):
        """主动发送聊天消息"""
        await self.bridge.send_chat(f"[{self.name}] {message}")

    def get_status(self) -> dict:
        """获取 Agent 状态摘要 (用于 Web 仪表盘)"""
        return {
            "name": self.name,
            "state": self.state.name,
            "is_processing": self.is_processing,
            "last_active_at": self._last_active_at,
            "stats": self.stats.to_dict(),
            "memory": {
                "message_count": self.memory.message_count,
                "world_state": self.memory.world_state.to_context_text()
                if self.memory.world_state else "未知",
            },
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Agent 统计
# ═══════════════════════════════════════════════════════════════════════════════

class AgentStats:
    """Agent 运行统计"""

    def __init__(self):
        self.tasks_started: int = 0
        self.tasks_completed: int = 0
        self.tasks_failed: int = 0
        self.llm_calls: int = 0
        self.tool_calls: int = 0
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0

    def to_dict(self) -> dict:
        return {
            "tasks_started": self.tasks_started,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
        }
