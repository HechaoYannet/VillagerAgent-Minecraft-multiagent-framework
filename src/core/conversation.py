"""
对话记忆管理 — Phase 2 短期记忆系统

管理 Agent 与 LLM 之间的消息历史，包括:
- 循环缓冲区 (最近 N 条消息)
- 系统提示词注入
- 世界状态上下文
- 工具调用消息链
- 性格系统集成

这是短期记忆层 —— 长期记忆 (ChromaDB) 将在 Phase 4 接入。

用法:
    memory = ConversationMemory(
        system_prompt=AGENT_SYSTEM_PROMPT,
        personality=personality_data,
        max_history=100,
    )
    messages = memory.build_messages(user_message="挖矿")
    # → [SystemMessage, ..., UserMessage]
    memory.add_assistant_message("好的，让我去找钻石！")
    memory.add_tool_result(tool_call_id, tool_name, result)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from src.llm.base import (
    Message,
    SystemMessage,
    UserMessage,
    AssistantMessage,
    ToolMessage,
    ToolCall,
)
from src.prompts.system_prompts import (
    AGENT_SYSTEM_PROMPT,
    MINECRAFT_KNOWLEDGE_CARD_ZH,
    build_personality_text,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 世界状态快照
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class WorldStateSnapshot:
    """世界状态快照 — 从 Minecraft Bridge 获取"""
    position: Optional[list[float]] = None  # [x, y, z]
    health: float = 20.0
    food: float = 20.0
    time_of_day: str = "day"
    dimension: str = "overworld"
    nearby_entities: list[dict] = field(default_factory=list)
    inventory_summary: str = "库存为空"
    held_item: str = "空手"
    timestamp: float = field(default_factory=time.monotonic)

    def to_context_text(self) -> str:
        """转换为提示词上下文文本"""
        lines = [
            f"## 当前状态",
            f"- 位置: {self.position if self.position else '未知'}",
            f"- 生命值: {self.health}/20",
            f"- 食物值: {self.food}/20",
            f"- 时间: {self.time_of_day}",
            f"- 手持: {self.held_item}",
            f"- 库存: {self.inventory_summary}",
        ]
        if self.nearby_entities:
            entity_names = [e.get("name", "未知") for e in self.nearby_entities[:10]]
            lines.append(f"- 附近实体: {', '.join(entity_names)}")
        return "\n".join(lines)

    @classmethod
    def from_bridge_data(cls, data: dict) -> "WorldStateSnapshot":
        """从 Minecraft Bridge 返回的数据构造"""
        return cls(
            position=data.get("my_position"),
            health=data.get("health", 20.0),
            food=data.get("food", 20.0),
            time_of_day=data.get("timeOfDay", "day"),
            dimension=data.get("dimension", "overworld"),
            nearby_entities=data.get("nearby_entities", []),
            inventory_summary=data.get("inventory_summary", "库存为空"),
            held_item=data.get("held_item", "空手"),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 对话记忆
# ═══════════════════════════════════════════════════════════════════════════════

class ConversationMemory:
    """
    对话记忆 — 短期消息历史管理器

    维护 Agent 与 LLM 之间的消息列表，包括:
    - 系统提示词 (固定头部)
    - 历史消息 (循环缓冲区)
    - 当前轮次的工具调用链

    特性:
        - 自动管理 Assistant/Tool 消息配对
        - 世界状态每次更新 (TIMER 触发)
        - 超出 max_history 时智能截断 (保留系统提示词)
    """

    def __init__(
        self,
        system_prompt: str = AGENT_SYSTEM_PROMPT,
        personality: Optional[dict] = None,
        max_history: int = 100,
        agent_name: str = "伙伴",
    ):
        self._system_prompt = system_prompt
        self._personality = personality or {}
        self._max_history = max_history
        self._agent_name = agent_name

        # 固定部分 (不会被截断)
        self._system_message: str = ""  # 完整的系统提示词 (含性格+知识)
        self._rebuild_system()

        # 可变部分 (循环缓冲区)
        self._messages: list[Message] = []

        # 世界状态
        self._world_state: Optional[WorldStateSnapshot] = None
        self._world_state_message_index: int = -1

    # ── 系统提示词 ──────────────────────────────────────────────────

    def _rebuild_system(self):
        """重建系统提示词 (性格或 agent_name 变化时调用)"""
        personality_text = build_personality_text(self._personality)

        self._system_message = self._system_prompt.replace(
            "{{agent_name}}", self._agent_name
        ).replace(
            "{{personality}}", personality_text
        ).replace(
            "{{traits}}", self._personality.get("特征", self._personality.get("traits", "友好、乐于助人"))
        ).replace(
            "{{minecraft_knowledge}}", MINECRAFT_KNOWLEDGE_CARD_ZH
        ).replace(
            "{{tool_descriptions}}", ""  # 由 ToolRegistry 动态注入
        ).replace(
            "{{relevant_data}}", ""
        ).replace(
            "{{env}}", ""
        ).replace(
            "{{agent_state}}", ""
        ).replace(
            "{{other_agents}}", ""
        ).replace(
            "{{agent_action_list}}", "暂无操作记录"
        )

    def set_personality(self, personality: dict):
        """更新性格"""
        self._personality = personality
        self._rebuild_system()

    def set_agent_name(self, name: str):
        """更新 Agent 名称"""
        self._agent_name = name
        self._rebuild_system()

    # ── 世界状态 ────────────────────────────────────────────────────

    def update_world_state(self, data: dict):
        """更新世界状态快照 (TIMER 事件触发)"""
        self._world_state = WorldStateSnapshot.from_bridge_data(data)

    def update_tool_descriptions(self, tool_descriptions: str):
        """动态更新工具描述"""
        self._system_message = self._system_message.replace(
            "{{tool_descriptions}}", tool_descriptions
        )

    # ── 消息管理 ────────────────────────────────────────────────────

    def add_user_message(self, content: str, player: str = ""):
        """添加用户消息"""
        if player:
            prefix = f"[来自 {player}]: "
        else:
            prefix = ""
        self._messages.append(UserMessage(content=prefix + content))
        self._trim()

    def add_assistant_message(
        self,
        content: Optional[str] = None,
        tool_calls: Optional[list[ToolCall]] = None,
        reasoning: Optional[str] = None,
    ):
        """
        添加助手回复

        如果有关联的 reasoning (DeepSeek 思考链)，存入内部日志但不发送给 LLM。
        """
        self._messages.append(AssistantMessage(
            content=content,
            tool_calls=tool_calls,
        ))
        self._trim()

        # reasoning 仅用于日志，不加入消息历史 (节省 token)
        if reasoning:
            self._last_reasoning = reasoning

    def add_tool_result(self, tool_call_id: str, tool_name: str, result: dict):
        """添加工具执行结果"""
        result_str = json.dumps(result, ensure_ascii=False)
        self._messages.append(ToolMessage(
            tool_call_id=tool_call_id,
            name=tool_name,
            content=result_str,
        ))

    def _trim(self):
        """截断超出 max_history 的消息 (保留最近 N 条)"""
        if len(self._messages) > self._max_history:
            # 保留最近的消息 (从后面保留)
            excess = len(self._messages) - self._max_history
            self._messages = self._messages[excess:]
            logger = __import__("logging").getLogger(__name__)
            logger.debug(f"消息历史截断: 移除了 {excess} 条旧消息")

    # ── 构建 LLM 消息列表 ──────────────────────────────────────────

    def build_messages(self, user_message: Optional[str] = None) -> list[Message]:
        """
        构建完整的消息列表 (发送给 LLM)

        结构:
            [SystemMessage]
            [WorldStateSnapshot → UserMessage]  (如果有)
            [...历史消息...]
            [UserMessage]  (当前用户输入)

        Args:
            user_message: 当前用户消息 (None = 仅返回历史)
        """
        messages: list[Message] = [SystemMessage(content=self._system_message)]

        # 注入世界状态
        if self._world_state:
            context = self._world_state.to_context_text()
            messages.append(UserMessage(content=f"[世界状态]\n{context}"))

        # 历史消息
        messages.extend(self._messages)

        # 当前用户输入
        if user_message:
            messages.append(UserMessage(content=user_message))

        return messages

    # ── 查询 ────────────────────────────────────────────────────────

    @property
    def world_state(self) -> Optional[WorldStateSnapshot]:
        return self._world_state

    @property
    def last_reasoning(self) -> str:
        return getattr(self, "_last_reasoning", "")

    @property
    def message_count(self) -> int:
        return len(self._messages)

    def get_recent_messages(self, n: int = 10) -> list[Message]:
        """获取最近 N 条消息 (用于 UI 展示)"""
        return self._messages[-n:]

    def get_history_summary(self) -> str:
        """生成对话摘要 (用于跨轮次上下文)"""
        parts = []
        for msg in self._messages[-20:]:  # 最近 20 条
            if isinstance(msg, UserMessage):
                parts.append(f"[用户]: {str(msg.content)[:100]}")
            elif isinstance(msg, AssistantMessage):
                if msg.content:
                    parts.append(f"[Agent]: {msg.content[:100]}")
                if msg.tool_calls:
                    tools = ", ".join(tc.name for tc in msg.tool_calls)
                    parts.append(f"[工具调用]: {tools}")
        return "\n".join(parts)

    def clear_history(self):
        """清除历史 (保留系统提示词和世界状态)"""
        self._messages.clear()
