"""
长期记忆系统 — Phase 4 JSON 文件持久化记忆

存储 Agent 需要跨会话保留的信息:
- 事件时间线 (什么时候做了什么)
- 位置知识库 (哪里有什么)
- 玩家偏好 (玩家喜欢什么)

当前实现使用 JSON 文件 (Phase 4)。
后续 Phase 可迁移到 ChromaDB 或 FAISS 向量数据库。

架构:
    LongTermMemory
    ├── EventTimeline    # 事件时间线 (最近 500 条)
    ├── LocationKnowledge # 位置知识库 (箱子内容、方块位置等)
    └── PlayerPreferences # 玩家偏好 (建筑风格、常用物品等)

用法:
    memory = LongTermMemory("my_world", data_dir="data/memory")
    await memory.load()

    await memory.record_event("在 x=200, z=300 发现钻石矿")
    await memory.remember_location("diamond_mine", x=200, y=12, z=300,
                                    "钻石矿脉, 约 6 个钻石矿")

    recent = memory.get_recent_events(10)
    diamonds = memory.find_locations_by_tag("diamond")
    pref = memory.get_player_preference("Steve", "building_style")
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TimelineEvent:
    """时间线事件"""
    timestamp: str  # ISO 格式
    description: str
    tags: list[str] = field(default_factory=list)
    importance: int = 1  # 1-5, 越高越重要
    related_location: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "description": self.description,
            "tags": self.tags,
            "importance": self.importance,
            "related_location": self.related_location,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TimelineEvent":
        return cls(
            timestamp=data.get("timestamp", ""),
            description=data.get("description", ""),
            tags=data.get("tags", []),
            importance=data.get("importance", 1),
            related_location=data.get("related_location"),
        )


@dataclass
class KnownLocation:
    """位置知识条目"""
    name: str
    x: float
    y: float
    z: float
    description: str
    tags: list[str] = field(default_factory=list)
    contents: dict[str, int] = field(default_factory=dict)  # {物品名: 数量}
    last_visited: Optional[str] = None
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "x": self.x, "y": self.y, "z": self.z,
            "description": self.description,
            "tags": self.tags,
            "contents": self.contents,
            "last_visited": self.last_visited,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KnownLocation":
        return cls(
            name=data.get("name", ""),
            x=data.get("x", 0), y=data.get("y", 0), z=data.get("z", 0),
            description=data.get("description", ""),
            tags=data.get("tags", []),
            contents=data.get("contents", {}),
            last_visited=data.get("last_visited"),
            created_at=data.get("created_at", ""),
        )


@dataclass
class PlayerProfile:
    """玩家档案"""
    name: str
    preferences: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    interaction_count: int = 0
    last_interaction: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "preferences": self.preferences,
            "notes": self.notes,
            "interaction_count": self.interaction_count,
            "last_interaction": self.last_interaction,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlayerProfile":
        return cls(
            name=data.get("name", ""),
            preferences=data.get("preferences", {}),
            notes=data.get("notes", []),
            interaction_count=data.get("interaction_count", 0),
            last_interaction=data.get("last_interaction"),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 长期记忆存储
# ═══════════════════════════════════════════════════════════════════════════════

class LongTermMemory:
    """
    JSON 文件持久化记忆存储

    三个存储区:
        - 事件时间线 (最近 500 条)
        - 位置知识库 (可无限增长)
        - 玩家档案 (每位玩家一份)

    自动保存策略:
        - 每 10 次记录操作后自动保存
        - 也可手动调用 save()
    """

    def __init__(
        self,
        world_name: str = "default",
        data_dir: str = "data/memory",
        auto_save_interval: int = 10,
    ):
        self.world_name = world_name
        self.data_dir = data_dir
        self.file_path = os.path.join(data_dir, f"{world_name}_memory.json")
        self.auto_save_interval = auto_save_interval

        # 存储
        self.timeline: list[TimelineEvent] = []
        self.locations: dict[str, KnownLocation] = {}
        self.players: dict[str, PlayerProfile] = {}

        # 状态
        self._loaded = False
        self._dirty_count = 0

    # ── 文件 I/O ──────────────────────────────────────────────────────

    async def load(self) -> bool:
        """加载记忆文件"""
        os.makedirs(self.data_dir, exist_ok=True)

        if not os.path.exists(self.file_path):
            logger.info(f"记忆文件不存在, 创建空白记忆: {self.file_path}")
            self._loaded = True
            return True

        try:
            data = await self._read_json()
            self._load_timeline(data.get("timeline", []))
            self._load_locations(data.get("locations", []))
            self._load_players(data.get("players", []))
            self._loaded = True
            logger.info(
                f"长期记忆已加载: {len(self.timeline)} 事件, "
                f"{len(self.locations)} 位置, {len(self.players)} 玩家"
            )
            return True
        except Exception as e:
            logger.error(f"记忆加载失败: {e}")
            self._loaded = True  # 标记为已加载，使用空白记忆
            return False

    async def save(self):
        """保存记忆到文件"""
        if not self._loaded:
            return

        data = {
            "world": self.world_name,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "timeline": [e.to_dict() for e in self.timeline[-500:]],
            "locations": [loc.to_dict() for loc in self.locations.values()],
            "players": [p.to_dict() for p in self.players.values()],
        }

        await self._write_json(data)
        self._dirty_count = 0
        logger.debug(f"记忆已保存: {len(self.timeline)} 事件")

    async def _read_json(self) -> dict:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: json.load(open(self.file_path, "r", encoding="utf-8")),
        )

    async def _write_json(self, data: dict):
        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: json.dump(
                data,
                open(self.file_path, "w", encoding="utf-8"),
                ensure_ascii=False,
                indent=2,
            ),
        )

    def _load_timeline(self, data: list[dict]):
        self.timeline = [TimelineEvent.from_dict(d) for d in data[-500:]]

    def _load_locations(self, data: list[dict]):
        self.locations = {}
        for d in data:
            loc = KnownLocation.from_dict(d)
            self.locations[loc.name.lower()] = loc

    def _load_players(self, data: list[dict]):
        self.players = {}
        for d in data:
            player = PlayerProfile.from_dict(d)
            self.players[player.name.lower()] = player

    async def _maybe_auto_save(self):
        """可能触发自动保存"""
        self._dirty_count += 1
        if self._dirty_count >= self.auto_save_interval:
            await self.save()

    # ── 事件时间线 ───────────────────────────────────────────────────

    async def record_event(
        self,
        description: str,
        tags: Optional[list[str]] = None,
        importance: int = 1,
        related_location: Optional[str] = None,
    ):
        """记录事件"""
        event = TimelineEvent(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            description=description,
            tags=tags or [],
            importance=min(max(importance, 1), 5),
            related_location=related_location,
        )
        self.timeline.append(event)

        # 保留最近 500 条
        if len(self.timeline) > 500:
            self.timeline = self.timeline[-500:]

        await self._maybe_auto_save()

    def get_recent_events(
        self,
        n: int = 20,
        min_importance: int = 0,
        tags: Optional[list[str]] = None,
    ) -> list[TimelineEvent]:
        """获取最近的事件"""
        events = self.timeline[-n:]
        if min_importance > 0:
            events = [e for e in events if e.importance >= min_importance]
        if tags:
            events = [e for e in events if any(t in e.tags for t in tags)]
        return list(reversed(events))

    def search_events(self, query: str, limit: int = 20) -> list[TimelineEvent]:
        """搜索事件 (简单关键词匹配)"""
        query_lower = query.lower()
        results = [
            e for e in self.timeline
            if query_lower in e.description.lower()
            or any(query_lower in tag.lower() for tag in e.tags)
        ]
        return list(reversed(results[-limit:]))

    def get_timeline_summary(self, n: int = 10) -> str:
        """生成时间线摘要文本"""
        events = self.get_recent_events(n, min_importance=1)
        if not events:
            return "暂无重要事件记录。"

        lines = ["## 最近事件时间线"]
        for e in events:
            lines.append(e.description if not e.tags
                        else f"- [{', '.join(e.tags)}] {e.description}")
        return "\n".join(lines)

    # ── 位置知识库 ───────────────────────────────────────────────────

    async def remember_location(
        self,
        name: str,
        x: float, y: float, z: float,
        description: str = "",
        tags: Optional[list[str]] = None,
        contents: Optional[dict[str, int]] = None,
    ):
        """记住或更新一个位置"""
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())

        key = name.lower()
        if key in self.locations:
            loc = self.locations[key]
            loc.x, loc.y, loc.z = x, y, z
            if description:
                loc.description = description
            if tags:
                loc.tags = list(set(loc.tags) | set(tags))
            if contents:
                loc.contents.update(contents)
            loc.last_visited = now
        else:
            self.locations[key] = KnownLocation(
                name=name, x=x, y=y, z=z,
                description=description,
                tags=tags or [],
                contents=contents or {},
                last_visited=now,
                created_at=now,
            )

        await self._maybe_auto_save()

    async def update_location_contents(self, name: str, contents: dict[str, int]):
        """更新位置的内容 (如箱子物品)"""
        key = name.lower()
        if key in self.locations:
            self.locations[key].contents = contents
            self.locations[key].last_visited = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime()
            )
            await self._maybe_auto_save()

    def find_locations_by_tag(self, tag: str) -> list[KnownLocation]:
        """按标签搜索位置"""
        results = [
            loc for loc in self.locations.values()
            if tag.lower() in [t.lower() for t in loc.tags]
        ]
        return sorted(
            results,
            key=lambda loc: (
                int(loc.last_visited[:4]) if loc.last_visited else 0
            ),
            reverse=True,
        )

    def find_locations_by_name(self, query: str) -> list[KnownLocation]:
        """按名称搜索位置"""
        query_lower = query.lower()
        return [
            loc for loc in self.locations.values()
            if query_lower in loc.name.lower()
            or query_lower in loc.description.lower()
        ]

    def find_nearest_location(self, x: float, y: float, z: float,
                              min_results: int = 3) -> list[KnownLocation]:
        """找最近的位置"""
        if not self.locations:
            return []
        sorted_locs = sorted(
            self.locations.values(),
            key=lambda loc: (loc.x - x) ** 2 + (loc.y - y) ** 2 + (loc.z - z) ** 2,
        )
        return sorted_locs[:min_results]

    def get_locations_summary(self) -> str:
        """生成位置摘要文本"""
        if not self.locations:
            return "暂无已知位置。"

        lines = ["## 已知位置"]
        for loc in self.locations.values():
            contents_str = ""
            if loc.contents:
                top_items = sorted(loc.contents.items(), key=lambda x: -x[1])[:3]
                contents_str = f" (含: {', '.join(f'{k}x{v}' for k, v in top_items)})"
            lines.append(
                f"- **{loc.name}**: {loc.description} "
                f"({loc.x:.0f}, {loc.y:.0f}, {loc.z:.0f}){contents_str}"
            )
        return "\n".join(lines)

    # ── 玩家档案 ─────────────────────────────────────────────────────

    async def record_interaction(self, player_name: str, note: str = ""):
        """记录与玩家的互动"""
        key = player_name.lower()
        if key not in self.players:
            self.players[key] = PlayerProfile(name=player_name)

        profile = self.players[key]
        profile.interaction_count += 1
        profile.last_interaction = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        if note:
            profile.notes.append(note)
            if len(profile.notes) > 50:
                profile.notes = profile.notes[-50:]

        await self._maybe_auto_save()

    async def set_player_preference(self, player_name: str,
                                    key: str, value: str):
        """设置玩家偏好"""
        pkey = player_name.lower()
        if pkey not in self.players:
            self.players[pkey] = PlayerProfile(name=player_name)
        self.players[pkey].preferences[key] = value
        await self._maybe_auto_save()

    def get_player_preference(self, player_name: str,
                               key: str, default: str = "") -> str:
        """获取玩家偏好"""
        profile = self.players.get(player_name.lower())
        if profile:
            return profile.preferences.get(key, default)
        return default

    def get_player_profile(self, player_name: str) -> Optional[PlayerProfile]:
        """获取玩家完整档案"""
        return self.players.get(player_name.lower())

    def get_players_summary(self) -> str:
        """生成玩家摘要文本"""
        if not self.players:
            return "暂无玩家档案。"

        lines = ["## 玩家档案"]
        for profile in self.players.values():
            prefs = ", ".join(f"{k}={v}" for k, v in profile.preferences.items())
            lines.append(
                f"- **{profile.name}**: 互动 {profile.interaction_count} 次"
                + (f", 偏好: {prefs}" if prefs else "")
            )
        return "\n".join(lines)

    # ── 综合记忆上下文 ───────────────────────────────────────────────

    def to_system_prompt_context(self) -> str:
        """
        生成综合记忆上下文 (注入到 Agent 系统提示词)

        包含: 最近事件 + 已知位置 + 玩家档案
        """
        parts = []

        # 最近事件
        events_summary = self.get_timeline_summary(10)
        if events_summary:
            parts.append(events_summary)

        # 已知位置
        locations_summary = self.get_locations_summary()
        if locations_summary:
            parts.append(locations_summary)

        # 玩家档案
        players_summary = self.get_players_summary()
        if players_summary:
            parts.append(players_summary)

        return "\n\n".join(parts) if parts else ""

    # ── 查询 ──────────────────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def event_count(self) -> int:
        return len(self.timeline)

    @property
    def location_count(self) -> int:
        return len(self.locations)

    @property
    def player_count(self) -> int:
        return len(self.players)
