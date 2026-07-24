"""
热重载系统 — Phase 8 GitHub 配置热更新

从 GitHub 仓库拉取最新提示词/配置，无需重启即可生效。

特性:
    - 定期检查 GitHub 仓库更新
    - 检测 src/prompts/、config/、data/world/ 变更
    - 自动重载提示词模块
    - EventBus 通知所有 Agent
    - Web 后台可查看重载历史

用法:
    reloader = HotReloader(
        repo_url="https://github.com/user/repo",
        branch="main",
        event_bus=bus,
    )
    await reloader.start(check_interval=300)

配置 (config/default.yaml):
    hot_reload:
      enabled: true
      github_repo: "https://github.com/user/villager-agent-config"
      branch: "main"
      check_interval_seconds: 300
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ReloadEvent:
    """热重载事件"""
    timestamp: str = ""
    trigger: str = ""        # "scheduled" | "manual" | "webhook"
    files_changed: list[str] = field(default_factory=list)
    commit_hash: str = ""
    success: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "trigger": self.trigger,
            "files_changed": self.files_changed,
            "commit_hash": self.commit_hash,
            "success": self.success,
            "error": self.error,
        }


# 监控的目录 (相对于仓库根目录)
WATCH_PATHS = [
    "src/prompts/",
    "config/",
    "data/world/",
]

# 排除的文件模式
EXCLUDE_PATTERNS = [
    ".git",
    "__pycache__",
    "*.pyc",
    "*.bak",
    ".DS_Store",
]


# ═══════════════════════════════════════════════════════════════════════════════

class HotReloader:
    """
    GitHub 热重载管理器

    定期 fetch 仓库，检测 WATCH_PATHS 中的文件变更，
    发现变更后自动重载提示词模块并发送 EventBus 通知。

    用法:
        reloader = HotReloader(
            repo_url="https://github.com/user/my-config",
            event_bus=controller.event_bus,
        )
        await reloader.start(check_interval=300)
    """

    def __init__(
        self,
        repo_url: str = "",
        branch: str = "main",
        event_bus=None,  # EventBus
        controller=None,  # AgentController
        local_repo_path: str = ".hot_reload_repo",
    ):
        self.repo_url = repo_url
        self.branch = branch
        self.event_bus = event_bus
        self.controller = controller
        self.local_repo_path = local_repo_path

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_commit: str = ""
        self._history: list[ReloadEvent] = []  # 最近 50 条
        self._enabled = bool(repo_url)

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def start(self, check_interval: float = 300.0):
        """启动定期检查"""
        if not self._enabled:
            logger.info("热重载未启用 (未配置 GitHub 仓库)")
            return

        self._running = True
        self._task = asyncio.create_task(
            self._loop(check_interval), name="hot-reload"
        )
        logger.info(f"热重载已启动 (间隔: {check_interval}s, 分支: {self.branch})")

    async def stop(self):
        """停止热重载"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self, interval: float):
        """定期检查循环"""
        # 首次启动时先 clone/fetch
        await self._ensure_repo()

        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break
            try:
                await self.check_and_reload(trigger="scheduled")
            except Exception as e:
                logger.warning(f"热重载检查失败: {e}")

    # ── 仓库管理 ──────────────────────────────────────────────────────

    async def _ensure_repo(self):
        """确保本地仓库存在并更新"""
        loop = asyncio.get_event_loop()

        if os.path.exists(os.path.join(self.local_repo_path, ".git")):
            # 已存在, fetch
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["git", "fetch", "origin", self.branch],
                    cwd=self.local_repo_path,
                    capture_output=True,
                    timeout=30,
                ),
            )
        else:
            # 首次 clone
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["git", "clone", "--branch", self.branch,
                     "--single-branch", self.repo_url, self.local_repo_path],
                    capture_output=True,
                    timeout=60,
                ),
            )

    # ── 变更检测 ──────────────────────────────────────────────────────

    async def check_and_reload(self, trigger: str = "scheduled") -> Optional[ReloadEvent]:
        """
        检查变更并执行热重载

        Returns:
            ReloadEvent (None = 无变更)
        """
        if not os.path.exists(os.path.join(self.local_repo_path, ".git")):
            await self._ensure_repo()
            return None

        # 获取当前 HEAD
        current_head = await self._get_head()
        if not current_head:
            return None

        # 记录上次 commit
        if not self._last_commit:
            self._last_commit = current_head
            return None

        # 检测文件变更
        changed_files = await self._get_changed_files(self._last_commit, current_head)
        if not changed_files:
            return None

        # 过滤只关注 WATCH_PATHS
        relevant = [
            f for f in changed_files
            if any(f.startswith(p) for p in WATCH_PATHS)
        ]
        if not relevant:
            self._last_commit = current_head
            return None

        logger.info(f"检测到 {len(relevant)} 个文件变更: {relevant[:5]}")

        # 执行重载
        event = await self._reload(relevant, current_head, trigger)

        # 更新 commit 记录
        self._last_commit = current_head

        # 保存历史
        self._history.append(event)
        if len(self._history) > 50:
            self._history = self._history[-50:]

        return event

    async def _reload(self, files: list[str], commit: str, trigger: str) -> ReloadEvent:
        """执行热重载"""
        event = ReloadEvent(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            trigger=trigger,
            files_changed=files,
            commit_hash=commit[:12],
        )

        try:
            # 1. 重载提示词模块
            prompt_files = [f for f in files if "prompts" in f]
            if prompt_files:
                await self._reload_prompts(prompt_files)

            # 2. 重载世界配置
            world_files = [f for f in files if "world" in f]
            if world_files and self.controller:
                await self._reload_world_configs(world_files)

            # 3. 通知 EventBus
            if self.event_bus:
                from src.core.event_bus import Event, EventType
                await self.event_bus.publish(Event(
                    type=EventType.SYSTEM,
                    source="hot_reload",
                    data={
                        "type": "config_updated",
                        "files": files,
                        "commit": commit[:12],
                    },
                ))

            event.success = True
            logger.info(f"热重载成功: commit {commit[:12]}, {len(files)} 文件")

        except Exception as e:
            event.success = False
            event.error = str(e)
            logger.error(f"热重载失败: {e}")

        return event

    async def _reload_prompts(self, files: list[str]):
        """重载提示词模块"""
        import importlib
        try:
            import src.prompts.system_prompts as sp
            import src.prompts.emotions as em
            importlib.reload(sp)
            importlib.reload(em)
            logger.info("提示词模块已重载")
        except Exception as e:
            logger.warning(f"提示词重载失败: {e}")

    async def _reload_world_configs(self, files: list[str]):
        """重载世界配置"""
        if self.controller:
            for file in files:
                world_name = os.path.splitext(os.path.basename(file))[0]
                for agent in self.controller._agents.values():
                    if (agent.world_config and
                            agent.world_config.world_name == world_name):
                        await agent.world_config.load()
                        logger.info(f"世界配置已重载: {world_name}")

    # ── Git 操作 ────────────────────────────────────────────────────

    async def _get_head(self) -> str:
        """获取当前 HEAD commit"""
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=self.local_repo_path,
                    capture_output=True, text=True, timeout=10,
                ),
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    async def _get_changed_files(self, old_commit: str, new_commit: str) -> list[str]:
        """获取两个 commit 之间的变更文件列表"""
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["git", "diff", "--name-only", old_commit, new_commit],
                    cwd=self.local_repo_path,
                    capture_output=True, text=True, timeout=10,
                ),
            )
            if result.returncode == 0:
                return [f.strip() for f in result.stdout.split("\n") if f.strip()]
        except Exception:
            pass
        return []

    # ── 手动触发 ──────────────────────────────────────────────────────

    async def manual_reload(self) -> Optional[ReloadEvent]:
        """手动触发重载 (Web 后台调用)"""
        await self._ensure_repo()
        return await self.check_and_reload(trigger="manual")

    # ── 查询 ──────────────────────────────────────────────────────────

    def get_history(self, n: int = 20) -> list[dict]:
        """获取重载历史"""
        return [e.to_dict() for e in self._history[-n:]]

    @property
    def is_enabled(self) -> bool:
        return self._enabled
