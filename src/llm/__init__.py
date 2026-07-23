"""
LLM 抽象层 — Phase 3 完成

提供统一的 LLM 接口，支持:
- OpenAI 兼容协议 (DeepSeek / GPT / Qwen / vLLM)
- 原生工具调用 (function calling)
- DeepSeek v4 思考 token (reasoning_content)
- 流式响应
- 指数退避 + 断路器
"""

from src.llm.base import (  # noqa: F401
    # 数据类
    AsyncChatModel,
    ChatResult,
    ChatChunk,
    ToolCall,
    TokenUsage,
    ToolDefinition,
    ToolParameter,
    ModelCapabilities,
    # 消息类型
    Message,
    SystemMessage,
    UserMessage,
    AssistantMessage,
    ToolMessage,
    messages_to_openai,
    # 工具函数
    detect_capabilities,
    MODEL_CAPABILITIES_PRESETS,
)

from src.llm.openai_compat import OpenAICompatClient, ToolCallLoop  # noqa: F401
from src.llm.retry import (  # noqa: F401
    RetryConfig,
    CircuitBreaker,
    async_retry,
    sync_retry,
    retry_with_backoff,
    RetryExhausted,
    CircuitBreakerOpen,
)
from src.llm.factory import init_language_model, init_model_from_config  # noqa: F401
