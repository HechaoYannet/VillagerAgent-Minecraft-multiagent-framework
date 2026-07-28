"""
通用 OpenAI-Compatible 客户端 — Phase 3 核心实现

替代原有 OpenAILanguageModel 中的 API 调用逻辑，新增：
- 原生 function calling (OpenAI 工具调用协议)
- DeepSeek v4 reasoning_content 提取
- 真正的流式响应 (含 tool call 流式)
- 指数退避 + 断路器重试
- 多 API Key 轮换

设计为协议无关：任何兼容 OpenAI Chat Completions API 的提供商
(DeepSeek, Qwen, GPT, vLLM, local proxies) 均可使用。

用法：
    from src.llm.openai_compat import OpenAICompatClient
    from src.llm.base import SystemMessage, UserMessage

    client = OpenAICompatClient(
        api_key="sk-...",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
    )

    # 普通聊天
    result = await client.chat([SystemMessage("你好"), UserMessage("你是谁")])

    # 带工具调用
    result = await client.chat_with_tools(messages, tools=[my_tool])

    # 流式
    async for chunk in client.chat_stream(messages):
        print(chunk.delta_content, end="")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Any, AsyncIterator, Callable, Literal, Optional

from openai import AsyncOpenAI

from src.llm.base import (
    MODEL_CAPABILITIES_PRESETS,
    AsyncChatModel,
    ChatChunk,
    ChatResult,
    Message,
    ModelCapabilities,
    SystemMessage,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    UserMessage,
    AssistantMessage,
    ToolMessage,
    detect_capabilities,
    messages_to_openai,
)
from src.llm.retry import (
    CircuitBreaker,
    CircuitBreakerOpen,
    RetryConfig,
    RetryExhausted,
    async_retry,
    classify_exception,
    ErrorCategory,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 配置常量
# ═══════════════════════════════════════════════════════════════════════════════

# 默认不发送 reasoning_content 给下一轮对话（节省 token）
STRIP_REASONING_BY_DEFAULT = True

# DeepSeek 推理模型 (R1 系列, 有 reasoning_content)
DEEPSEEK_REASONING_MODELS = {
    "deepseek-reasoner", "deepseek-r1",
}

# Qwen3 模型需要特殊参数来禁用 thinking（非思考模式）
QWEN3_MODELS = {"qwen3", "qwen3-32b", "qwen3-72b", "qwen3-235b"}


# ═══════════════════════════════════════════════════════════════════════════════
# 客户端实现
# ═══════════════════════════════════════════════════════════════════════════════

class OpenAICompatClient(AsyncChatModel):
    """
    通用 OpenAI-Compatible 异步客户端

    支持所有兼容 OpenAI Chat Completions API 的 LLM 提供商。
    自动检测模型能力并启用对应功能。

    特性:
        - 原生工具调用 (function calling)
        - reasoning_content 提取 (DeepSeek / Qwen3)
        - 流式响应 (含增量 tool call)
        - 多 API Key 轮换
        - 断路器保护
        - Token 用量追踪
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        model: str = "",
        api_key_list: Optional[list[str]] = None,
        role_name: str = "",
        # 重试配置
        max_retries: int = 5,
        retry_base_delay: float = 1.0,
        retry_max_delay: float = 60.0,
        # 断路器配置
        circuit_breaker_threshold: int = 7,
        circuit_breaker_recovery: float = 60.0,
        # 行为配置
        strip_reasoning: bool = True,
        enable_thinking: bool = True,
        log_dir: str = "logs/llm",
    ):
        """
        Args:
            api_key: API 密钥 (空字符串则从环境变量读取)
            base_url: API 基础 URL
            model: 模型名称
            api_key_list: 多 API Key 列表 (轮换)
            role_name: Agent 角色名 (用于日志)
            max_retries: 最大重试次数
            retry_base_delay: 重试基础延迟 (秒)
            retry_max_delay: 重试最大延迟 (秒)
            circuit_breaker_threshold: 断路器熔断阈值 (连续失败次数)
            circuit_breaker_recovery: 断路器恢复超时 (秒)
            strip_reasoning: 是否从非流式响应中剥离 reasoning_content
            enable_thinking: 是否启用 LLM 思考模式 (DeepSeek thinking / Qwen3 enable_thinking)
            log_dir: 日志/数据目录
        """
        # API 配置
        if api_key == "" or api_key is None:
            api_key = os.environ.get("OPENAI_API_KEY", "")
        if api_key == "":
            raise RuntimeError(
                "未找到 LLM API Key。\n"
                "  解决: 复制 config/secrets.template.yaml 为 config/secrets.yaml 并填入 llm.api_key,\n"
                "  或设置环境变量 LLM_API_KEY / OPENAI_API_KEY。"
            )

        self._api_key = api_key
        self._api_key_list = list(set(api_key_list)) if api_key_list else [api_key]
        self._base_url = base_url.rstrip("/")
        self._model = model or os.environ.get("LLM_MODEL") or "deepseek-chat"
        self.role_name = role_name

        # 行为配置
        self._strip_reasoning = strip_reasoning
        self._enable_thinking = enable_thinking
        self._log_dir = log_dir

        # 重试配置
        self._retry_config = RetryConfig(
            max_retries=max_retries,
            base_delay=retry_base_delay,
            max_delay=retry_max_delay,
            jitter=True,
        )

        # 断路器
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=circuit_breaker_threshold,
            recovery_timeout=circuit_breaker_recovery,
        )

        # 模型能力
        self._capabilities = detect_capabilities(model)

        # 创建异步 client (每次调用时重新创建以轮换 API Key)
        self._create_client()

        # 确保日志目录存在
        if not os.path.exists(self._log_dir):
            os.makedirs(self._log_dir, exist_ok=True)

        logger.info(
            f"OpenAICompatClient 初始化: model={model}, "
            f"base_url={base_url}, "
            f"tool_calling={self._capabilities.supports_tool_calling}, "
            f"reasoning={self._capabilities.supports_reasoning}"
        )

    # ── 属性 ──────────────────────────────────────────────────────────────

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    @property
    def circuit_breaker_state(self) -> str:
        return self._circuit_breaker.state.value

    # ── 内部方法 ──────────────────────────────────────────────────────────

    def _create_client(self):
        """创建或重新创建 AsyncOpenAI 客户端 (用于 Key 轮换)"""
        key = random.choice(self._api_key_list)
        self._async_client = AsyncOpenAI(
            api_key=key,
            base_url=self._base_url,
            max_retries=0,  # 我们自己管理重试
            timeout=120.0,
        )

    def _get_extra_params(self) -> dict:
        """
        获取特定模型的额外参数

        DeepSeek v4: 通过 thinking 参数控制思考模式
        Qwen3: 通过 enable_thinking 参数控制思考模式
        """
        model_lower = self._model.lower()

        # 如果 thinking 被禁用，发送对应的 API 参数
        if not self._enable_thinking:
            # DeepSeek 模型: 使用 thinking 参数禁用思考
            if any(name in model_lower for name in DEEPSEEK_REASONING_MODELS):
                return {"extra_body": {"thinking": {"type": "disabled"}}}

            # Qwen3 本地部署 (localhost) 使用 chat_template_kwargs
            if any(name in model_lower for name in QWEN3_MODELS):
                base_lower = self._base_url.lower()
                if "localhost" in base_lower or "127.0.0.1" in base_lower:
                    return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
                # Qwen3 云端 API
                return {"extra_body": {"enable_thinking": False}}

        # 即使 enable_thinking=True, Qwen3 本地部署默认也需要禁用思考
        # (Qwen3 本地版思考过程冗长且不必要)
        if self._enable_thinking:
            if any(name in model_lower for name in QWEN3_MODELS):
                base_lower = self._base_url.lower()
                if "localhost" in base_lower or "127.0.0.1" in base_lower:
                    return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}

        return {}

    def _build_api_params(
        self,
        messages: list[Message],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        tool_choice: Literal["auto", "none", "required"] = "auto",
        stream: bool = False,
    ) -> dict:
        """构建 OpenAI API 请求参数"""
        params: dict[str, Any] = {
            "model": self._model,
            "messages": messages_to_openai(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }

        # 工具定义
        if tools and self._capabilities.supports_tool_calling:
            params["tools"] = [t.to_openai_schema() for t in tools]
            params["tool_choice"] = tool_choice

        # 模型特殊参数
        params.update(self._get_extra_params())

        # DeepSeek reasoning 流式配置 (仅在 thinking 启用时请求 reasoning)
        if stream and self._enable_thinking and self._capabilities.supports_reasoning:
            params["stream_options"] = {"include_reasoning": True}

        return params

    def _process_response(self, response: Any) -> ChatResult:
        """处理非流式 API 响应，提取所有字段"""
        choice = response.choices[0]
        message = choice.message

        # 提取内容
        content = message.content
        reasoning = getattr(message, "reasoning_content", None)

        # 如果不需要 reasoning，剥离它
        if self._strip_reasoning and reasoning:
            reasoning = None

        # 提取工具调用
        tool_calls = None
        if hasattr(message, "tool_calls") and message.tool_calls:
            tool_calls = [ToolCall.from_openai(tc) for tc in message.tool_calls]

        # 提取用量
        usage = TokenUsage.from_openai(response.usage) if hasattr(response, "usage") else TokenUsage()

        return ChatResult(
            content=content,
            reasoning=reasoning,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=choice.finish_reason or "stop",
            model=response.model,
        )

    async def _stream_response(
        self,
        params: dict,
    ) -> AsyncIterator[ChatChunk]:
        """处理流式 API 响应"""

        # 用于累积流式 tool calls
        tool_call_accumulators: dict[int, dict] = {}

        try:
            stream = await self._async_client.chat.completions.create(**params)

            async for chunk in stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta

                    # 提取 delta 内容
                    delta_content = getattr(delta, "content", None)
                    delta_reasoning = getattr(delta, "reasoning_content", None)

                    # 处理流式 tool calls
                    tool_call_chunk = None
                    if hasattr(delta, "tool_calls") and delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_call_accumulators:
                                tool_call_accumulators[idx] = {
                                    "id": tc_delta.id or "",
                                    "name": "",
                                    "arguments": "",
                                }
                            acc = tool_call_accumulators[idx]
                            if tc_delta.id:
                                acc["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    acc["name"] += tc_delta.function.name
                                if tc_delta.function.arguments:
                                    acc["arguments"] += tc_delta.function.arguments

                        tool_call_chunk = {
                            "index": idx,
                            "id": tool_call_accumulators[idx].get("id"),
                            "name": tool_call_accumulators[idx].get("name"),
                            "arguments": tool_call_accumulators[idx].get("arguments"),
                        }

                    # 提取 finish_reason
                    finish_reason = chunk.choices[0].finish_reason

                    # 提取 usage (某些流式最后一个 chunk 会包含)
                    usage = None
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage = TokenUsage.from_openai(chunk.usage)

                    if delta_content or delta_reasoning or tool_call_chunk or finish_reason:
                        yield ChatChunk(
                            delta_content=delta_content,
                            delta_reasoning=delta_reasoning,
                            tool_call_chunk=tool_call_chunk,
                            finish_reason=finish_reason,
                            usage=usage,
                        )

        except Exception as e:
            logger.error(f"流式响应错误: {type(e).__name__}: {e}")
            raise

    # ── 公共接口 ──────────────────────────────────────────────────────────

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

        支持工具调用。自动重试临时错误。
        """
        params = self._build_api_params(
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            stream=False,
        )

        async def _call():
            self._create_client()  # Key 轮换
            response = await self._async_client.chat.completions.create(**params)
            return self._process_response(response)

        try:
            result = await async_retry(
                _call,
                config=self._retry_config,
                circuit_breaker=self._circuit_breaker,
            )

            # 日志记录
            self._log_call(messages, result)
            return result

        except RetryExhausted as e:
            logger.error(f"LLM 调用失败 (重试耗尽): {e.last_error}")
            raise
        except CircuitBreakerOpen as e:
            logger.error(f"LLM 调用被断路器拒绝: {e}")
            raise

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

        DeepSeek v4 会在前几个 chunk 中返回 reasoning_content，
        之后返回 content 或 tool_calls。
        """
        params = self._build_api_params(
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            stream=True,
        )

        async def _stream_all():
            chunks = []
            async for chunk in self._stream_response(params):
                chunks.append(chunk)
                yield chunk

            # 流结束后记录日志
            self._log_stream(messages, chunks)

        # 流式调用（带重试 — 但如果流已经开始则无法重试）
        try:
            async for chunk in _stream_all():
                yield chunk
        except Exception as e:
            category = classify_exception(e)
            if category == ErrorCategory.FATAL:
                raise
            logger.warning(f"流式响应中断: {type(e).__name__}: {e}")
            # 流式中断返回错误
            yield ChatChunk(
                delta_content=f"\n[流式响应中断: {e}]",
                finish_reason="error",
            )

    async def chat_with_tools(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> ChatResult:
        """
        带工具调用的聊天 — 强制 auto 模式

        覆盖基类方法以使用 tool_choice="auto"
        """
        return await self.chat(
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice="auto",
        )

    # ── 日志方法 ──────────────────────────────────────────────────────────

    def _log_call(self, messages: list[Message], result: ChatResult):
        """记录非流式调用"""
        try:
            log_entry = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "model": result.model,
                "messages_count": len(messages),
                "has_tool_calls": result.has_tool_calls,
                "has_reasoning": result.reasoning is not None,
                "finish_reason": result.finish_reason,
                "usage": {
                    "prompt": result.usage.prompt_tokens,
                    "completion": result.usage.completion_tokens,
                    "total": result.usage.total_tokens,
                    "reasoning": result.usage.reasoning_tokens,
                },
            }
            log_path = os.path.join(self._log_dir, "llm_calls.jsonl")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"日志记录失败: {e}")

    def _log_stream(self, messages: list[Message], chunks: list[ChatChunk]):
        """记录流式调用"""
        try:
            full_content = "".join(
                c.delta_content for c in chunks if c.delta_content
            )
            full_reasoning = "".join(
                c.delta_reasoning for c in chunks if c.delta_reasoning
            )
            last_usage = None
            for c in reversed(chunks):
                if c.usage:
                    last_usage = c.usage
                    break

            log_entry = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "model": self._model,
                "messages_count": len(messages),
                "stream": True,
                "chunks": len(chunks),
                "content_length": len(full_content),
                "reasoning_length": len(full_reasoning) if full_reasoning else 0,
                "usage": {
                    "prompt": last_usage.prompt_tokens if last_usage else 0,
                    "completion": last_usage.completion_tokens if last_usage else 0,
                    "total": last_usage.total_tokens if last_usage else 0,
                    "reasoning": last_usage.reasoning_tokens if last_usage else 0,
                },
            }
            log_path = os.path.join(self._log_dir, "llm_calls.jsonl")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"流式日志记录失败: {e}")

    async def close(self):
        """关闭客户端"""
        await self._async_client.close()

    # ── 兼容旧版 API ──────────────────────────────────────────────────────

    async def few_shot_generate(
        self,
        system_prompt: str = "",
        examples: list[str] = [],
        max_tokens: int = 4096,
        temperature: float = 0.0,
        stream: bool = False,
    ) -> str:
        """
        兼容旧版 few_shot_generate_thoughts() 接口

        将旧的 system_prompt + alternating examples 格式
        转换为新的 Message 列表并调用。

        Args:
            system_prompt: 系统提示词
            examples: 示例列表 (奇数次: user, 偶数次: assistant)
            max_tokens: 最大输出
            temperature: 温度
            stream: 是否流式

        Returns:
            LLM 响应文本
        """
        messages: list[Message] = [SystemMessage(content=system_prompt)]

        for i, example in enumerate(examples):
            if i % 2 == 0:
                messages.append(UserMessage(content=example))
            else:
                messages.append(AssistantMessage(content=example))

        if stream:
            content_parts = []
            async for chunk in self.chat_stream(
                messages, temperature=temperature, max_tokens=max_tokens
            ):
                if chunk.delta_content:
                    content_parts.append(chunk.delta_content)
            return "".join(content_parts)
        else:
            result = await self.chat(
                messages, temperature=temperature, max_tokens=max_tokens
            )
            return result.content or ""

    def few_shot_generate_sync(
        self,
        system_prompt: str = "",
        examples: list[str] = [],
        max_tokens: int = 4096,
        temperature: float = 0.0,
        stream: bool = False,
    ) -> str:
        """
        同步版本的 few_shot_generate (兼容旧代码)
        """
        return asyncio.run(
            self.few_shot_generate(
                system_prompt=system_prompt,
                examples=examples,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=stream,
            )
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 工具调用循环 — 高层封装
# ═══════════════════════════════════════════════════════════════════════════════

class ToolCallLoop:
    """
    工具调用循环 — 替代 LangChain ReAct Agent

    实现 OpenAI 原生的工具调用循环：
    1. 发送消息 + 工具定义给 LLM
    2. 如果 LLM 返回 tool_calls → 执行工具 → 将结果 append 到消息 → 回到 1
    3. 如果 LLM 返回 content → 返回最终结果

    支持中断检测和最大步数限制。

    用法:
        loop = ToolCallLoop(
            client=client,
            tools=registry.get_openai_tools(),
            on_tool_call=lambda name, args: registry.execute(name, args),
        )
        result = await loop.run(system_prompt="你是一个Minecraft助手", user_message="挖矿")
    """

    def __init__(
        self,
        client: OpenAICompatClient,
        tools: list[ToolDefinition],
        on_tool_call: Callable[[str, dict], Any],
        max_steps: int = 15,
        stream_callback: Optional[Callable[[ChatChunk], None]] = None,
    ):
        """
        Args:
            client: LLM 客户端
            tools: 可用工具列表
            on_tool_call: 工具执行回调 (name, arguments) -> result_dict
            max_steps: 最大工具调用步数 (防止无限循环)
            stream_callback: 流式回调 (实时 UI 更新)
        """
        self._client = client
        self._tools = tools
        self._on_tool_call = on_tool_call
        self._max_steps = max_steps
        self._stream_callback = stream_callback

    async def run(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> ChatResult:
        """
        运行工具调用循环

        Args:
            system_prompt: 系统提示词
            user_message: 用户消息
            temperature: 采样温度
            max_tokens: 最大输出 token

        Returns:
            最终的 ChatResult (包含 content 或最后一步的 tool_calls)
        """
        messages: list[Message] = [
            SystemMessage(content=system_prompt),
            UserMessage(content=user_message),
        ]

        for step in range(self._max_steps):
            result = await self._client.chat_with_tools(
                messages=messages,
                tools=self._tools,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            # 如果 LLM 返回了文本内容（最终回答），停止循环
            if result.has_content and not result.has_tool_calls:
                return result

            # 如果有 tool calls，执行它们
            if result.has_tool_calls:
                # 将 assistant 消息（含 tool_calls）加入历史
                messages.append(AssistantMessage(
                    content=result.content,
                    tool_calls=result.tool_calls,
                ))

                # 执行每个 tool call
                for tc in result.tool_calls:
                    try:
                        tool_result = self._on_tool_call(tc.name, tc.arguments)
                        result_str = json.dumps(tool_result, ensure_ascii=False)
                    except Exception as e:
                        result_str = json.dumps({"error": str(e)}, ensure_ascii=False)

                    messages.append(ToolMessage(
                        tool_call_id=tc.id,
                        name=tc.name,
                        content=result_str,
                    ))

                continue

            # 没有内容也没有 tool calls — 可能是拒绝回答等情况
            return result

        # 达到最大步数
        return ChatResult(
            content=f"任务在 {self._max_steps} 步内未完成，请检查任务复杂度。",
            finish_reason="stop",
            model=self._client.model_name,
        )
