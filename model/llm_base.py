"""
向后兼容 shim — 重导出自 src.llm.base

Phase 3 已将实现迁移至 src/llm/base.py。
新代码请使用: from src.llm.base import ChatResult, ToolCall, ...
"""

from src.llm.base import *  # noqa: F401, F403
