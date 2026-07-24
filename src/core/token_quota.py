"""
Token 配额管理 — Phase 8 成本控制

每位玩家每日/每小时的 Token 使用限制。
- 接近限制时发出警告
- 配额耗尽时优雅降级 (回退到模板回复)
- 通过 Web 管理后台按玩家调整配额

用法:
    quota = TokenQuota(manager)
    quota.set_limits("Steve", daily=500000, hourly=100000)
    ok, msg = quota.check("Steve", tokens_used=15000)
    if not ok:
        return msg  # 配额不足的降级回复
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PlayerQuota:
    """玩家配额状态"""
    player_name: str
    daily_limit: int = 1_000_000
    hourly_limit: int = 100_000
    daily_used: int = 0
    hourly_used: int = 0
    last_reset_daily: str = ""   # YYYY-MM-DD
    last_reset_hourly: str = ""  # YYYY-MM-DD-HH
    warn_threshold: float = 0.8  # 80% 时警告
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "player_name": self.player_name,
            "daily_limit": self.daily_limit,
            "hourly_limit": self.hourly_limit,
            "daily_used": self.daily_used,
            "hourly_used": self.hourly_used,
            "last_reset_daily": self.last_reset_daily,
            "last_reset_hourly": self.last_reset_hourly,
            "warn_threshold": self.warn_threshold,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlayerQuota":
        return cls(
            player_name=data.get("player_name", ""),
            daily_limit=data.get("daily_limit", 1_000_000),
            hourly_limit=data.get("hourly_limit", 100_000),
            daily_used=data.get("daily_used", 0),
            hourly_used=data.get("hourly_used", 0),
            last_reset_daily=data.get("last_reset_daily", ""),
            last_reset_hourly=data.get("last_reset_hourly", ""),
            warn_threshold=data.get("warn_threshold", 0.8),
            enabled=data.get("enabled", True),
        )


# 降级回复模板 (配额耗尽时使用, 不消耗 Token)
FALLBACK_RESPONSES = {
    "greeting": [
        "你好！我今天已经说了很多话了，让我休息一下~有什么需要帮助的可以直接说哦。",
        "嗨！我现在处于节能模式，不过还是可以帮忙的！",
    ],
    "busy": [
        "抱歉，今天的对话配额已经用完了。请明天再来找我聊天吧！",
        "我已经说了很多话啦，需要休息一会儿。有紧急事情的话可以等一会儿再试试。",
    ],
    "help": [
        "你可以直接告诉我需要做什么，比如'挖矿'、'建造'、'合成'等。",
        "试试对我说具体指令：'帮我挖10个石头' 或者 '合成一把石镐'。",
    ],
    "error": [
        "哎呀，出错了...稍等一下再试试吧。",
        "抱歉，现在有点问题。请稍后再试。",
    ],
}


# ═══════════════════════════════════════════════════════════════════════════════

class TokenQuotaManager:
    """
    Token 配额管理器

    特性:
        - 每日/每小时双限制
        - 自动重置 (跨天/跨小时)
        - 80% 警告阈值
        - 配额耗尽 → 回退模板回复
        - JSON 文件持久化
        - Web 后台可调整

    用法:
        mgr = TokenQuotaManager(data_dir="data/quota")
        await mgr.load()

        ok, msg = await mgr.check("Steve", 15000)
        if not ok:
            return msg  # 降级回复

        await mgr.record_usage("Steve", 15000)
    """

    def __init__(
        self,
        data_dir: str = "data/quota",
        default_daily: int = 1_000_000,
        default_hourly: int = 100_000,
        global_enabled: bool = True,
    ):
        self.data_dir = data_dir
        self.default_daily = default_daily
        self.default_hourly = default_hourly
        self.global_enabled = global_enabled

        self._quotas: dict[str, PlayerQuota] = {}
        self._file_path = os.path.join(data_dir, "quotas.json")

    # ── 文件 I/O ──────────────────────────────────────────────────────

    async def load(self):
        """加载配额文件"""
        os.makedirs(self.data_dir, exist_ok=True)
        if os.path.exists(self._file_path):
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                data = await loop.run_in_executor(
                    None,
                    lambda: json.load(open(self._file_path, "r", encoding="utf-8")),
                )
                for item in data.get("players", []):
                    q = PlayerQuota.from_dict(item)
                    self._quotas[q.player_name.lower()] = q
                logger.info(f"配额已加载: {len(self._quotas)} 位玩家")
            except Exception as e:
                logger.warning(f"配额加载失败: {e}")

    async def save(self):
        """保存配额到文件"""
        import asyncio
        os.makedirs(self.data_dir, exist_ok=True)
        data = {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "players": [q.to_dict() for q in self._quotas.values()],
        }
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: json.dump(data, open(self._file_path, "w", encoding="utf-8"),
                            ensure_ascii=False, indent=2),
        )

    # ── 配额检查 ──────────────────────────────────────────────────────

    async def check(self, player_name: str, tokens: int = 0) -> tuple[bool, str]:
        """
        检查玩家是否还有配额

        Args:
            player_name: 玩家名称
            tokens: 本次将使用的 Token 数 (0 = 仅检查)

        Returns:
            (ok, message): ok=True 可以继续, ok=False 需要降级
        """
        if not self.global_enabled:
            return True, ""

        quota = self._get_or_create(player_name)
        if not quota.enabled:
            return True, ""

        # 检查是否需要重置
        self._maybe_reset(quota)

        # 检查每日限制
        if quota.daily_used + tokens > quota.daily_limit:
            return False, self._fallback("busy")

        # 检查每小时限制
        if quota.hourly_used + tokens > quota.hourly_limit:
            return False, self._fallback("busy")

        # 警告
        daily_ratio = quota.daily_used / quota.daily_limit
        if daily_ratio >= quota.warn_threshold:
            remaining = quota.daily_limit - quota.daily_used
            return True, self._warn_message(player_name, remaining)

        return True, ""

    async def record_usage(self, player_name: str, tokens: int):
        """记录 Token 使用"""
        if not self.global_enabled:
            return

        quota = self._get_or_create(player_name)
        self._maybe_reset(quota)
        quota.daily_used += tokens
        quota.hourly_used += tokens

        # 每 10 次记录保存一次
        if (quota.daily_used + quota.hourly_used) % (10 * 1000) < tokens:
            await self.save()

    async def get_remaining(self, player_name: str) -> dict:
        """获取玩家剩余配额"""
        quota = self._get_or_create(player_name)
        self._maybe_reset(quota)
        return {
            "daily_remaining": max(0, quota.daily_limit - quota.daily_used),
            "hourly_remaining": max(0, quota.hourly_limit - quota.hourly_used),
            "daily_limit": quota.daily_limit,
            "hourly_limit": quota.hourly_limit,
            "daily_used": quota.daily_used,
        }

    # ── 配额管理 ────────────────────────────────────────────────────

    def set_limits(self, player_name: str, daily: int = 0, hourly: int = 0):
        """设置玩家配额"""
        quota = self._get_or_create(player_name)
        if daily > 0:
            quota.daily_limit = daily
        if hourly > 0:
            quota.hourly_limit = hourly

    def set_enabled(self, player_name: str, enabled: bool):
        """启用/禁用玩家配额"""
        self._get_or_create(player_name).enabled = enabled

    def reset_player(self, player_name: str):
        """重置玩家用量"""
        key = player_name.lower()
        if key in self._quotas:
            q = self._quotas[key]
            q.daily_used = 0
            q.hourly_used = 0

    def get_all_quotas(self) -> dict[str, dict]:
        """获取所有玩家配额 (Web 后台)"""
        result = {}
        for name, q in self._quotas.items():
            self._maybe_reset(q)
            result[name] = {
                "daily": f"{q.daily_used}/{q.daily_limit}",
                "hourly": f"{q.hourly_used}/{q.hourly_limit}",
                "enabled": q.enabled,
            }
        return result

    # ── 内部方法 ──────────────────────────────────────────────────────

    def _get_or_create(self, player_name: str) -> PlayerQuota:
        """获取或创建玩家配额"""
        key = player_name.lower()
        if key not in self._quotas:
            self._quotas[key] = PlayerQuota(
                player_name=player_name,
                daily_limit=self.default_daily,
                hourly_limit=self.default_hourly,
            )
        return self._quotas[key]

    def _maybe_reset(self, quota: PlayerQuota):
        """检查并重置过期配额"""
        today = time.strftime("%Y-%m-%d")
        hour_key = time.strftime("%Y-%m-%d-%H")

        if quota.last_reset_daily != today:
            quota.daily_used = 0
            quota.last_reset_daily = today

        if quota.last_reset_hourly != hour_key:
            quota.hourly_used = 0
            quota.last_reset_hourly = hour_key

    def _warn_message(self, player_name: str, remaining: int) -> str:
        """配额警告消息"""
        return (
            f"[配额提醒] 今日剩余 Token: {remaining:,}。"
            f"请精简指令，或等待明天重置。"
        )

    def _fallback(self, category: str) -> str:
        """获取降级回复"""
        import random
        templates = FALLBACK_RESPONSES.get(category, FALLBACK_RESPONSES["busy"])
        return random.choice(templates)
