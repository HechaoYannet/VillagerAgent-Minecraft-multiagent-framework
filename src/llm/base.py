"""
LLM 抽象基类 — Phase 3 核心接口定义

定义所有 LLM 客户端必须实现的统一接口，支持：
- 原生 OpenAI 工具调用 (function calling)
- DeepSeek v4 思考 token (reasoning_content)
- 流式响应 (streaming)
- Token 用量追踪

用法：
    from model.llm_base import ChatResult, ToolCall, TokenUsage, AsyncChatModel

设计原则：
    - 协议无关：OpenAI / DeepSeek / Qwen / Gemini 均通过同一接口访问
    - 异步优先：所有 I/O 方法均为 async，兼容 asyncio 事件循环
    - 数据不可变：ChatResult / ToolCall / TokenUsage 使用 frozen dataclass
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Optional

# ═══════════════════════════════════════════════════════════════════════════════
# 数据类型
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ToolParameter:
    """工具参数的 JSON Schema 定义"""
    name: str
    type: str  # "string", "number", "integer", "boolean", "object", "array"
    description: str
    required: bool = True
    enum: Optional[list[str]] = None
    items: Optional[dict] = None  # for array type
    properties: Optional[dict[str, "ToolParameter"]] = None  # for object type

    def to_openai_schema(self) -> dict:
        """转换为 OpenAI 兼容的 JSON Schema 参数定义"""
        schema: dict[str, Any] = {
            "type": self.type,
            "description": self.description,
        }
        if self.enum:
            schema["enum"] = self.enum
        if self.items:
            schema["items"] = self.items
        if self.properties:
            schema["properties"] = {
                name: prop.to_openai_schema()
                for name, prop in self.properties.items()
            }
        return schema


@dataclass(frozen=True)
class ToolDefinition:
    """工具定义 — OpenAI function calling 格式"""
    name: str
    description: str
    parameters: list[ToolParameter] = field(default_factory=list)
    strict: bool = False  # OpenAI strict mode (requires allOf/anyOf-free schema)

    @property
    def required_params(self) -> list[str]:
        return [p.name for p in self.parameters if p.required]

    def to_openai_schema(self) -> dict:
        """生成 OpenAI tool definition"""
        properties = {}
        for p in self.parameters:
            properties[p.name] = p.to_openai_schema()

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": self.required_params,
                },
            },
        }


@dataclass(frozen=True)
class ToolCall:
    """LLM 返回的工具调用"""
    id: str
    name: str
    arguments: dict[str, Any]

    @classmethod
    def from_openai(cls, tool_call: Any) -> "ToolCall":
        """从 OpenAI API 响应的 tool_call 对象构造"""
        try:
            args = json.loads(tool_call.function.arguments)
        except (json.JSONDecodeError, AttributeError):
            args = {}
        return cls(
            id=tool_call.id,
            name=tool_call.function.name,
            arguments=args,
        )


@dataclass(frozen=True)
class TokenUsage:
    """Token 用量统计"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0  # DeepSeek 思考 token

    @classmethod
    def from_openai(cls, usage: Any) -> "TokenUsage":
        """从 OpenAI API usage 对象构造"""
        if usage is None:
            return cls()
        details = getattr(usage, "completion_tokens_details", None)
        return cls(
            prompt_tokens=getattr(usage, "prompt_tokens", 0),
            completion_tokens=getattr(usage, "completion_tokens", 0),
            total_tokens=getattr(usage, "total_tokens", 0),
            reasoning_tokens=getattr(details, "reasoning_tokens", 0) if details else 0,
        )


@dataclass(frozen=True)
class ChatResult:
    """LLM 聊天响应"""
    content: Optional[str] = None
    reasoning: Optional[str] = None  # DeepSeek v4 reasoning_content
    tool_calls: Optional[list[ToolCall]] = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str = "stop"  # "stop", "tool_calls", "length", "content_filter"
    model: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return self.tool_calls is not None and len(self.tool_calls) > 0

    @property
    def has_content(self) -> bool:
        return self.content is not None and len(self.content.strip()) > 0


@dataclass(frozen=True)
class ChatChunk:
    """流式响应的单个 chunk"""
    delta_content: Optional[str] = None
    delta_reasoning: Optional[str] = None
    tool_call_chunk: Optional[dict] = None  # 增量 tool call 数据
    finish_reason: Optional[str] = None
    usage: Optional[TokenUsage] = None


# ═══════════════════════════════════════════════════════════════════════════════
# 消息类型
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SystemMessage:
    """系统提示词"""
    content: str


@dataclass(frozen=True)
class UserMessage:
    """用户消息"""
    content: str | list[dict]  # 纯文本或多模态内容块


@dataclass(frozen=True)
class AssistantMessage:
    """助手回复"""
    content: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None


@dataclass(frozen=True)
class ToolMessage:
    """工具执行结果"""
    tool_call_id: str
    name: str
    content: str  # JSON 字符串或纯文本结果


Message = SystemMessage | UserMessage | AssistantMessage | ToolMessage


def messages_to_openai(messages: list[Message]) -> list[dict]:
    """将消息列表转换为 OpenAI API 格式"""
    result = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            result.append({"role": "system", "content": msg.content})
        elif isinstance(msg, UserMessage):
            result.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AssistantMessage):
            entry: dict[str, Any] = {"role": "assistant"}
            if msg.content:
                entry["content"] = msg.content
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            result.append(entry)
        elif isinstance(msg, ToolMessage):
            result.append({
                "role": "tool",
                "tool_call_id": msg.tool_call_id,
                "content": msg.content,
            })
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 抽象基类
# ═══════════════════════════════════════════════════════════════════════════════

class AsyncChatModel(ABC):
    """
    LLM 抽象基类 — 所有模型实现必须继承此类

    设计为异步优先，兼容 asyncio 事件循环。
    子类可实现同步包装器（如果需要），但核心接口均为 async。

    用法:
        model = OpenAICompatClient(api_key="...", base_url="...", model="deepseek-v4")
        result = await model.chat([SystemMessage("你好")])
        print(result.content)

        # 带工具调用
        result = await model.chat_with_tools(
            messages=[UserMessage("帮我查天气")],
            tools=[weather_tool],
        )
    """

    @property
    @abstractmethod
    def model_name(self) -> str:
        """模型标识名称"""
        ...

    @property
    @abstractmethod
    def capabilities(self) -> "ModelCapabilities":
        """模型能力标记"""
        ...

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        tool_choice: Literal["auto", "none", "required"] = "auto",
    ) -> ChatResult:
        """
        发送聊天请求并获取完整响应

        Args:
            messages: 消息历史列表
            tools: 可用的工具定义列表 (None = 无工具)
            temperature: 采样温度
            max_tokens: 最大输出 token 数
            tool_choice: 工具调用策略

        Returns:
            ChatResult: 包含 content / reasoning / tool_calls / usage
        """
        ...

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[Message],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        tool_choice: Literal["auto", "none", "required"] = "auto",
    ) -> AsyncIterator[ChatChunk]:
        """
        流式聊天 — 逐步 yield ChatChunk

        用于实时 UI 反馈。DeepSeek 模型会在初期 chunk 中
        返回 reasoning_content。

        Yields:
            ChatChunk: delta_content / delta_reasoning / tool_call_chunk
        """
        ...

    async def chat_with_tools(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> ChatResult:
        """
        带工具调用的聊天 — 返回 ChatResult（可能包含 tool_calls）

        默认实现委托给 chat()，子类可以覆盖以优化。
        """
        return await self.chat(
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice="auto",
        )

    def count_tokens(self, text: str) -> int:
        """
        估算文本的 token 数量

        默认使用 4 字符 ≈ 1 token 的粗略估算。
        子类应覆盖以使用实际分词器。
        """
        return len(text) // 4

    async def close(self):
        """关闭底层连接（子类可覆盖）"""
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 模型能力标记
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ModelCapabilities:
    """模型能力标记 — 用于运行时判断模型支持哪些功能"""

    supports_tool_calling: bool = True
    """是否支持原生 function calling (OpenAI 格式)"""

    supports_reasoning: bool = False
    """是否返回 reasoning_content (DeepSeek v4, o1 等)"""

    supports_vision: bool = False
    """是否支持图片输入 (多模态)"""

    supports_streaming: bool = True
    """是否支持流式输出"""

    max_context_tokens: int = 128_000
    """最大上下文窗口 token 数"""

    max_output_tokens: int = 8_192
    """单次最大输出 token 数"""

    reasoning_in_stream_only: bool = False
    """reasoning_content 是否仅在 streaming 模式下返回 (DeepSeek 特性)"""


# ═══════════════════════════════════════════════════════════════════════════════
# 已知模型的能力预设
# ═══════════════════════════════════════════════════════════════════════════════

MODEL_CAPABILITIES_PRESETS: dict[str, ModelCapabilities] = {
    # DeepSeek V4 (2026-04 发布，2026-07-24 起 chat/reasoner 弃用)
    "deepseek-v4-flash": ModelCapabilities(
        supports_tool_calling=True,
        supports_reasoning=True,       # 支持 thinking 模式
        supports_streaming=True,
        max_context_tokens=1_000_000,  # 1M
        max_output_tokens=384_000,
        reasoning_in_stream_only=False,
    ),
    "deepseek-v4-pro": ModelCapabilities(
        supports_tool_calling=True,
        supports_reasoning=True,
        supports_streaming=True,
        max_context_tokens=1_000_000,
        max_output_tokens=384_000,
        reasoning_in_stream_only=False,
    ),
    # 旧版别名 (已弃用 2026-07-24，保留用于向后兼容)
    "deepseek-chat": ModelCapabilities(
        supports_tool_calling=True,
        supports_reasoning=False,
        max_context_tokens=128_000,
        max_output_tokens=8_192,
    ),
    "deepseek-reasoner": ModelCapabilities(
        supports_tool_calling=False,
        supports_reasoning=True,
        max_context_tokens=128_000,
        max_output_tokens=8_192,
    ),
    "deepseek-v4": ModelCapabilities(  # 泛用别名 → flash
        supports_tool_calling=True,
        supports_reasoning=True,
        supports_streaming=True,
        max_context_tokens=1_000_000,
        max_output_tokens=384_000,
        reasoning_in_stream_only=False,
    ),
    # OpenAI
    "gpt-4o": ModelCapabilities(
        supports_tool_calling=True,
        supports_reasoning=False,
        supports_vision=True,
        max_context_tokens=128_000,
        max_output_tokens=16_384,
    ),
    "gpt-4o-mini": ModelCapabilities(
        supports_tool_calling=True,
        supports_reasoning=False,
        supports_vision=True,
        max_context_tokens=128_000,
        max_output_tokens=16_384,
    ),
    "gpt-4.1": ModelCapabilities(
        supports_tool_calling=True,
        supports_reasoning=False,
        supports_vision=True,
        max_context_tokens=1_000_000,
        max_output_tokens=32_768,
    ),
    # Qwen
    "qwen-max": ModelCapabilities(
        supports_tool_calling=True,
        supports_reasoning=False,
        max_context_tokens=128_000,
        max_output_tokens=8_192,
    ),
    "qwen3": ModelCapabilities(
        supports_tool_calling=True,
        supports_reasoning=True,
        max_context_tokens=128_000,
        max_output_tokens=8_192,
    ),
    # Gemini (via OpenAI-compatible API)
    "gemini-pro": ModelCapabilities(
        supports_tool_calling=True,
        supports_reasoning=False,
        supports_vision=True,
        max_context_tokens=128_000,
        max_output_tokens=8_192,
    ),
    # Claude (via OpenAI-compatible API / Anthropic proxy)
    "claude": ModelCapabilities(
        supports_tool_calling=True,
        supports_reasoning=False,
        supports_vision=True,
        max_context_tokens=200_000,
        max_output_tokens=16_384,
    ),
    # Default / unknown
    "default": ModelCapabilities(
        supports_tool_calling=True,
        supports_reasoning=False,
        supports_streaming=True,
        max_context_tokens=128_000,
        max_output_tokens=8_192,
    ),
}


def detect_capabilities(model_name: str) -> ModelCapabilities:
    """从模型名称检测能力"""
    name_lower = model_name.lower()

    # 精确匹配
    for key, caps in MODEL_CAPABILITIES_PRESETS.items():
        if key.lower() == name_lower:
            return caps

    # 模糊匹配
    if "deepseek" in name_lower:
        # V4 系列 (2026-04+)
        if "v4" in name_lower:
            if "pro" in name_lower:
                return MODEL_CAPABILITIES_PRESETS["deepseek-v4-pro"]
            return MODEL_CAPABILITIES_PRESETS["deepseek-v4-flash"]
        # 旧版推理模型
        if "reasoner" in name_lower or "r1" in name_lower:
            return MODEL_CAPABILITIES_PRESETS["deepseek-reasoner"]
        return ModelCapabilities(
            supports_tool_calling=True,
            supports_reasoning=False,
            max_context_tokens=128_000,
            max_output_tokens=32_000,
            reasoning_in_stream_only=True,
        )
    if "gpt-4.1" in name_lower:
        return MODEL_CAPABILITIES_PRESETS["gpt-4.1"]
    if "gpt-4o" in name_lower:
        return MODEL_CAPABILITIES_PRESETS["gpt-4o"]
    if "gpt-4" in name_lower:
        return MODEL_CAPABILITIES_PRESETS["gpt-4o"]
    if "gpt-3" in name_lower:
        return ModelCapabilities(
            supports_tool_calling=True,
            max_context_tokens=16_384,
            max_output_tokens=4_096,
        )
    if "qwen" in name_lower:
        if "qwen3" in name_lower:
            return MODEL_CAPABILITIES_PRESETS["qwen3"]
        return MODEL_CAPABILITIES_PRESETS["qwen-max"]
    if "gemini" in name_lower:
        return MODEL_CAPABILITIES_PRESETS["gemini-pro"]
    if "claude" in name_lower:
        return MODEL_CAPABILITIES_PRESETS["claude"]
    if "glm" in name_lower:
        return ModelCapabilities(
            supports_tool_calling=True,
            max_context_tokens=128_000,
            max_output_tokens=4_096,
        )
    if "llama" in name_lower or "vllm" in name_lower:
        return ModelCapabilities(
            supports_tool_calling=True,
            supports_reasoning=False,
            max_context_tokens=32_768,
            max_output_tokens=4_096,
        )

    # Default
    return MODEL_CAPABILITIES_PRESETS["default"]
