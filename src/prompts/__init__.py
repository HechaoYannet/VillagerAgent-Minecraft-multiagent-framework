"""
提示词模块 — Phase 3+ 中文提示词系统
"""

from src.prompts.system_prompts import (  # noqa: F401
    AGENT_SYSTEM_PROMPT,
    AGENT_USER_PROMPT,
    AGENT_COOPERATION_PROMPT,
    REFLECT_SYSTEM_PROMPT,
    REFLECT_USER_PROMPT,
    MINECRAFT_KNOWLEDGE_CARD_ZH,
    IDLE_SYSTEM_PROMPT,
    IDLE_USER_PROMPT,
    CONTROLLER_SYSTEM_PROMPT_ZH,
    build_personality_text,
    # 兼容旧版引用
    reflect_system_prompt,
    reflect_user_prompt,
    minecraft_knowledge_card,
    agent_prompt_w_emoji,
    agent_prompt_wo_emoji,
    agent_cooper_prompt,
    idle_prompt_w_emoji,
    idle_prompt_wo_emoji,
    task_prompt,
    state_prompt,
    one_step_reflect_prompt,
)
