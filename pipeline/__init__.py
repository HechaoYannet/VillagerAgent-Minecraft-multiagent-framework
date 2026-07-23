"""
Agent 流水线模块 — 向后兼容

Phase 3 已将工具注册表和提示词迁移到 src/core/ 和 src/prompts/。
本模块保留为向后兼容层。

新代码应使用:
    from src.core import ToolRegistry, MINECRAFT_TOOL_DEFINITIONS
    from src.prompts import AGENT_SYSTEM_PROMPT, build_personality_text

旧代码继续可用:
    from pipeline.tool_registry import ToolRegistry
    from pipeline.prompts_zh import AGENT_SYSTEM_PROMPT
    from pipeline.agent import BaseAgent
    from pipeline.controller import GlobalController
"""
