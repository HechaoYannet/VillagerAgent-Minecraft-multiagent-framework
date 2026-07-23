"""
异步事件总线 — Phase 2 核心基础设施

系统消息中枢：所有 Agent、Minecraft Bridge、Controller 之间的通信
通过事件总线解耦，支持发布-订阅和请求-响应两种模式。

特性:
- 优先级队列 (INTERRUPT > USER_INPUT > CHAT > WORLD_CHANGE > TIMER > AGENT_STATE)
- 类型安全的订阅管理
- 请求-响应模式 (publish + await future)
- 事件历史 (调试/回放)
- 事件过滤 (target 匹配)
- 优雅启停

用法:
    bus = EventBus()
    await bus.start()

    # 订阅
    bus.subscribe(EventType.USER_INPUT, my_handler, target="MyAgent")

    # 发布
    await bus.publish(Event(type=EventType.CHAT, source="minecraft", data={...}))

    # 请求-响应
    future = await bus.request(Event(type=EventType.USER_INPUT, ...))
    result = await future  # 等待响应

    await bus.stop()
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 事件类型与数据结构
# ═══════════════════════════════════════════════════════════════════════════════

class EventType(IntEnum):
    """事件类型 —— 按优先级排序 (值越小优先级越高)"""
    INTERRUPT = 0       # 最高: 紧急停止
    USER_INPUT = 1       # 用户指令
    CHAT = 2             # 游戏内聊天
    WORLD_CHANGE = 3     # 世界状态变化
    TIMER = 4            # 定时触发
    AGENT_STATE = 5      # Agent 状态变更通知
    SYSTEM = 6           # 系统事件 (启动/关闭)


@dataclass(frozen=True)
class Event:
    """
    事件 — 不可变数据结构

    Attributes:
        id: 唯一标识 (UUID)
        type: 事件类型 (决定优先级)
        source: 事件来源 ("minecraft.chat", "web.dashboard", "timer", ...)
        target: 目标 Agent 名称 (None = 广播到所有订阅者)
        data: 事件负载
        timestamp: 事件时间戳
        requires_response: 是否需要回复 (用于 request-response 模式)
    """
    type: EventType
    source: str = "unknown"
    target: Optional[str] = None
    data: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.monotonic)
    requires_response: bool = False

    def __lt__(self, other: "Event") -> bool:
        """优先级比较 (用于 PriorityQueue)"""
        return self.type.value < other.type.value


# ═══════════════════════════════════════════════════════════════════════════════
# 类型别名
# ═══════════════════════════════════════════════════════════════════════════════

EventHandler = Callable[[Event], Coroutine[Any, Any, None]]
"""事件处理函数签名: async def handler(event: Event) -> None"""

EventFilter = Callable[[Event], bool]
"""事件过滤器: def filter(event: Event) -> bool"""


# ═══════════════════════════════════════════════════════════════════════════════
# 订阅条目
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class _Subscription:
    """内部订阅条目"""
    handler: EventHandler
    target: Optional[str] = None  # None = 接收所有
    filter: Optional[EventFilter] = None
    once: bool = False  # 一次性订阅


# ═══════════════════════════════════════════════════════════════════════════════
# 事件总线
# ═══════════════════════════════════════════════════════════════════════════════

class EventBus:
    """
    异步事件总线 — 优先级调度 + 发布订阅 + 请求响应

    线程安全: 所有操作必须在同一个 asyncio 事件循环中执行。

    用法:
        bus = EventBus(history_size=500)
        await bus.start()

        # 订阅
        bus.subscribe(EventType.USER_INPUT, my_handler, target="MyAgent")

        # 发布 (fire-and-forget)
        await bus.publish(Event(type=EventType.CHAT, source="mc", data={"msg": "hi"}))

        # 请求-响应
        future = await bus.request(Event(type=EventType.USER_INPUT, target="Bot",
                                         data={"message": "挖矿"}, requires_response=True))
        result = await future  # 阻塞直到收到响应事件

        await bus.stop()
    """

    def __init__(self, history_size: int = 500):
        self._queue: asyncio.PriorityQueue[tuple[int, float, Event]] = asyncio.PriorityQueue()
        self._subscriptions: dict[EventType, list[_Subscription]] = {
            t: [] for t in EventType
        }
        self._history: deque[Event] = deque(maxlen=history_size)
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._running = False
        self._seq = 0  # 单调递增序号 (打破优先级平局)
        self._main_task: Optional[asyncio.Task] = None

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def start(self):
        """启动事件循环 (非阻塞 — 创建后台 Task)"""
        if self._running:
            logger.warning("EventBus 已在运行中")
            return

        self._running = True
        self._main_task = asyncio.create_task(self._event_loop(), name="event-bus")
        logger.info("EventBus 已启动")

    async def stop(self):
        """停止事件循环"""
        if not self._running:
            return

        self._running = False
        if self._main_task:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass

        # 取消所有未决请求
        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()

        logger.info("EventBus 已停止")

    async def _event_loop(self):
        """主事件循环 — 从优先级队列调度事件到订阅者"""
        while self._running:
            try:
                # 1秒超时，允许检查 _running 标志
                _, _, event = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            # 记录历史
            self._history.append(event)

            # 处理请求-响应
            if event.id in self._pending_requests:
                future = self._pending_requests.pop(event.id)
                if not future.done():
                    future.set_result(event)

            # 分发到订阅者
            subs = self._subscriptions.get(event.type, [])
            to_remove = []

            for sub in subs:
                try:
                    # 检查目标过滤
                    if sub.target is not None and sub.target != event.target:
                        continue
                    # 检查自定义过滤
                    if sub.filter is not None and not sub.filter(event):
                        continue

                    await sub.handler(event)

                    if sub.once:
                        to_remove.append(sub)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        f"事件处理器异常: type={event.type.name}, "
                        f"handler={sub.handler.__name__}"
                    )

            # 清理一次性订阅
            for sub in to_remove:
                try:
                    subs.remove(sub)
                except ValueError:
                    pass

    # ── 发布 ──────────────────────────────────────────────────────────

    async def publish(self, event: Event):
        """
        发布事件 (fire-and-forget)

        事件被加入优先级队列，按序分发给匹配的订阅者。
        """
        self._seq += 1
        await self._queue.put((event.type.value, self._seq, event))
        logger.debug(f"事件发布: {event.type.name} ← {event.source} (target={event.target})")

    async def request(self, event: Event, timeout: float = 30.0) -> "asyncio.Future[Event]":
        """
        发布事件并等待响应 (request-response 模式)

        返回一个 Future，当收到 id 匹配的响应事件时 resolve。
        超时则 raise TimeoutError。

        用法:
            future = await bus.request(Event(type=USER_INPUT, target="Bot",
                                             data={"msg": "挖矿"}, requires_response=True))
            try:
                response = await asyncio.wait_for(future, timeout=30.0)
                print(response.data["reply"])
            except asyncio.TimeoutError:
                print("Agent 超时未响应")
        """
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[event.id] = future
        await self.publish(event)
        return future

    async def respond_to(self, original_event: Event, response_data: dict):
        """发送响应事件 (匹配原始请求的 id)"""
        response = Event(
            type=original_event.type,
            source=original_event.target or "agent",
            target=original_event.source,
            data=response_data,
            id=original_event.id,  # 相同 id → resolver 匹配
        )
        await self.publish(response)

    # ── 订阅 ──────────────────────────────────────────────────────────

    def subscribe(
        self,
        event_type: EventType,
        handler: EventHandler,
        target: Optional[str] = None,
        filter: Optional[EventFilter] = None,
        once: bool = False,
    ):
        """
        订阅事件类型

        Args:
            event_type: 要订阅的事件类型
            handler: 异步处理函数 async def handler(event: Event) -> None
            target: 目标 Agent 名称过滤 (None = 接收所有)
            filter: 自定义过滤函数 (event) -> bool
            once: 是否只触发一次后自动取消订阅
        """
        sub = _Subscription(
            handler=handler,
            target=target,
            filter=filter,
            once=once,
        )
        self._subscriptions[event_type].append(sub)
        logger.debug(
            f"订阅: {event_type.name} ← {handler.__name__}"
            + (f" (target={target})" if target else "")
            + (" [once]" if once else "")
        )

    def unsubscribe(self, event_type: EventType, handler: EventHandler):
        """
        取消订阅
        """
        subs = self._subscriptions.get(event_type, [])
        before = len(subs)
        self._subscriptions[event_type] = [
            s for s in subs if s.handler is not handler
        ]
        removed = before - len(self._subscriptions[event_type])
        if removed > 0:
            logger.debug(f"取消订阅: {event_type.name} ← {handler.__name__}")

    def subscribe_once(
        self,
        event_type: EventType,
        handler: EventHandler,
        target: Optional[str] = None,
        filter: Optional[EventFilter] = None,
    ):
        """一次性订阅 — 触发后自动取消"""
        self.subscribe(event_type, handler, target=target, filter=filter, once=True)

    # ── 查询 ──────────────────────────────────────────────────────────

    def get_history(
        self,
        event_type: Optional[EventType] = None,
        limit: int = 50,
    ) -> list[Event]:
        """获取事件历史 (用于调试)"""
        events = list(self._history)
        if event_type is not None:
            events = [e for e in events if e.type == event_type]
        return events[-limit:]

    @property
    def subscriber_count(self) -> dict[str, int]:
        """各事件类型的订阅者数量 (用于调试)"""
        return {t.name: len(subs) for t, subs in self._subscriptions.items()}

    @property
    def is_running(self) -> bool:
        return self._running


# ═══════════════════════════════════════════════════════════════════════════════
# 便捷事件构造函数
# ═══════════════════════════════════════════════════════════════════════════════

def make_interrupt(target: str, reason: str = "") -> Event:
    """构造中断事件"""
    return Event(
        type=EventType.INTERRUPT,
        source="controller",
        target=target,
        data={"reason": reason},
    )


def make_user_input(
    message: str,
    target: str,
    source: str = "minecraft.chat",
    player: str = "",
) -> Event:
    """构造用户输入事件"""
    return Event(
        type=EventType.USER_INPUT,
        source=source,
        target=target,
        data={"message": message, "player": player},
        requires_response=True,
    )


def make_chat(player: str, message: str) -> Event:
    """构造聊天事件"""
    return Event(
        type=EventType.CHAT,
        source=f"minecraft.chat.{player}",
        data={"player": player, "message": message},
    )


def make_timer(interval: float, label: str = "") -> Event:
    """构造定时器事件"""
    return Event(
        type=EventType.TIMER,
        source="timer",
        data={"interval": interval, "label": label},
    )


def make_world_change(change_type: str, data: dict) -> Event:
    """构造世界变化事件"""
    return Event(
        type=EventType.WORLD_CHANGE,
        source=f"minecraft.world.{change_type}",
        data=data,
    )
