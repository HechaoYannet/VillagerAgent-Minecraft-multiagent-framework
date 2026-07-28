"""
LLM 模型工厂 — Phase 3 重构

根据配置自动选择合适的 LLM 客户端。
支持 OpenAI 兼容协议、Google Gemini、Zhipu GLM 等。

重构要点:
- 统一使用 OpenAICompatClient 处理所有 OpenAI 兼容 API
- 自动检测模型能力 (工具调用 / 推理 / 视觉)
- 简洁的提供商检测逻辑
- 保留对旧版 Google/Zhipu 模型的后向兼容

用法:
    from model.init_model import init_language_model

    model = init_language_model({
        "api_model": "deepseek-chat",
        "api_key": "sk-...",
        "api_base": "https://api.deepseek.com/v1",
    })
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def init_language_model(args: dict, use_new_client: bool = True) -> Any:
    """
    初始化语言模型

    Args:
        args: 配置字典，包含:
            - api_model: 模型名称 (如 "deepseek-chat", "gpt-4o", "qwen-max")
            - api_key: API 密钥
            - api_base: API 基础 URL
            - api_key_list: 多 API Key 列表 (可选)
            - role_name: Agent 角色名 (可选)
            - evaluation_strategy: 评估策略 (可选)
            - enable_ReAct_prompting: 是否启用 ReAct 提示 (已废弃)
            - strategy: 策略名称 (可选)
        use_new_client: 是否使用新的 OpenAICompatClient (默认 True)

    Returns:
        模型实例 (OpenAICompatClient / GoogleLanguageModel)
    """
    api_model = args.get("api_model", "").lower()

    # ── Google Gemini ──
    if "gemini" in api_model:
        return _init_google(args)

    # ── OpenAI 兼容系列 (DeepSeek / GPT / Qwen / vLLM / local / 其他) ──
    # 所有 OpenAI 兼容 API 统一走 OpenAICompatClient
    return _init_openai_compat(args)


def _init_openai_compat(args: dict) -> Any:
    """初始化 OpenAI 兼容客户端"""
    import os
    api_model = (
        args.get("api_model")
        or os.environ.get("LLM_MODEL")
        or "deepseek-chat"
    )
    api_key = args.get("api_key", "")
    api_base = args.get("api_base", "")
    api_key_list = args.get("api_key_list", [])
    role_name = args.get("role_name", "")
    enable_thinking = args.get("enable_thinking", True)

    from src.llm.openai_compat import OpenAICompatClient

    # 如果没有指定 api_base，根据模型名称选择默认值
    if not api_base:
        api_base = _default_base_url(api_model)

    return OpenAICompatClient(
        api_key=api_key,
        base_url=api_base,
        model=api_model,
        api_key_list=api_key_list if api_key_list else None,
        role_name=role_name,
        max_retries=5,
        retry_base_delay=1.0,
        retry_max_delay=60.0,
        strip_reasoning=True,
        enable_thinking=enable_thinking,
    )


def _init_google(args: dict) -> Any:
    """初始化 Google Gemini 模型 (可选, 需要 pip install google-generativeai)"""
    raise NotImplementedError(
        "Google Gemini 支持已移除 (旧实现未移植到新 AsyncChatModel 接口)。"
        "请使用 OpenAI 兼容 API (deepseek / qwen / gpt 等)。"
    )


def _default_base_url(api_model: str) -> str:
    """根据模型名称推断默认 API 地址"""
    model_lower = api_model.lower()

    if "deepseek" in model_lower:
        return "https://api.deepseek.com/v1"
    if "qwen" in model_lower:
        return "https://dashscope.aliyuncs.com/compatible-mode/v1"
    if "glm" in model_lower or "zhipu" in model_lower:
        return "https://open.bigmodel.cn/api/paas/v4"
    if "claude" in model_lower:
        return "https://api.anthropic.com/v1"

    return "https://api.openai.com/v1"


# ═══════════════════════════════════════════════════════════════════════════════
# 便捷函数 — 从 YAML 配置初始化
# ═══════════════════════════════════════════════════════════════════════════════

def init_model_from_config(config: dict, role_name: str = "") -> Any:
    """
    从 YAML 配置字典初始化模型

    支持的 YAML 格式:
        llm:
          api_model: "deepseek-chat"
          api_key: "${DEEPSEEK_API_KEY}"
          api_base: "https://api.deepseek.com/v1"
          api_key_list: ["key1", "key2"]
          enable_thinking: false  # 禁用 LLM 思考模式

    Args:
        config: YAML 配置字典 (llm 节点)
        role_name: Agent 角色名

    Returns:
        模型实例
    """
    import os

    args = {}

    # 从 llm 子节点或根节点读取
    llm_config = config.get("llm", config)

    args["api_model"] = (
        llm_config.get("api_model")
        or llm_config.get("model")
        or os.environ.get("LLM_MODEL")
        or "deepseek-chat"
    )
    args["api_base"] = llm_config.get("api_base", llm_config.get("base_url", ""))

    # API Key: 支持 ${ENV_VAR} 引用, 依次回退 LLM_API_KEY / OPENAI_API_KEY
    api_key = llm_config.get("api_key", "")
    if api_key.startswith("${") and api_key.endswith("}"):
        env_var = api_key[2:-1]
        api_key = os.environ.get(env_var, "")
    if not api_key:
        api_key = os.environ.get("LLM_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    args["api_key"] = api_key

    args["api_key_list"] = llm_config.get("api_key_list", [])
    args["role_name"] = role_name

    # 思考模式控制
    args["enable_thinking"] = llm_config.get("enable_thinking", True)

    return init_language_model(args)
