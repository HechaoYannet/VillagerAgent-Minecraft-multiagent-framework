"""
向后兼容 shim — 重导出自 src.llm.openai_compat

Phase 3 已将实现迁移至 src/llm/openai_compat.py。
新代码请使用: from src.llm.openai_compat import OpenAICompatClient, ToolCallLoop
"""

from src.llm.openai_compat import *  # noqa: F401, F403
