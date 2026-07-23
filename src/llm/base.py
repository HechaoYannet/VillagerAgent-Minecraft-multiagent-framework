"""
LLM 抽象基类

Phase 3 将实现完整的 OpenAI-compatible 客户端，
支持 DeepSeek v4 thinking tokens、流式输出和原生工具调用。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ChatResult:
    """LLM 调用返回结果"""
    content: Optional[str] = None
    reasoning: Optional[str] = None  # DeepSeek v4 thinking tokens
    tool_calls: Optional[list] = None
    usage: Optional[dict] = None
    finish_reason: Optional[str] = None


@dataclass
class ToolDefinition:
    """工具定义"""
    name: str
    description: str
    parameters: dict  # JSON Schema


class BaseLLM(ABC):
    """LLM 抽象基类"""

    def __init__(self, model: str, api_key: str, api_base: str, **kwargs):
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.kwargs = kwargs

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        stream: bool = False,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> ChatResult:
        """发送聊天请求"""
        ...

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ):
        """流式聊天请求 (async generator)"""
        ...


class LLMFactory:
    """LLM 工厂 - 根据配置创建合适的 LLM 实例"""

    @staticmethod
    def create(config: dict) -> BaseLLM:
        provider = config.get("llm", {}).get("provider", "deepseek")
        model = config["llm"]["model"]
        api_key = config["llm"]["api_key"]
        api_base = config["llm"]["api_base"]

        # Phase 3 将实现具体 provider
        raise NotImplementedError(
            f"LLM provider '{provider}' not yet implemented. "
            f"This will be completed in Phase 3."
        )
