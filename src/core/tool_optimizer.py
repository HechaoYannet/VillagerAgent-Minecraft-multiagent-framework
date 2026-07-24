"""
工具优化器 — Phase 6 工具调用优化

提供 Agent 工具调用的智能优化:
1. 批量操作: "挖掘所有范围内的 coal_ore" → 多个 mineBlock 调用
2. 合成链: "制作石镐" → 获取圆石→获取木棍→合成
3. 工具提示: 告诉 LLM 挖什么方块用什么工具
4. 路径缓存: 常用路线记忆

用法:
    optimizer = ToolOptimizer(bridge=bridge, memory=long_term_memory)
    expanded = await optimizer.expand_batch("mineBlock", {"block_name": "coal_ore", "radius": 10})
    # → [("mineBlock", {"x":100,"y":60,"z":200}), ("mineBlock", {"x":101,...}), ...]
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Minecraft 知识库: 工具↔方块映射
# ═══════════════════════════════════════════════════════════════════════════════

# 哪种工具最适合挖掘哪种方块
BEST_TOOL_FOR_BLOCK: dict[str, str] = {
    # 石头类 → 镐
    "stone": "pickaxe", "cobblestone": "pickaxe", "granite": "pickaxe",
    "diorite": "pickaxe", "andesite": "pickaxe", "deepslate": "pickaxe",
    "coal_ore": "pickaxe", "iron_ore": "pickaxe", "gold_ore": "pickaxe",
    "diamond_ore": "pickaxe", "emerald_ore": "pickaxe", "copper_ore": "pickaxe",
    "redstone_ore": "pickaxe", "lapis_ore": "pickaxe",
    "netherrack": "pickaxe", "end_stone": "pickaxe", "obsidian": "diamond_pickaxe",
    # 木头类 → 斧
    "oak_log": "axe", "spruce_log": "axe", "birch_log": "axe",
    "jungle_log": "axe", "acacia_log": "axe", "dark_oak_log": "axe",
    "oak_planks": "axe", "crafting_table": "axe", "chest": "axe",
    # 泥土类 → 铲
    "dirt": "shovel", "grass_block": "shovel", "sand": "shovel",
    "gravel": "shovel", "clay": "shovel", "soul_sand": "shovel",
    # 其他
    "wool": "shears", "leaves": "shears", "cobweb": "sword",
    "snow": "shovel", "ice": "pickaxe",
}

# 工具材料等级 (数字越大越好)
TOOL_TIER: dict[str, int] = {
    "wooden": 0, "stone": 1, "iron": 2, "diamond": 3, "netherite": 4, "golden": 0,
}

# 简单合成配方链 (Phase 6: 程序化合成路径)
# 格式: target → [(step_name, [(ingredient, count)])]
CRAFTING_CHAINS: dict[str, list] = {
    "stone_pickaxe": [
        ("craft_stick", [("oak_planks", 2)]),
        ("craft_stone_pickaxe", [("cobblestone", 3), ("stick", 2)]),
    ],
    "iron_pickaxe": [
        ("craft_stick", [("oak_planks", 2)]),
        ("craft_iron_pickaxe", [("iron_ingot", 3), ("stick", 2)]),
    ],
    "diamond_pickaxe": [
        ("craft_stick", [("oak_planks", 2)]),
        ("craft_diamond_pickaxe", [("diamond", 3), ("stick", 2)]),
    ],
    "stone_axe": [
        ("craft_stick", [("oak_planks", 2)]),
        ("craft_stone_axe", [("cobblestone", 3), ("stick", 2)]),
    ],
    "stone_shovel": [
        ("craft_stick", [("oak_planks", 2)]),
        ("craft_stone_shovel", [("cobblestone", 1), ("stick", 2)]),
    ],
    "stone_sword": [
        ("craft_stick", [("oak_planks", 1)]),
        ("craft_stone_sword", [("cobblestone", 2), ("stick", 1)]),
    ],
    "iron_sword": [
        ("craft_stick", [("oak_planks", 1)]),
        ("craft_iron_sword", [("iron_ingot", 2), ("stick", 1)]),
    ],
    "torch": [
        ("craft_torch", [("coal", 1), ("stick", 1)]),
    ],
    "furnace": [
        ("craft_furnace", [("cobblestone", 8)]),
    ],
    "chest": [
        ("craft_chest", [("oak_planks", 8)]),
    ],
    "crafting_table": [
        ("craft_crafting_table", [("oak_planks", 4)]),
    ],
    "iron_ingot": [
        ("smelt_iron_ore", [("iron_ore", 1), ("coal", 1)]),
    ],
    "gold_ingot": [
        ("smelt_gold_ore", [("gold_ore", 1), ("coal", 1)]),
    ],
}


# ═══════════════════════════════════════════════════════════════════════════════

class ToolOptimizer:
    """
    工具调用优化器

    功能:
        - expand_batch(): 将批量指令展开为多次单独调用
        - resolve_crafting_chain(): 解析合成配方链
        - suggest_tool(): 推荐挖掘工具
        - get_batch_operations(): 判断哪些工具支持批量操作
    """

    def __init__(
        self,
        bridge=None,  # MinecraftBridge (用于扫描)
        memory=None,  # LongTermMemory (用于位置查询)
        recipes_file: str = "data/recipes.json",
    ):
        self.bridge = bridge
        self.memory = memory
        self._recipes = self._load_recipes(recipes_file)
        self._path_cache: dict[tuple, list] = {}  # (from, to) → [waypoints]

    def _load_recipes(self, path: str) -> dict:
        """加载 Minecraft 配方数据"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                recipes_list = json.load(f)
            # 转为 {result_name: [recipe, ...]} 方便查找
            recipes = {}
            for r in recipes_list:
                result_name = r.get("result", {}).get("name", "")
                if result_name:
                    if result_name not in recipes:
                        recipes[result_name] = []
                    recipes[result_name].append(r)
            logger.debug(f"加载了 {len(recipes)} 种物品的配方")
            return recipes
        except Exception as e:
            logger.warning(f"配方加载失败: {e}")
            return {}

    # ── 批量操作展开 ────────────────────────────────────────────────

    async def expand_batch(
        self,
        tool_name: str,
        args: dict,
    ) -> list[tuple[str, dict]]:
        """
        将批量指令展开为多次单独工具调用

        支持的批量操作:
            - mineBlock: block_name + radius → 多次 mineBlock
            - placeBlock: block_name + count → 多次 placeBlock
            - countNearby: 直接执行, 不展开

        Returns:
            [(tool_name, args), ...] — 展开后的调用列表
        """
        if tool_name == "mineBlock" and "block_name" in args and "radius" in args:
            return await self._expand_mine_batch(args)

        if tool_name == "placeBlock" and "count" in args:
            return await self._expand_place_batch(args)

        # 非批量: 直接返回
        return [(tool_name, args)]

    async def _expand_mine_batch(self, args: dict) -> list[tuple[str, dict]]:
        """展开批量挖掘"""
        block_name = args.get("block_name", "")
        radius = args.get("radius", 8)

        # 扫描附近方块
        nearby = []
        if self.bridge:
            nearby = await self.bridge.scan_nearby_blocks(
                radius=radius, block_name=block_name,
            )

        if not nearby:
            # 无法扫描 → 返回单个操作让 Agent 自行处理
            return [("mineBlock", {"x": args.get("x", 0), "y": args.get("y", 64), "z": args.get("z", 0)})]

        # 展开为每个方块一次 mineBlock
        return [
            ("mineBlock", {
                "x": b.get("position", [0, 0, 0])[0],
                "y": b.get("position", [0, 64, 0])[1],
                "z": b.get("position", [0, 0, 0])[2],
            })
            for b in nearby
        ]

    async def _expand_place_batch(self, args: dict) -> list[tuple[str, dict]]:
        """展开批量放置"""
        count = args.pop("count", 1)
        base_args = args.copy()
        return [("placeBlock", base_args) for _ in range(min(count, 64))]

    # ── 合成链解析 ──────────────────────────────────────────────────

    def resolve_crafting_chain(self, target_item: str) -> Optional[list[dict]]:
        """
        解析合成配方链

        "stone_pickaxe" → [
            {"tool": "craftItem", "args": {"item_name": "stick", "count": 4}},
            {"tool": "craftItem", "args": {"item_name": "stone_pickaxe", "count": 1}},
        ]

        Returns:
            步骤列表 (None = 未找到配方)
        """
        # 1. 检查预定义合成链
        if target_item in CRAFTING_CHAINS:
            chain = CRAFTING_CHAINS[target_item]
            steps = []
            for step_name, ingredients in chain:
                for ing_name, ing_count in ingredients:
                    # 可能需要先合成原料
                    sub_chain = self.resolve_crafting_chain(ing_name)
                    if sub_chain:
                        steps.extend(sub_chain)
                        break  # 只展开第一个匹配的子链
                steps.append({
                    "tool": "craftItem",
                    "args": {"item_name": step_name.replace("craft_", "").replace("smelt_", ""),
                             "count": 1},
                })
            return steps

        # 2. 检查 recipes.json
        if target_item in self._recipes:
            recipe = self._recipes[target_item][0]  # 取第一个可用配方
            ingredients = [
                f"{ing['name']} x{ing['count']}"
                for ing in recipe.get("ingredients", [])
            ]
            return [{
                "tool": "craftItem",
                "args": {"item_name": target_item, "count": 1},
                "ingredients": ingredients,
            }]

        return None

    def get_required_resources(self, target_item: str) -> dict[str, int]:
        """
        计算合成所需的总原料

        "stone_pickaxe" → {"oak_planks": 2, "cobblestone": 3}
        """
        resources: dict[str, int] = {}

        if target_item in CRAFTING_CHAINS:
            for _, ingredients in CRAFTING_CHAINS[target_item]:
                for name, count in ingredients:
                    # 递归获取子原料
                    sub_resources = self.get_required_resources(name)
                    if sub_resources:
                        for sub_name, sub_count in sub_resources.items():
                            resources[sub_name] = resources.get(sub_name, 0) + sub_count * count
                    else:
                        resources[name] = resources.get(name, 0) + count
            return resources

        return resources

    # ── 工具推荐 ──────────────────────────────────────────────────────

    def suggest_tool(self, block_name: str) -> Optional[str]:
        """为指定方块推荐最佳挖掘工具"""
        return BEST_TOOL_FOR_BLOCK.get(block_name)

    def suggest_tool_tier(self, block_name: str) -> int:
        """推荐所需的工具等级 (0-4)"""
        tool = self.suggest_tool(block_name)
        if not tool:
            return 0
        if "diamond" in tool:
            return 3
        if "iron" in tool:
            return 2
        if "stone" in tool:
            return 1
        return 0

    def get_tool_hint_text(self, block_name: str) -> str:
        """生成工具提示文本 (注入系统提示词)"""
        tool = self.suggest_tool(block_name)
        if not tool:
            return f"挖掘 {block_name} 不需要特殊工具。"
        tier = self.suggest_tool_tier(block_name)
        tier_names = ["木质", "石质", "铁质", "钻石", "下界合金"]
        return f"挖掘 {block_name} 建议使用 {tier_names[tier]}{tool}。"

    def get_batch_tool_hints(self) -> str:
        """生成所有工具提示 (注入系统提示词)"""
        lines = ["## 工具推荐"]
        seen = set()
        for block, tool in sorted(BEST_TOOL_FOR_BLOCK.items()):
            if tool not in seen:
                seen.add(tool)
                blocks = [b for b, t in BEST_TOOL_FOR_BLOCK.items() if t == tool]
                sample = ", ".join(blocks[:5])
                lines.append(f"- **{tool}**: {sample}" + (f" 等 {len(blocks)} 种方块" if len(blocks) > 5 else ""))
        return "\n".join(lines)

    # ── 批量操作检测 ─────────────────────────────────────────────────

    BATCHABLE_TOOLS = {
        "mineBlock": {
            "pattern": "block_name + radius",
            "description": "指定方块名和半径 → 自动展开为范围内所有该方块的挖掘",
        },
        "countNearby": {
            "pattern": "自动执行",
            "description": "统计附近实体/方块数量",
        },
        "landscaping": {
            "pattern": "自动执行",
            "description": "大范围地形改造 (自动分区执行)",
        },
        "sortInventory": {
            "pattern": "自动执行",
            "description": "一次性整理整个库存",
        },
    }

    def is_batchable(self, tool_name: str) -> bool:
        return tool_name in self.BATCHABLE_TOOLS

    def get_batch_description(self, tool_name: str) -> str:
        return self.BATCHABLE_TOOLS.get(tool_name, {}).get("description", "")

    def get_batch_operations_summary(self) -> str:
        """生成批量操作摘要 (注入系统提示词)"""
        lines = ["## 批量操作支持", "以下工具支持批量操作 (一次调用完成多个动作):"]
        for tool_name, info in self.BATCHABLE_TOOLS.items():
            lines.append(f"- **{tool_name}**: {info['description']}")
        return "\n".join(lines)

    # ── 路径缓存 ──────────────────────────────────────────────────────

    def cache_path(self, from_pos: tuple, to_pos: tuple, waypoints: list[tuple]):
        """缓存成功路径"""
        key = (from_pos, to_pos)
        self._path_cache[key] = waypoints
        if len(self._path_cache) > 100:
            # LRU: 删除最早的一项
            oldest = next(iter(self._path_cache))
            del self._path_cache[oldest]

    def get_cached_path(self, from_pos: tuple, to_pos: tuple) -> Optional[list[tuple]]:
        """查找缓存路径 (近似匹配, 32 格容差)"""
        # 精确匹配
        if (from_pos, to_pos) in self._path_cache:
            return self._path_cache[(from_pos, to_pos)]

        # 近似匹配 (附近 32 格内)
        for (f, t), waypoints in self._path_cache.items():
            if (abs(f[0] - from_pos[0]) < 32 and
                abs(f[1] - from_pos[1]) < 32 and
                abs(f[2] - from_pos[2]) < 32 and
                abs(t[0] - to_pos[0]) < 32 and
                abs(t[1] - to_pos[1]) < 32 and
                abs(t[2] - to_pos[2]) < 32):
                return waypoints

        return None
