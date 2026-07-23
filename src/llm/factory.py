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
        "api_model": "deepseek-v4",
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
            - api_model: 模型名称 (如 "deepseek-v4", "gpt-4o", "qwen-max")
            - api_key: API 密钥
            - api_base: API 基础 URL
            - api_key_list: 多 API Key 列表 (可选)
            - role_name: Agent 角色名 (可选)
            - evaluation_strategy: 评估策略 (可选)
            - enable_ReAct_prompting: 是否启用 ReAct 提示 (已废弃)
            - strategy: 策略名称 (可选)
        use_new_client: 是否使用新的 OpenAICompatClient (默认 True)

    Returns:
        模型实例 (OpenAICompatClient / OpenAILanguageModel / GoogleLanguageModel / ...)
    """
    api_model = args.get("api_model", "").lower()

    # ── OpenAI 兼容系列 (DeepSeek / GPT / Qwen / vLLM / local) ──
    if _is_openai_compatible(api_model):
        return _init_openai_compat(args, use_new_client)

    # ── Google Gemini ──
    elif "gemini" in api_model:
        return _init_google(args)

    # ── Zhipu GLM ──
    elif "glm" in api_model:
        return _init_zhipu(args)

    # ── 本地 HF 模型 (已废弃，保留兼容) ──
    else:
        logger.warning(f"未识别的模型 '{api_model}'，尝试使用 HuggingFace 模型")
        return _init_huggingface(args)


def _is_openai_compatible(api_model: str) -> bool:
    """判断模型是否为 OpenAI 兼容协议"""
    openai_keywords = [
        "gpt", "qwen", "deepseek", "default",
        "vllm", "llama", "nas",
        "claude",  # Anthropic proxy via OpenAI API
        "o1", "o3", "o4",  # OpenAI reasoning models
    ]
    return any(keyword in api_model for keyword in openai_keywords)


def _init_openai_compat(args: dict, use_new_client: bool = True) -> Any:
    """初始化 OpenAI 兼容客户端"""
    api_model = args.get("api_model", "qwen-max")
    api_key = args.get("api_key", "")
    api_base = args.get("api_base", "")
    api_key_list = args.get("api_key_list", [])
    role_name = args.get("role_name", "")

    if use_new_client:
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
        )
    else:
        # 回退到旧版 OpenAILanguageModel
        from model.openai_models import OpenAILanguageModel

        new_args = {
            "api_key": api_key,
            "api_model": api_model,
            "api_base": api_base or "https://api.openai.com/v1",
            "evaluation_strategy": args.get("evaluation_strategy", None),
            "enable_ReAct_prompting": args.get("enable_ReAct_prompting", None),
            "strategy": args.get("strategy", None),
            "role_name": role_name,
            "api_key_list": api_key_list if api_key_list else None,
        }
        new_args = {k: v for k, v in new_args.items() if v is not None}
        return OpenAILanguageModel(**new_args)


def _init_google(args: dict) -> Any:
    """初始化 Google Gemini 模型"""
    try:
        from src.llm.google_model import GoogleLanguageModel
    except ImportError:
        from model.google_model import GoogleLanguageModel

    new_args = {
        "api_key": args.get("api_key", None),
        "api_model": args.get("api_model", "gemini-pro"),
        "role_name": args.get("role_name", None),
        "api_key_list": args.get("api_key_list", None),
    }
    new_args = {k: v for k, v in new_args.items() if v is not None}
    return GoogleLanguageModel(**new_args)


def _init_zhipu(args: dict) -> Any:
    """初始化 Zhipu GLM 模型"""
    try:
        from model.zhipu_model import ZhipuLanguageModel
    except ImportError:
        raise ImportError(
            "Zhipu GLM 模型需要安装相关依赖。"
            "请确保 model/zhipu_model.py 存在。"
        )

    new_args = {
        "api_key": args.get("api_key", None),
        "api_model": args.get("api_model", "glm-4"),
        "role_name": args.get("role_name", None),
        "api_key_list": args.get("api_key_list", None),
    }
    new_args = {k: v for k, v in new_args.items() if v is not None}
    return ZhipuLanguageModel(**new_args)


def _init_huggingface(args: dict) -> Any:
    """初始化 HuggingFace 本地模型 (已废弃)"""
    try:
        from model.huggingface_model import HFLanguageModel
    except ImportError:
        raise ImportError(
            "HuggingFace 本地模型已废弃。"
            "请使用 OpenAI 兼容的 API 模型。"
        )

    new_args = {
        "api_key": args.get("api_key", None),
        "model_tokenizer": args.get("model_tokenizer", None),
        "verbose": args.get("verbose", None),
        "api_key_list": args.get("api_key_list", None),
    }
    new_args = {k: v for k, v in new_args.items() if v is not None}
    return HFLanguageModel(**new_args)


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
          api_model: "deepseek-v4"
          api_key: "${DEEPSEEK_API_KEY}"
          api_base: "https://api.deepseek.com/v1"
          api_key_list: ["key1", "key2"]

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

    args["api_model"] = llm_config.get("api_model", llm_config.get("model", ""))
    args["api_base"] = llm_config.get("api_base", llm_config.get("base_url", ""))

    # API Key 支持环境变量引用
    api_key = llm_config.get("api_key", "")
    if api_key.startswith("${") and api_key.endswith("}"):
        env_var = api_key[2:-1]
        api_key = os.environ.get(env_var, "")
    args["api_key"] = api_key

    args["api_key_list"] = llm_config.get("api_key_list", [])
    args["role_name"] = role_name

    return init_language_model(args)
