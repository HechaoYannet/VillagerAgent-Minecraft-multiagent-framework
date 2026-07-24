"""
LLM 重试工具 — 指数退避 + 断路器模式

替代原有的 @retry(tries=10, delay=5, backoff=2, max_delay=60) 装饰器。

特性：
- 指数退避 + 随机抖动 (jitter)，避免惊群效应
- 断路器模式：连续失败 N 次后暂时熔断
- 错误分类：可重试错误 vs 致命错误
- Token 用量计入重试预算

用法：
    from model.retry_utils import RetryConfig, async_retry, CircuitBreaker

    retry = RetryConfig(max_retries=5, base_delay=1.0, max_delay=60.0)
    result = await async_retry(lambda: client.chat(...), config=retry)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import wraps
from typing import Any, Callable, Coroutine, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ═══════════════════════════════════════════════════════════════════════════════
# 错误分类
# ═══════════════════════════════════════════════════════════════════════════════

class ErrorCategory(Enum):
    """LLM API 错误类别"""
    RETRYABLE = auto()       # 临时错误 — 可重试
    RATE_LIMIT = auto()      # 速率限制 — 等待后重试
    AUTH_ERROR = auto()      # 认证错误 — 不可重试
    FATAL = auto()           # 致命错误 — 不可重试


def classify_http_error(status_code: int, response_body: str = "") -> ErrorCategory:
    """
    将 HTTP 状态码分类为错误类别

    参考 OpenAI API 错误码：
    - 429: 速率限制
    - 5xx: 服务端临时错误
    - 401/403: 认证错误
    - 400/404: 请求错误 (部分可重试)
    """
    if status_code == 429:
        return ErrorCategory.RATE_LIMIT
    if status_code in (500, 502, 503, 504):
        # 502/503/504 通常是临时的
        return ErrorCategory.RETRYABLE
    if status_code in (401, 403):
        # API Key 过期或权限不足
        if "expired" in response_body.lower() or "insufficient" in response_body.lower():
            return ErrorCategory.AUTH_ERROR
        return ErrorCategory.FATAL
    if status_code == 408:
        return ErrorCategory.RETRYABLE
    if status_code == 400:
        # 某些 400 错误是可重试的 (如 context_length_exceeded)
        if "context_length" in response_body.lower():
            return ErrorCategory.FATAL  # 上下文过长，重试无意义
        if "invalid_request" in response_body.lower():
            return ErrorCategory.FATAL
        return ErrorCategory.FATAL
    return ErrorCategory.FATAL


def classify_exception(e: Exception) -> ErrorCategory:
    """将异常分类"""
    import openai

    if isinstance(e, openai.RateLimitError):
        return ErrorCategory.RATE_LIMIT
    if isinstance(e, openai.APIConnectionError):
        return ErrorCategory.RETRYABLE
    if isinstance(e, openai.InternalServerError):
        return ErrorCategory.RETRYABLE
    if isinstance(e, openai.APITimeoutError):
        return ErrorCategory.RETRYABLE
    if isinstance(e, openai.AuthenticationError):
        return ErrorCategory.AUTH_ERROR
    if isinstance(e, openai.PermissionDeniedError):
        return ErrorCategory.AUTH_ERROR
    if isinstance(e, openai.APIStatusError):
        return classify_http_error(e.status_code, str(e.response) if hasattr(e, "response") else "")
    if isinstance(e, asyncio.TimeoutError):
        return ErrorCategory.RETRYABLE
    if isinstance(e, ConnectionError):
        return ErrorCategory.RETRYABLE
    return ErrorCategory.FATAL


def is_retryable(e: Exception) -> bool:
    """是否应该重试此异常"""
    return classify_exception(e) in (ErrorCategory.RETRYABLE, ErrorCategory.RATE_LIMIT)


# ═══════════════════════════════════════════════════════════════════════════════
# 重试配置
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RetryConfig:
    """重试配置"""

    max_retries: int = 5
    """最大重试次数"""

    base_delay: float = 1.0
    """基础延迟 (秒) — 首次重试等待约此时间"""

    max_delay: float = 60.0
    """最大延迟上限 (秒)"""

    backoff_factor: float = 2.0
    """退避因子 — 每次重试延迟乘以该值"""

    jitter: bool = True
    """是否添加随机抖动 (0-100% 的延迟)"""

    retry_on_rate_limit: bool = True
    """速率限制错误是否重试 (通常应该重试)"""

    rate_limit_respect_header: bool = True
    """是否遵从 Retry-After 响应头"""

    timeout: float = 120.0
    """单次调用超时时间 (秒)"""

    def delay_for_attempt(self, attempt: int, rate_limit_retry_after: Optional[float] = None) -> float:
        """计算第 N 次重试应等待的时间"""
        if rate_limit_retry_after is not None and self.rate_limit_respect_header:
            return rate_limit_retry_after + random.uniform(0, 1.0)

        delay = self.base_delay * (self.backoff_factor ** (attempt - 1))
        delay = min(delay, self.max_delay)

        if self.jitter:
            delay = delay * (0.5 + random.random())  # 50%-150% 抖动

        return delay


# ═══════════════════════════════════════════════════════════════════════════════
# 断路器
# ═══════════════════════════════════════════════════════════════════════════════

class CircuitState(Enum):
    CLOSED = "closed"             # 正常 — 请求通过
    OPEN = "open"                 # 熔断 — 请求直接失败
    HALF_OPEN = "half_open"       # 半开 — 允许一次探测请求


@dataclass
class CircuitBreaker:
    """
    断路器 — 防止重复调用失败的 LLM API

    状态转换：
        CLOSED → (连续失败 N 次) → OPEN
        OPEN → (等待 timeout 秒) → HALF_OPEN
        HALF_OPEN → (成功) → CLOSED
        HALF_OPEN → (失败) → OPEN
    """

    failure_threshold: int = 5
    """连续失败多少次后熔断"""

    recovery_timeout: float = 30.0
    """熔断后多少秒尝试恢复 (进入 HALF_OPEN)"""

    half_open_max_requests: int = 1
    """HALF_OPEN 状态允许通过的探测请求数"""

    # 内部状态
    _state: CircuitState = CircuitState.CLOSED
    _failure_count: int = 0
    _last_failure_time: float = 0.0
    _half_open_count: int = 0

    @property
    def state(self) -> CircuitState:
        self._maybe_transition()
        return self._state

    def _maybe_transition(self):
        """检查是否需要状态转换 (OPEN → HALF_OPEN)"""
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_count = 0
                logger.info("断路器: OPEN → HALF_OPEN (恢复超时到期)")

    def before_call(self) -> bool:
        """调用前检查 — 返回 True 表示允许调用"""
        self._maybe_transition()
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.HALF_OPEN:
            if self._half_open_count < self.half_open_max_requests:
                self._half_open_count += 1
                return True
            return False
        # OPEN
        return False

    def on_success(self):
        """调用成功时记录"""
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            logger.info("断路器: HALF_OPEN → CLOSED (探测成功)")
        elif self._state == CircuitState.CLOSED:
            self._failure_count = 0  # 重置计数器

    def on_failure(self):
        """调用失败时记录"""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            logger.warning("断路器: HALF_OPEN → OPEN (探测失败)")
        elif self._state == CircuitState.CLOSED and self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            logger.warning(
                f"断路器: CLOSED → OPEN (连续失败 {self._failure_count} 次，"
                f"熔断 {self.recovery_timeout}s)"
            )

    def reset(self):
        """手动重置断路器到 CLOSED 状态"""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._half_open_count = 0


# ═══════════════════════════════════════════════════════════════════════════════
# 异步重试装饰器
# ═══════════════════════════════════════════════════════════════════════════════

class RetryExhausted(Exception):
    """重试次数耗尽"""

    def __init__(self, message: str, last_error: Optional[Exception] = None):
        super().__init__(message)
        self.last_error = last_error


class CircuitBreakerOpen(Exception):
    """断路器已熔断"""
    pass


async def async_retry(
    fn: Callable[[], Coroutine[Any, Any, T]],
    config: RetryConfig = RetryConfig(),
    circuit_breaker: Optional[CircuitBreaker] = None,
    on_retry: Optional[Callable[[Exception, int, float], None]] = None,
) -> T:
    """
    异步重试 — 指数退避 + 断路器

    Args:
        fn: 异步可调用对象
        config: 重试配置
        circuit_breaker: 可选的断路器实例
        on_retry: 每次重试时的回调 (exception, attempt, delay)

    Returns:
        函数成功时的返回值

    Raises:
        RetryExhausted: 所有重试均失败
        CircuitBreakerOpen: 断路器已熔断
    """
    last_error: Optional[Exception] = None
    rate_limit_retry_after: Optional[float] = None

    for attempt in range(config.max_retries + 1):
        # 断路器检查
        if circuit_breaker is not None and attempt > 0:
            if not circuit_breaker.before_call():
                raise CircuitBreakerOpen(
                    f"断路器已熔断 (连续失败 {circuit_breaker._failure_count} 次)"
                )

        try:
            result = await asyncio.wait_for(fn(), timeout=config.timeout)
            if circuit_breaker is not None:
                circuit_breaker.on_success()
            return result

        except asyncio.TimeoutError as e:
            last_error = e
            category = ErrorCategory.RETRYABLE

        except Exception as e:
            last_error = e
            category = classify_exception(e)

        # 判断是否可重试
        if category == ErrorCategory.AUTH_ERROR:
            raise last_error  # 认证错误不重试

        if category == ErrorCategory.FATAL:
            raise last_error  # 致命错误不重试

        if category == ErrorCategory.RATE_LIMIT and not config.retry_on_rate_limit:
            raise last_error

        # 如果是最后一次尝试，不再重试
        if attempt >= config.max_retries:
            break

        # 断路器记录失败
        if circuit_breaker is not None:
            circuit_breaker.on_failure()

        # 计算延迟
        if category == ErrorCategory.RATE_LIMIT:
            retry_after = getattr(last_error, "retry_after", None) if last_error else None
            delay = config.delay_for_attempt(attempt + 1, retry_after)
        else:
            delay = config.delay_for_attempt(attempt + 1)

        if on_retry:
            on_retry(last_error, attempt + 1, delay)

        logger.warning(
            f"LLM 调用失败 (尝试 {attempt + 1}/{config.max_retries}): "
            f"{type(last_error).__name__}: {last_error} — "
            f"{delay:.1f}s 后重试"
        )

        await asyncio.sleep(delay)

    # 所有重试已耗尽
    raise RetryExhausted(
        f"LLM 调用在 {config.max_retries} 次重试后仍然失败: {last_error}",
        last_error=last_error,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 同步重试 (兼容层 — 用于现有代码迁移)
# ═══════════════════════════════════════════════════════════════════════════════

def sync_retry(
    fn: Callable[[], T],
    config: RetryConfig = RetryConfig(),
    circuit_breaker: Optional[CircuitBreaker] = None,
) -> T:
    """
    同步重试 — 兼容现有同步代码

    内部使用 asyncio.run() 来运行异步重试逻辑。
    仅用于迁移过渡期 — 新代码应直接使用 async_retry。
    """
    import asyncio

    async def _runner():
        return await async_retry(
            fn=lambda: asyncio.get_event_loop().run_in_executor(None, fn)
            if asyncio.iscoroutinefunction(fn) is False
            else fn(),
            config=config,
            circuit_breaker=circuit_breaker,
        )

    # 检查是否已在事件循环中
    try:
        loop = asyncio.get_running_loop()
        # 如果已在事件循环中，不能使用 run()
        # 回退到简单的同步重试
        return _sync_retry_fallback(fn, config, circuit_breaker)
    except RuntimeError:
        return asyncio.run(_runner())


def _sync_retry_fallback(
    fn: Callable[[], T],
    config: RetryConfig,
    circuit_breaker: Optional[CircuitBreaker],
) -> T:
    """同步重试的简单回退实现"""
    import time as time_module

    last_error = None
    for attempt in range(config.max_retries + 1):
        if circuit_breaker is not None and attempt > 0:
            if not circuit_breaker.before_call():
                raise CircuitBreakerOpen("断路器已熔断")

        try:
            result = fn()
            if circuit_breaker is not None:
                circuit_breaker.on_success()
            return result
        except Exception as e:
            last_error = e
            category = classify_exception(e)

            if category in (ErrorCategory.AUTH_ERROR, ErrorCategory.FATAL):
                raise

            if attempt >= config.max_retries:
                break

            if circuit_breaker is not None:
                circuit_breaker.on_failure()

            delay = config.delay_for_attempt(attempt + 1)
            logger.warning(f"重试 {attempt + 1}/{config.max_retries}: {e} — {delay:.1f}s")
            time_module.sleep(delay)

    raise RetryExhausted(
        f"在 {config.max_retries} 次重试后仍然失败: {last_error}",
        last_error=last_error,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 装饰器 (兼容旧版 @retry 用法)
# ═══════════════════════════════════════════════════════════════════════════════

def retry_with_backoff(
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    # 兼容旧版 retry 包的参数名
    tries: Optional[int] = None,
    delay: Optional[float] = None,
    backoff: Optional[float] = None,
):
    """
    装饰器 — 替代旧版 @retry 装饰器

    兼容旧版 retry 包的参数名:
        @retry_with_backoff(tries=10, delay=5, backoff=2, max_delay=60)
        def my_llm_call():
            ...

    也支持新参数名:
        @retry_with_backoff(max_retries=5, base_delay=1.0)
        def my_llm_call():
            ...
    """
    # 兼容旧版参数名
    if tries is not None:
        max_retries = tries
    if delay is not None:
        base_delay = delay
    if backoff is not None:
        backoff_factor = backoff

    config = RetryConfig(
        max_retries=max_retries,
        base_delay=base_delay,
        max_delay=max_delay,
        backoff_factor=backoff_factor,
        jitter=jitter,
    )

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            return sync_retry(lambda: func(*args, **kwargs), config=config)
        return wrapper
    return decorator
