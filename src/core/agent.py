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
        max_tool_steps: int = 8,
        stream_callback: Optional[StreamCallback] = None,
        log_dir: str = "logs",
        # Phase 4: 持久化记忆与规划
        world_config = None,      # WorldConfig
        long_term_memory = None,  # LongTermMemory
        planner = None,           # TaskPlanner
        # Phase 5: 情绪与交互
        emotion_engine = None,    # EmotionEngine
        interaction_manager = None,  # InteractionManager
        # Token 优化
        max_history: int = 24,
        llm_max_tokens: int = 1024,
        proactive_llm: bool = False,
        proactive_cooldown: float = 300.0,
    ):
        self.name = name
        self.event_bus = event_bus
        self.llm = llm
        self.tools = tools
        self.bridge = bridge
        self.max_tool_steps = max_tool_steps
        self.stream_callback = stream_callback

        # Token 优化
        self.llm_max_tokens = llm_max_tokens
        self.proactive_llm = proactive_llm
        self.proactive_cooldown = proactive_cooldown
        self._proactive_last_fired: dict[str, float] = {}

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
            max_history=max_history,
            agent_name=name,
        )

        # 日志
        self.log_dir = log_dir
        self.structured_log: Any = None  # Phase 7: injected by controller

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
        # LLM 禁用时直接忽略所有用户指令 (不调用 LLM)
        if not self.bridge.llm_enabled:
            logger.info(f"[{self.name}] LLM 已禁用，忽略用户指令")
            return

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
        # 更新世界状态 (即使在 LLM 禁用时也更新)
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

        # LLM 禁用时跳过所有主动行为 (会触发 LLM 调用)
        if not self.bridge.llm_enabled:
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

        # 主动行为检测 (规则化, 默认不走 LLM 以节省 token)
        detected = self._detect_proactive_action()
        if detected:
            key, msg = detected
            now = time.monotonic()
            last = self._proactive_last_fired.get(key, 0.0)
            if now - last >= self.proactive_cooldown:
                self._proactive_last_fired[key] = now
                if self.proactive_llm:
                    # 保留旧行为: 发布 USER_INPUT 走完整 LLM 决策循环
                    self._log_state(f"主动行为触发(LLM): {msg}")
                    await self.event_bus.publish(Event(
                        type=EventType.USER_INPUT,
                        source="timer.proactive",
                        target=self.name,
                        data={"message": msg},
                    ))
                else:
                    # 规则模板直接回复, 零 LLM 调用
                    self._log_state(f"主动行为触发(模板): {msg}")
                    await self.bridge.send_chat(f"[{self.name}] {msg}")

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

        # ── Trace: 任务开始 ──
        _task_start_time = time.monotonic()
        _step_start_time = _task_start_time
        _task_llm_calls_before = self.stats.llm_calls
        _task_tool_calls_before = self.stats.tool_calls
        logger.info(f"[{self.name}] 📩 收到: [{player or '玩家'}] {user_message[:100]}")

        # Phase 4: 预规划 (LLM 推理任务步骤)
        # 闲聊 / 一步命令跳过规划，节省 LLM 调用
        _skip_plan = self._should_skip_planning(user_message)
        task_plan = None
        if not _skip_plan and self.planner and self.planner.planning_enabled:
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

        # 注入世界知识 + 情绪状态 (作为独立上下文消息, 保持 system 前缀稳定以命中缓存)
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
            # 插到当前用户消息之前 (历史之后), 不污染静态 system 前缀
            messages.insert(
                len(messages) - 1,
                UserMessage(content=f"[上下文]\n{extra_context}"),
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
                    max_tokens=self.llm_max_tokens,
                )

                self.stats.llm_calls += 1
                self.stats.total_prompt_tokens += result.usage.prompt_tokens
                self.stats.total_completion_tokens += result.usage.completion_tokens

                # Phase 7: 结构化日志
                if self.structured_log:
                    await self.structured_log.llm_request(
                        model=getattr(self.llm, '_model', 'unknown'),
                        prompt_tokens=result.usage.prompt_tokens,
                        completion_tokens=result.usage.completion_tokens,
                        has_tool_calls=result.has_tool_calls,
                        has_reasoning=bool(getattr(result, 'reasoning', None)),
                    )

                # ── Trace: LLM 响应 ──
                _llm_duration = time.monotonic() - _step_start_time
                _step_start_time = time.monotonic()  # reset for tool execution

                # reasoning
                if result.reasoning:
                    _reasoning_preview = str(result.reasoning)[:150].replace('\n', ' ')
                    logger.info(f"[{self.name}] 💭 推理: {_reasoning_preview}")
                    if self.structured_log:
                        await self.structured_log.agent_thought(str(result.reasoning), step + 1)

                # tool calls planned
                if result.has_tool_calls:
                    _tool_names = [tc.name for tc in (result.tool_calls or [])]
                    logger.info(
                        f"[{self.name}] 🧠 step {step + 1}/{self.max_tool_steps} "
                        f"(in:{result.usage.prompt_tokens} out:{result.usage.completion_tokens} "
                        f"⏱{_llm_duration:.1f}s) → 工具: {', '.join(_tool_names)}"
                    )
                else:
                    logger.info(
                        f"[{self.name}] 🧠 step {step + 1}/{self.max_tool_steps} "
                        f"(in:{result.usage.prompt_tokens} out:{result.usage.completion_tokens} "
                        f"⏱{_llm_duration:.1f}s)"
                    )

                # 流式回调: 思考过程
                if self.stream_callback and result.reasoning:
                    await self.stream_callback("reasoning", result.reasoning)

                # ── 文本回复 → REFLECTING → IDLE ──────
                if result.has_content and not result.has_tool_calls:
                    reply = result.content or ""

                    # ── Trace: 回复 ──
                    _reply_preview = reply[:200].replace('\n', ' ')
                    logger.info(f"[{self.name}] 💬 回复: {_reply_preview}")

                    # 流式回调: 回复内容
                    if self.stream_callback:
                        await self.stream_callback("content", reply)

                    # 记录回复
                    self.memory.add_assistant_message(
                        content=reply,
                        reasoning=result.reasoning,
                    )

                    # Phase 7: 聊天日志
                    if self.structured_log:
                        await self.structured_log.agent_chat(
                            player=player or "玩家",
                            message=reply,
                            direction="outgoing",
                        )

                    # Phase 5: 记录任务 (在回复之前)
                    if self.interaction:
                        self.interaction.record_task(user_message, True, len(tool_steps))

                    # REFLECTING → 回复 (含 player 名字)
                    self.state = AgentState.REFLECTING
                    await self._reflect_and_respond(event, reply, tool_steps, player=player)

                    # Phase 5: 情绪触发
                    if self.emotion_engine:
                        self.emotion_engine.on_task_success(difficulty=len(tool_steps) / 10)

                    # Phase 4: 记录任务完成事件
                    if self.long_term_memory:
                        await self.long_term_memory.record_event(
                            f"完成任务: {user_message[:100]} → {reply[:100]}",
                            tags=["task", "completed"],
                            importance=3,
                        )

                    self.state = AgentState.IDLE
                    self.stats.tasks_completed += 1
                    # ── Trace: 任务完成 ──
                    _total_time = time.monotonic() - _task_start_time
                    _llm_count = self.stats.llm_calls - _task_llm_calls_before
                    _tool_count = self.stats.tool_calls - _task_tool_calls_before
                    logger.info(
                        f"[{self.name}] ✅ 完成 ({step + 1}步, "
                        f"LLM×{_llm_count} 工具×{_tool_count}, "
                        f"in:{self.stats.total_prompt_tokens} out:{self.stats.total_completion_tokens}, "
                        f"⏱{_total_time:.1f}s)"
                    )
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

                        # ── Trace: 工具调用 ──
                        _args_preview = json.dumps(tc.arguments, ensure_ascii=False)[:100]
                        logger.info(f"[{self.name}] 🔧 {tc.name}({_args_preview})")

                        # 执行工具 (try/except 确保 ToolMessage 始终被添加)
                        _tool_start = time.monotonic()
                        try:
                            bridge_result = await self.bridge.execute(tc.name, tc.arguments)
                        except Exception as _tool_err:
                            _tool_duration = time.monotonic() - _tool_start
                            logger.warning(f"[{self.name}] ✗ 工具异常: {tc.name} → {_tool_err}")
                            # 构造错误结果 → API 消息序列不会残缺
                            error_dict = {
                                "status": False,
                                "message": f"Tool execution error: {_tool_err}",
                                "error": str(_tool_err),
                            }
                            tool_steps.append({
                                "tool": tc.name,
                                "args": tc.arguments,
                                "result": error_dict,
                            })
                            messages.append(ToolMessage(
                                tool_call_id=tc.id,
                                name=tc.name,
                                content=ConversationMemory.truncate_tool_result(error_dict),
                            ))
                            self.memory.add_tool_result(tc.id, tc.name, error_dict)
                            continue

                        _tool_duration = time.monotonic() - _tool_start

                        tool_steps.append({
                            "tool": tc.name,
                            "args": tc.arguments,
                            "result": bridge_result.to_dict(),
                        })

                        # ── Trace: 工具结果 ──
                        _status_icon = "✓" if bridge_result.status else "✗"
                        _result_raw = str(bridge_result.message) if not isinstance(bridge_result.message, str) else bridge_result.message
                        _result_preview = _result_raw[:150].replace('\n', ' ')
                        logger.info(
                            f"[{self.name}] {_status_icon} 结果: {_result_preview} "
                            f"({_tool_duration:.1f}s)"
                        )

                        # 结构化日志: agent_action
                        if self.structured_log:
                            await self.structured_log.agent_action(
                                tool_name=tc.name,
                                args=tc.arguments,
                                result=bridge_result.to_dict(),
                                duration_ms=int(_tool_duration * 1000),
                                success=bridge_result.status,
                            )

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
                            content=ConversationMemory.truncate_tool_result(result_dict),
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
                logger.warning(f"[{self.name}] ⚠️ LLM 返回空响应 (step {step + 1})")
                break

            # max_steps 耗尽
            timeout_msg = f"任务在 {self.max_tool_steps} 步内未完成。"
            logger.warning(f"[{self.name}] ⏰ 超时: {timeout_msg}")
            await self._respond(event, timeout_msg, success=False, player=player)
            self.state = AgentState.IDLE

        except asyncio.CancelledError:
            logger.info(f"[{self.name}] 🛑 任务被取消")
            self.state = AgentState.IDLE
            raise
        except Exception as e:
            logger.error(f"[{self.name}] ❌ 异常 (step {step + 1}): {e}")
            # Phase 5: 情绪触发
            if self.emotion_engine:
                self.emotion_engine.on_task_failure()
            # 结构化日志: error
            if self.structured_log:
                await self.structured_log.agent_error(
                    error_type=type(e).__name__,
                    error_msg=str(e),
                    step=step + 1,
                )
            await self._respond(event, f"处理出错: {e}", success=False, player=player)
            self.state = AgentState.IDLE
            self.stats.tasks_failed += 1

    # ── 反思与回复 ──────────────────────────────────────────────────

    async def _reflect_and_respond(
        self,
        event: Event,
        reply: str,
        tool_steps: list[dict],
        player: str = "",
    ):
        """任务完成后的反思评估"""
        # 简要反思: 任务是否真的完成？
        # Phase 4 将在此接入完整的预规划系统
        success = True
        for step in tool_steps:
            if not step["result"].get("status", False):
                success = False
                break

        await self._respond(event, reply, success=success, player=player)

    async def _respond(self, event: Event, message: str, success: bool = True, player: str = ""):
        """回复用户"""
        # 发送到 Minecraft 聊天
        if not success:
            message = f"❌ {message}"

        # 使用 InteractionManager 格式化 (含玩家名), 否则用简单格式
        if self.interaction:
            formatted = self.interaction.format_response(message, recipient=player)
        else:
            formatted = f"[{self.name}] {message}"

        await self.bridge.send_chat(formatted)

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

    # ── 跳过规划检测 ───────────────────────────────────────────────

    def _should_skip_planning(self, message: str) -> bool:
        """
        判断是否可跳过预规划 LLM 调用

        两种情况跳过:
        1. 闲聊（打招呼、问感受、短消息无动作关键词）
        2. 明显的一步命令（指令本身已暗示唯一的工具）
        """
        msg = message.strip().lower()

        # 闲聊检测 (原 _is_casual_chat)
        action_keywords = [
            "挖", "砍", "去", "过来", "走", "找", "拿", "放", "做", "建",
            "收集", "攻击", "跑", "跳", "合成", "打开", "看", "给",
            "mine", "dig", "go", "come", "find", "craft", "place", "build",
            "attack", "get", "give", "move", "follow",
        ]
        is_short = len(msg) < 15
        has_action = any(kw in msg for kw in action_keywords)
        is_question = "?" in msg or "？" in msg
        if is_short and not has_action and not is_question:
            return True

        # 一步命令：无需规划，直接执行即可
        single_step_patterns = [
            "过来", "停下", "站住", "跟紧我", "跟着我",
            "打开箱子", "开箱", "看看周围", "看周围", "附近有什么",
            "报时", "几点了", "什么时间",
            "come", "come here", "follow me",
            "stop", "wait", "look around",
            "open chest",
        ]
        for p in single_step_patterns:
            if p in msg:
                return True

        return False

    # ── 主动行为检测 ────────────────────────────────────────────────

    def _detect_proactive_action(self) -> Optional[tuple[str, str]]:
        """
        检测是否需要主动行为

        在 TIMER 事件中调用。基于世界状态判断是否需要主动帮助。
        返回 (冷却键, 模板消息) 或 None。默认走规则模板 (零 LLM 调用)。
        """
        ws = self.memory.world_state
        if ws is None:
            return None

        # 低生命值 → 提醒
        if ws.health < 10:
            return ("low_health", "⚠️ 你的生命值很低！快吃点东西或注意安全！")

        # 低饱食度 → 提供食物
        if ws.food < 6:
            return ("low_food", "🍗 你的饱食度很低了，需要我帮你找点食物吗？")

        # 夜间 → 提醒睡觉
        if ws.time_of_day in ("night", "midnight"):
            return ("night", "🌙 天黑了，怪物要出来了。需要我守卫，还是先睡觉？")

        return None

    # ── 聊天检测 ────────────────────────────────────────────────────

    def _should_respond_to_chat(self, player: str, message: str) -> bool:
        """判断是否应该响应聊天消息 — 仅响应 @ai 开头的消息"""
        if not message or not message.strip():
            return False
        # 过滤自己的消息
        if player == self.name or player == self.bridge.agent_name:
            return False
        # 只有 @ai 开头才响应
        if message.strip().startswith("@ai"):
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
