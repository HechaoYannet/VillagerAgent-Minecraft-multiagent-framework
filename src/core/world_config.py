"""
世界配置文件管理 — Phase 4 "Minecraft 版 CLAUDE.md"

每个 Minecraft 世界一个 Markdown 文件 (data/world/{world_name}.md)。
Agent 启动时读取并注入系统提示词，运行时可以请求更新。

文件格式:
    # 世界：{world_name}

    ## 世界信息
    - 种子：12345
    - 游戏模式：生存
    - 玩家：Steve, Alex

    ## 重要位置
    - 主基地：x=100, y=64, z=200
    - 矿井：x=150, y=64, z=180

    ## 规则与偏好
    - 不要移动自动分拣机中的物品
    - 优先使用精准采集镐

    ## 历史事件
    - 2026-07-20：击败末影龙
    - 2026-07-24：建造了守卫者农场

用法:
    config = WorldConfig("my_world")
    await config.load()
    context = config.to_system_prompt()
    await config.add_event("在 x=300, z=400 建造了新农场")
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class WorldInfo:
    """世界基本信息"""
    name: str = ""
    seed: str = ""
    game_mode: str = "survival"
    difficulty: str = "normal"
    players: list[str] = field(default_factory=list)
    dimension: str = "overworld"


@dataclass
class LocationEntry:
    """位置条目"""
    name: str
    description: str
    x: float = 0
    y: float = 64
    z: float = 0
    category: str = "general"  # base, mine, farm, portal, storage, other
    created_at: str = ""

    def to_markdown(self) -> str:
        return f"- **{self.name}**: {self.description} (x={self.x:.0f}, y={self.y:.0f}, z={self.z:.0f})"

    @classmethod
    def from_dict(cls, data: dict) -> "LocationEntry":
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            x=data.get("x", 0),
            y=data.get("y", 64),
            z=data.get("z", 0),
            category=data.get("category", "general"),
            created_at=data.get("created_at", ""),
        )


@dataclass
class EventEntry:
    """历史事件条目"""
    timestamp: str  # ISO 格式
    description: str
    category: str = "general"  # milestone, building, combat, discovery, player, agent

    def to_markdown(self) -> str:
        return f"- {self.timestamp[:10]} [{self.category}]: {self.description}"


# ═══════════════════════════════════════════════════════════════════════════════

class WorldConfig:
    """
    世界配置文件管理器

    读取/解析/写入 Markdown 格式的世界配置。
    支持分区解析：世界信息 / 重要位置 / 规则偏好 / 历史事件。

    特性:
        - Markdown ↔ 结构化数据 双向转换
        - 自动备份 (写入前保存 .bak)
        - 位置去重 (按名称)
        - 事件自动截断 (最近 200 条)
    """

    def __init__(
        self,
        world_name: str = "default",
        config_dir: str = "data/world",
    ):
        self.world_name = world_name
        self.config_dir = config_dir
        self.file_path = os.path.join(config_dir, f"{world_name}.md")

        # 结构化数据
        self.world_info = WorldInfo(name=world_name)
        self.locations: list[LocationEntry] = []
        self.rules: list[str] = []
        self.preferences: list[str] = []
        self.events: list[EventEntry] = []

        self._loaded = False

    # ── 文件 I/O ──────────────────────────────────────────────────────

    async def load(self) -> bool:
        """加载世界配置文件"""
        if not os.path.exists(self.file_path):
            logger.info(f"世界配置文件不存在, 创建默认: {self.file_path}")
            os.makedirs(self.config_dir, exist_ok=True)
            await self._create_default()
            self._loaded = True
            return True

        try:
            content = await self._read_file()
            self._parse(content)
            self._loaded = True
            logger.info(f"世界配置已加载: {self.world_name} "
                        f"({len(self.locations)} 位置, {len(self.events)} 事件)")
            return True
        except Exception as e:
            logger.error(f"世界配置加载失败: {e}")
            return False

    async def save(self):
        """保存世界配置文件 (自动备份)"""
        if not self._loaded:
            return

        content = self._render()
        os.makedirs(self.config_dir, exist_ok=True)

        # 备份旧文件
        if os.path.exists(self.file_path):
            bak_path = self.file_path + ".bak"
            try:
                os.replace(self.file_path, bak_path)
            except OSError:
                pass

        await self._write_file(content)
        logger.debug(f"世界配置已保存: {self.file_path}")

    async def _read_file(self) -> str:
        """异步读取文件"""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: open(self.file_path, "r", encoding="utf-8").read()
        )

    async def _write_file(self, content: str):
        """异步写入文件"""
        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: open(self.file_path, "w", encoding="utf-8").write(content)
        )

    async def _create_default(self):
        """创建默认配置文件"""
        self.world_info = WorldInfo(name=self.world_name, game_mode="survival")
        self.locations = []
        self.rules = [
            "不要破坏玩家的建筑",
            "未经允许不要进入下界传送门",
            "收集完成后始终补充食物",
        ]
        self.preferences = [
            "优先使用精准采集镐挖掘矿石",
        ]
        self.events = [
            EventEntry(
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                description=f"世界 '{self.world_name}' 初始化",
                category="system",
            ),
        ]
        await self.save()

    # ── 解析 ──────────────────────────────────────────────────────────

    def _parse(self, content: str):
        """解析 Markdown 内容 → 结构化数据"""
        # 世界名称
        title_match = re.search(r'^# 世界[：:]\s*(.+)$', content, re.MULTILINE)
        if title_match:
            self.world_info.name = title_match.group(1).strip()

        # 世界信息
        info_section = self._extract_section(content, "世界信息", "基本信息")
        if info_section:
            self.world_info.seed = self._extract_field(info_section, "种子")
            self.world_info.game_mode = self._extract_field(info_section, "游戏模式") or "survival"
            self.world_info.difficulty = self._extract_field(info_section, "难度") or "normal"
            players_str = self._extract_field(info_section, "玩家")
            if players_str:
                self.world_info.players = [p.strip() for p in players_str.split(",")]

        # 重要位置
        locations_section = self._extract_section(
            content, "重要位置", "基地位置", "建筑"
        )
        if locations_section:
            self.locations = self._parse_locations(locations_section)

        # 规则与偏好
        rules_section = self._extract_section(content, "规则与偏好", "规则")
        if rules_section:
            for line in rules_section.split("\n"):
                line = line.strip()
                if line.startswith("- "):
                    rule = line[2:].strip()
                    if "优先" in rule or "偏好" in rule or "喜欢" in rule:
                        self.preferences.append(rule)
                    else:
                        self.rules.append(rule)

        # 历史事件
        events_section = self._extract_section(content, "历史事件", "历史", "时间线")
        if events_section:
            self.events = self._parse_events(events_section)

    def _extract_section(self, content: str, *section_names: str) -> Optional[str]:
        """提取 Markdown 章节内容"""
        for name in section_names:
            pattern = rf'##\s+{name}\s*\n(.*?)(?=\n##\s|\Z)'
            match = re.search(pattern, content, re.DOTALL)
            if match:
                return match.group(1).strip()
        return None

    def _extract_field(self, section: str, field_name: str) -> str:
        """提取字段值 (- 字段名：值 或 - 字段名: 值)"""
        pattern = rf'-\s*{field_name}[：:]\s*(.+)'
        match = re.search(pattern, section, re.MULTILINE)
        return match.group(1).strip() if match else ""

    def _parse_locations(self, section: str) -> list[LocationEntry]:
        """解析位置条目"""
        locations = []
        for line in section.split("\n"):
            line = line.strip()
            if not line.startswith("- "):
                continue

            # 格式: - **名称**: 描述 (x=..., y=..., z=...)
            bold_match = re.match(r'-\s*\*\*(.+?)\*\*:\s*(.+)', line)
            if not bold_match:
                continue

            name = bold_match.group(1).strip()
            rest = bold_match.group(2).strip()

            # 提取坐标
            coord_match = re.search(
                r'x\s*=\s*(-?[\d.]+).*?y\s*=\s*(-?[\d.]+).*?z\s*=\s*(-?[\d.]+)', rest
            )
            x = float(coord_match.group(1)) if coord_match else 0.0
            y = float(coord_match.group(2)) if coord_match else 64.0
            z = float(coord_match.group(3)) if coord_match else 0.0

            # 去除坐标部分的描述
            description = re.sub(r'\s*\(x\s*=\s*[^)]+\)', '', rest).strip()

            locations.append(LocationEntry(
                name=name,
                description=description,
                x=x, y=y, z=z,
            ))

        return locations

    def _parse_events(self, section: str) -> list[EventEntry]:
        """解析历史事件"""
        events = []
        for line in section.split("\n"):
            line = line.strip()
            if not line.startswith("- "):
                continue

            # 格式: - YYYY-MM-DD [category]: description
            match = re.match(
                r'-\s*(\d{4}-\d{2}-\d{2})\s*(?:\[(\w+)\])?:\s*(.+)', line
            )
            if match:
                events.append(EventEntry(
                    timestamp=match.group(1) + "T00:00:00",
                    category=match.group(2) or "general",
                    description=match.group(3).strip(),
                ))
            else:
                # 简单格式: - description
                text = line[2:].strip()
                if text:
                    events.append(EventEntry(
                        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                        description=text,
                        category="general",
                    ))

        return events[-200:]  # 最近 200 条

    # ── 渲染 ──────────────────────────────────────────────────────────

    def _render(self) -> str:
        """结构化数据 → Markdown"""
        lines = [
            f"# 世界：{self.world_info.name}",
            "",
            "> 此文件由 VillagerAgent 自动管理。",
            "> Agent 启动时读取，运行时自动更新。",
            "> 你也可以手动编辑——Agent 会在下次加载时识别变更。",
            "",
            "## 世界信息",
            f"- 种子：{self.world_info.seed or '未知'}",
            f"- 游戏模式：{self.world_info.game_mode}",
            f"- 难度：{self.world_info.difficulty}",
            f"- 玩家：{', '.join(self.world_info.players) if self.world_info.players else '暂无'}",
            "",
            "## 重要位置",
        ]

        if self.locations:
            for loc in self.locations:
                lines.append(loc.to_markdown())
        else:
            lines.append("- 暂无记录。Agent 会在探索过程中自动记录重要位置。")
        lines.append("")

        lines.append("## 规则与偏好")
        if self.rules or self.preferences:
            for rule in self.rules:
                lines.append(f"- {rule}")
            for pref in self.preferences:
                lines.append(f"- {pref}")
        else:
            lines.append("- 暂无特殊规则。")
        lines.append("")

        lines.append("## 历史事件")
        if self.events:
            for event in self.events[-50:]:  # 最近 50 条写入文件
                lines.append(event.to_markdown())
        else:
            lines.append("- 暂无历史记录。")
        lines.append("")

        return "\n".join(lines)

    # ── 修改 API ──────────────────────────────────────────────────────

    async def add_location(self, name: str, description: str,
                           x: float, y: float, z: float,
                           category: str = "general"):
        """添加或更新位置 (按名称去重)"""
        entry = LocationEntry(
            name=name, description=description,
            x=x, y=y, z=z, category=category,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        )

        # 去重: 同名位置覆盖
        for i, loc in enumerate(self.locations):
            if loc.name.lower() == name.lower():
                self.locations[i] = entry
                logger.info(f"位置已更新: {name}")
                await self.save()
                return

        self.locations.append(entry)
        logger.info(f"位置已添加: {name}")
        await self.save()

    async def add_event(self, description: str, category: str = "general"):
        """添加历史事件"""
        event = EventEntry(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            description=description,
            category=category,
        )
        self.events.append(event)

        # 内存中保持最近 200 条
        if len(self.events) > 200:
            self.events = self.events[-200:]

        # 每 5 条事件自动保存一次
        if len(self.events) % 5 == 0:
            await self.save()

    async def add_rule(self, rule: str):
        """添加规则"""
        if rule not in self.rules:
            self.rules.append(rule)
            await self.save()

    async def add_preference(self, preference: str):
        """添加偏好"""
        if preference not in self.preferences:
            self.preferences.append(preference)
            await self.save()

    async def update_world_info(self, **kwargs):
        """更新世界信息"""
        for key, value in kwargs.items():
            if hasattr(self.world_info, key):
                setattr(self.world_info, key, value)
        await self.save()

    # ── 系统提示词注入 ───────────────────────────────────────────────

    def to_system_prompt(self) -> str:
        """
        生成系统提示词片段 (注入到 Agent 的 system prompt)

        LLM 可以通过这段上下文了解:
        - 世界基本信息
        - 已知的重要位置
        - 玩家设定的规则和偏好
        - 最近的历史事件 (时间线)
        """
        parts = [f"## 世界知识：{self.world_info.name}"]

        # 基本信息
        info_parts = [f"游戏模式: {self.world_info.game_mode}"]
        if self.world_info.seed:
            info_parts.append(f"种子: {self.world_info.seed}")
        if self.world_info.players:
            info_parts.append(f"在线玩家: {', '.join(self.world_info.players)}")
        parts.append(" | ".join(info_parts))

        # 重要位置
        if self.locations:
            parts.append("\n### 已知位置")
            for loc in self.locations[:20]:  # 最近 20 个位置
                parts.append(f"- {loc.name}: {loc.description} "
                             f"(x={loc.x:.0f}, y={loc.y:.0f}, z={loc.z:.0f})")

        # 规则与偏好
        if self.rules:
            parts.append("\n### 规则")
            for rule in self.rules:
                parts.append(f"- {rule}")
        if self.preferences:
            parts.append("\n### 偏好")
            for pref in self.preferences:
                parts.append(f"- {pref}")

        # 最近事件 (时间线)
        if self.events:
            parts.append("\n### 最近事件")
            for event in self.events[-10:]:
                parts.append(event.to_markdown())

        return "\n".join(parts)

    # ── 查询 ──────────────────────────────────────────────────────────

    def find_location(self, name: str) -> Optional[LocationEntry]:
        """按名称查找位置"""
        for loc in self.locations:
            if name.lower() in loc.name.lower():
                return loc
        return None

    def find_nearest_location(self, x: float, y: float, z: float) -> Optional[LocationEntry]:
        """找最近的位置"""
        if not self.locations:
            return None
        return min(
            self.locations,
            key=lambda loc: (loc.x - x) ** 2 + (loc.y - y) ** 2 + (loc.z - z) ** 2,
        )

    @property
    def is_loaded(self) -> bool:
        return self._loaded
