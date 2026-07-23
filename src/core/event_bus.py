"""
异步事件总线

Phase 2 将实现完整的事件驱动架构，
支持 USER_INPUT、WORLD_CHANGE、CHAT、TIMER、INTERRUPT 等事件类型。
"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Coroutine


class EventType(Enum):
    """事件类型 (按优先级排序)"""
    INTERRUPT = auto()    # 最高优先级：停止当前动作
    USER_INPUT = auto()   # 用户指令
    CHAT = auto()         # 游戏内聊天
    WORLD_CHANGE = auto() # 世界状态变化
    TIMER = auto()        # 定时触发
    AGENT_STATE = auto()  # Agent 状态变化
    SYSTEM = auto()       # 系统事件


@dataclass
class Event:
    """事件"""
    type: EventType
    source: str
    data: dict = field(default_factory=dict)
    timestamp: float = 0.0


class EventBus:
    """
    异步事件总线

    Phase 2 将实现完整的优先级队列、事件过滤和订阅管理。
    """

    def __init__(self):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._handlers: dict[EventType, list[Callable]] = {}
        self._running = False

    async def publish(self, event: Event):
        """发布事件"""
        await self._queue.put((event.type.value, event))

    def subscribe(self, event_type: EventType, handler: Callable[[Event], Coroutine]):
        """订阅事件类型"""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    async def start(self):
        """启动事件循环"""
        self._running = True
        while self._running:
            try:
                _, event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                handlers = self._handlers.get(event.type, [])
                for handler in handlers:
                    await handler(event)
            except asyncio.TimeoutError:
                continue

    async def stop(self):
        """停止事件循环"""
        self._running = False
