"""
结构化日志系统 — Phase 7 日志与调试基础设施

JSONL 格式 (每行一个 JSON 对象), 支持:
- LLM 请求日志 (request/response/tokens)
- Agent 动作日志 (action/parameters/result/timing)
- 聊天日志 (在游戏中)
- 错误日志 (LLM/Minecraft/System)
- 系统事件日志 (start/stop/health)
- Token 用量统计 (每日汇总)
- 自动保留策略 (retention_days)

所有日志文件存储在 logs/ 目录下。

用法:
    log = StructuredLogger(agent_name="Bot1")
    log.llm_request(model="deepseek-chat", prompt_tokens=1024, ...)
    log.agent_action("mineBlock", {"x":10,"y":64,"z":20}, result, 1.5)
    log.chat("Steve", "你好", "outgoing")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 日志条目
# ═══════════════════════════════════════════════════════════════════════════════

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


@dataclass
class LogEntry:
    """日志条目基类"""
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {"timestamp": self.timestamp}


# ═══════════════════════════════════════════════════════════════════════════════

class StructuredLogger:
    """
    结构化日志记录器

    为每个 Agent 创建独立的日志目录。
    所有写入操作通过 asyncio 执行器异步进行, 不阻塞事件循环。

    目录结构:
        logs/
        ├── agent/{agent_name}/
        │   ├── actions.jsonl     # Agent 动作日志
        │   ├── chat.jsonl        # 聊天记录
        │   └── thoughts.md       # 思考过程
        ├── llm/
        │   ├── requests.jsonl    # LLM 请求
        │   ├── tokens_daily.json # 每日 Token 汇总
        │   └── errors.jsonl      # LLM 错误
        ├── system/
        │   ├── events.jsonl      # 系统事件
        │   └── performance.jsonl # 性能指标
        └── debug/
            └── {date}/
                └── full_context.json  # 完整上下文快照
    """

    def __init__(
        self,
        agent_name: str = "default",
        log_dir: str = "logs",
        retention_days: int = 30,
        max_file_size_mb: int = 50,
    ):
        self.agent_name = agent_name
        self.log_dir = log_dir
        self.retention_days = retention_days
        self.max_file_size = max_file_size_mb * 1024 * 1024  # bytes

        # 确保目录存在
        self._agent_dir = os.path.join(log_dir, "agent", agent_name)
        self._llm_dir = os.path.join(log_dir, "llm")
        self._system_dir = os.path.join(log_dir, "system")
        self._debug_dir = os.path.join(log_dir, "debug", time.strftime("%Y-%m-%d"))

        for d in [self._agent_dir, self._llm_dir, self._system_dir, self._debug_dir]:
            os.makedirs(d, exist_ok=True)

    # ── Agent 动作日志 ────────────────────────────────────────────────

    async def agent_action(
        self,
        tool_name: str,
        args: dict,
        result: dict,
        duration_ms: float = 0.0,
        success: bool = True,
    ):
        """记录 Agent 工具调用"""
        entry = {
            "timestamp": _now(),
            "tool": tool_name,
            "args": args,
            "result": result,
            "duration_ms": round(duration_ms, 1),
            "success": success,
        }
        await self._append_jsonl("actions.jsonl", entry)

    async def agent_thought(self, thought: str, step: int = 0):
        """追加思考过程到 thoughts.md"""
        path = os.path.join(self._agent_dir, "thoughts.md")
        content = f"\n## Step {step} — {_now()}\n\n{thought}\n\n---\n"
        await self._append_text(path, content)

    async def agent_chat(self, player: str, message: str, direction: str = "outgoing"):
        """记录聊天"""
        entry = {
            "timestamp": _now(),
            "direction": direction,  # incoming / outgoing
            "player": player,
            "message": message,
        }
        await self._append_jsonl("chat.jsonl", entry)

    # ── LLM 请求日志 ──────────────────────────────────────────────────

    async def llm_request(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        duration_ms: float = 0.0,
        has_tool_calls: bool = False,
        has_reasoning: bool = False,
        success: bool = True,
        error: str = "",
    ):
        """记录 LLM API 调用"""
        entry = {
            "timestamp": _now(),
            "model": model,
            "agent": self.agent_name,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "duration_ms": round(duration_ms, 1),
            "has_tool_calls": has_tool_calls,
            "has_reasoning": has_reasoning,
            "success": success,
            "error": error,
        }
        await self._append_jsonl(os.path.join(self._llm_dir, "requests.jsonl"), entry)

        # 更新每日汇总
        await self._update_daily_tokens(prompt_tokens + completion_tokens, bool(error))

    async def llm_error(self, model: str, error_type: str, error_msg: str, retry_count: int = 0):
        """记录 LLM 错误"""
        entry = {
            "timestamp": _now(),
            "model": model,
            "agent": self.agent_name,
            "error_type": error_type,
            "error_message": error_msg[:500],
            "retry_count": retry_count,
        }
        await self._append_jsonl(os.path.join(self._llm_dir, "errors.jsonl"), entry)

    async def agent_error(self, error_type: str, error_msg: str, step: int = 0):
        """记录 Agent 处理错误"""
        entry = {
            "timestamp": _now(),
            "agent": self.agent_name,
            "error_type": error_type,
            "error_message": error_msg[:500],
            "step": step,
        }
        await self._append_jsonl("errors.jsonl", entry)

    # ── 系统日志 ──────────────────────────────────────────────────────

    async def system_event(self, event_type: str, details: dict = None):
        """记录系统事件"""
        entry = {
            "timestamp": _now(),
            "event": event_type,
            "agent": self.agent_name,
            "details": details or {},
        }
        await self._append_jsonl(os.path.join(self._system_dir, "events.jsonl"), entry)

    async def performance_metric(self, metric: str, value: float, unit: str = ""):
        """记录性能指标"""
        entry = {
            "timestamp": _now(),
            "metric": metric,
            "value": value,
            "unit": unit,
            "agent": self.agent_name,
        }
        await self._append_jsonl(
            os.path.join(self._system_dir, "performance.jsonl"), entry
        )

    # ── 调试快照 ──────────────────────────────────────────────────────

    async def debug_snapshot(self, context: dict, label: str = ""):
        """保存完整上下文快照 (调试)"""
        entry = {
            "timestamp": _now(),
            "label": label,
            "agent": self.agent_name,
            "context": context,
        }
        filename = os.path.join(
            self._debug_dir,
            f"snapshot_{time.strftime('%H%M%S')}_{label}.json",
        )
        await self._write_json(filename, entry)

    # ── 查询 ──────────────────────────────────────────────────────────

    async def get_recent_actions(self, n: int = 50) -> list[dict]:
        """获取最近 N 条动作日志"""
        return await self._read_jsonl_recent(
            os.path.join(self._agent_dir, "actions.jsonl"), n
        )

    async def get_llm_requests_today(self) -> list[dict]:
        """获取今日 LLM 请求"""
        today = time.strftime("%Y-%m-%d")
        requests = await self._read_jsonl_recent(
            os.path.join(self._llm_dir, "requests.jsonl"), 500
        )
        return [r for r in requests if r.get("timestamp", "").startswith(today)]

    async def get_token_summary(self) -> dict:
        """获取 Token 用量摘要"""
        path = os.path.join(self._llm_dir, "tokens_daily.json")
        try:
            data = await self._read_json(path)
            return data
        except Exception:
            return {}

    async def get_system_events_today(self) -> list[dict]:
        """获取今日系统事件"""
        today = time.strftime("%Y-%m-%d")
        events = await self._read_jsonl_recent(
            os.path.join(self._system_dir, "events.jsonl"), 200
        )
        return [e for e in events if e.get("timestamp", "").startswith(today)]

    # ── 内部 I/O (异步文件操作) ──────────────────────────────────────

    async def _append_jsonl(self, path: str, entry: dict):
        """追加一行 JSON 到文件"""
        if path.startswith("logs/"):
            path = os.path.join(self.log_dir, "..", path) if ".." in path else path
        full_path = os.path.join(self.log_dir, "..", path) if not path.startswith(self.log_dir) else path
        # Normalize path
        if not os.path.isabs(path):
            path = os.path.join(os.getcwd(), path)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        line = json.dumps(entry, ensure_ascii=False) + "\n"
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: open(path, "a", encoding="utf-8").write(line))

        # 检查文件大小, 必要时轮转
        await self._maybe_rotate(path)

    async def _append_text(self, path: str, content: str):
        """追加文本到文件"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: open(path, "a", encoding="utf-8").write(content)
        )

    async def _write_json(self, path: str, data: dict):
        """写入 JSON 文件"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: json.dump(data, open(path, "w", encoding="utf-8"),
                            ensure_ascii=False, indent=2),
        )

    async def _read_jsonl_recent(self, path: str, n: int = 50) -> list[dict]:
        """读取 JSONL 文件的最近 N 行"""
        if not os.path.exists(path):
            return []
        loop = asyncio.get_event_loop()

        def _read():
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            results = []
            for line in lines[-n:]:
                line = line.strip()
                if line:
                    try:
                        results.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return results

        return await loop.run_in_executor(None, _read)

    async def _read_json(self, path: str) -> dict:
        """读取 JSON 文件"""
        if not os.path.exists(path):
            return {}
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: json.load(open(path, "r", encoding="utf-8")),
        )

    async def _maybe_rotate(self, path: str):
        """文件过大时轮转"""
        try:
            size = os.path.getsize(path)
            if size > self.max_file_size:
                bak = path + f".{time.strftime('%Y%m%d_%H%M%S')}.bak"
                os.rename(path, bak)
                logger.info(f"日志轮转: {path} ({size / 1024 / 1024:.1f}MB)")
        except OSError:
            pass

    # ── 每日 Token 汇总 ──────────────────────────────────────────────

    async def _update_daily_tokens(self, tokens: int, is_error: bool):
        """更新每日 Token 统计"""
        today = time.strftime("%Y-%m-%d")
        path = os.path.join(self._llm_dir, "tokens_daily.json")

        data = await self._read_json(path)
        if today not in data:
            data[today] = {
                "date": today,
                "total_tokens": 0,
                "request_count": 0,
                "error_count": 0,
            }

        entry = data[today]
        entry["total_tokens"] += tokens
        entry["request_count"] += 1
        if is_error:
            entry["error_count"] += 1

        await self._write_json(path, data)

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def startup(self):
        await self.system_event("agent_started", {"agent": self.agent_name})
        logger.info(f"结构化日志已启动: {self.agent_name}")

    async def shutdown(self):
        await self.system_event("agent_stopped", {"agent": self.agent_name})
