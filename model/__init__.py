"""
LLM 模型模块 — 向后兼容

Phase 3 已将核心实现迁移到 src/llm/。
本模块保留为向后兼容层，所有导入重新导出自 src/llm/。

新代码应使用:
    from src.llm import OpenAICompatClient, ChatResult, ToolCall, ...
    from src.llm import init_language_model

旧代码继续可用:
    from model import OpenAILanguageModel
    from model.init_model import init_language_model
"""

# ── 新接口 (Phase 3) — 从 src.llm 重导出 ──
from src.llm.base import (  # noqa: F401
    AsyncChatModel,
    ChatResult,
    ChatChunk,
    ToolCall,
    TokenUsage,
    ToolDefinition,
    ToolParameter,
    ModelCapabilities,
    SystemMessage,
    UserMessage,
    AssistantMessage,
    ToolMessage,
    messages_to_openai,
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

# ── 旧接口 (兼容) ──
from model.openai_models import OpenAILanguageModel  # noqa: F401
from model.init_model import init_language_model, init_model_from_config  # noqa: F401

# Google Gemini (可选依赖: google-generativeai)
try:
    from model.google_model import GoogleLanguageModel  # noqa: F401
except ImportError:
    pass
