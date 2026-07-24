"""
JSON Schema 工具注册表 — Phase 3 核心组件

替代原有的 LangChain @tool 装饰器模式，使用 OpenAI 原生 function calling 协议。
每个工具都有一份中文描述和 JSON Schema 参数定义。

设计：
- 工具注册表负责：定义 → 执行 → 结果返回
- 与 OpenAI tool calling 协议完全兼容
- 支持工具分组 (分类管理)
- 内置 Minecraft 常用工具的 JSON Schema 定义

用法：
    registry = ToolRegistry()

    @registry.register(
        name="moveTo",
        description_zh="移动到指定坐标",
        parameters=[
            ToolParameter("x", "integer", "目标 X 坐标"),
            ToolParameter("y", "integer", "目标 Y 坐标"),
            ToolParameter("z", "integer", "目标 Z 坐标"),
        ],
    )
    def move_to(x: int, y: int, z: int) -> dict:
        return {"status": True, "message": f"已到达 ({x}, {y}, {z})"}

    # 获取 OpenAI 格式的工具列表
    tools = registry.get_openai_tools()

    # 执行工具调用
    result = registry.execute("moveTo", {"x": 10, "y": 64, "z": 20})
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any, Callable, Optional

from src.llm.base import ToolDefinition, ToolParameter

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 工具注册表
# ═══════════════════════════════════════════════════════════════════════════════

class ToolRegistry:
    """
    工具注册表 — 管理 Minecraft Agent 的所有可用工具

    每个工具包含:
        - name: 英文函数名 (用于 OpenAI function calling)
        - description_zh: 中文描述 (LLM 用于理解工具用途)
        - parameters: JSON Schema 参数定义
        - handler: 实际执行函数
        - category: 工具分类 (movement, block, inventory, entity, world, system)
    """

    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}

    # ── 注册 ──────────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        description_zh: str,
        parameters: Optional[list[ToolParameter]] = None,
        category: str = "general",
        handler: Optional[Callable[..., Any]] = None,
        enabled: bool = True,
    ):
        """
        注册一个工具

        可通过装饰器或直接调用使用。

        装饰器用法:
            @registry.register(name="mineBlock", description_zh="挖掘方块")
            def mine_block(x, y, z):
                ...

        直接调用:
            registry.register(
                name="mineBlock",
                description_zh="挖掘方块",
                parameters=[...],
                handler=my_func,
            )
        """
        if handler is not None:
            # 直接调用模式
            self._tools[name] = ToolEntry(
                definition=ToolDefinition(
                    name=name,
                    description=description_zh,
                    parameters=parameters or [],
                ),
                handler=handler,
                category=category,
                enabled=enabled,
            )
            logger.debug(f"工具已注册: {name} ({category})")
            return handler

        # 装饰器模式
        def decorator(func: Callable[..., Any]):
            # 从函数签名自动推导参数
            auto_params = parameters
            if auto_params is None:
                auto_params = self._infer_parameters(func)

            self._tools[name] = ToolEntry(
                definition=ToolDefinition(
                    name=name,
                    description=description_zh,
                    parameters=auto_params,
                ),
                handler=func,
                category=category,
                enabled=enabled,
            )
            logger.debug(f"工具已注册 (装饰器): {name} ({category})")
            return func

        return decorator

    def unregister(self, name: str):
        """移除工具"""
        self._tools.pop(name, None)

    def set_enabled(self, name: str, enabled: bool):
        """启用/禁用工具"""
        if name in self._tools:
            self._tools[name].enabled = enabled

    # ── 查询 ──────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional["ToolEntry"]:
        """获取工具条目"""
        return self._tools.get(name)

    def get_openai_tools(self, categories: Optional[list[str]] = None) -> list[ToolDefinition]:
        """
        获取 OpenAI 格式的工具列表

        Args:
            categories: 工具分类过滤 (None = 所有已启用的工具)

        Returns:
            ToolDefinition 列表 (可直接用于 OpenAI API tools 参数)
        """
        tools = []
        for entry in self._tools.values():
            if not entry.enabled:
                continue
            if categories and entry.category not in categories:
                continue
            tools.append(entry.definition)
        return tools

    def get_tools_by_category(self) -> dict[str, list[str]]:
        """按分类获取工具名称"""
        result: dict[str, list[str]] = {}
        for name, entry in self._tools.items():
            if entry.category not in result:
                result[entry.category] = []
            result[entry.category].append(name)
        return result

    def list_tools(self) -> list[str]:
        """列出所有工具名称"""
        return list(self._tools.keys())

    # ── 执行 ──────────────────────────────────────────────────────────

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """
        执行一个工具调用

        Args:
            name: 工具名称
            arguments: 工具参数 (来自 LLM tool_call)

        Returns:
            工具执行结果 (dict, 包含 status 和 message)
        """
        entry = self._tools.get(name)
        if entry is None:
            return {
                "status": False,
                "message": f"未知工具: {name}。可用工具: {', '.join(self.list_tools())}",
            }

        if not entry.enabled:
            return {
                "status": False,
                "message": f"工具 {name} 已被禁用",
            }

        try:
            result = entry.handler(**arguments)

            # 确保返回 dict 格式
            if not isinstance(result, dict):
                result = {"status": True, "message": str(result), "data": result}
            elif "status" not in result:
                result["status"] = True

            return result

        except TypeError as e:
            logger.error(f"工具 {name} 参数错误: {e}")
            return {
                "status": False,
                "message": f"工具 {name} 参数错误: {e}。期望参数: {[p.name for p in entry.definition.parameters]}",
            }
        except Exception as e:
            logger.error(f"工具 {name} 执行失败: {e}")
            return {
                "status": False,
                "message": f"工具 {name} 执行失败: {e}",
            }

    def to_tool_descriptions_text(self, lang: str = "zh") -> str:
        """
        生成工具描述的纯文本 (用于系统提示词)

        适用于不支持原生 function calling 的模型。
        """
        lines = []
        for name, entry in self._tools.items():
            if not entry.enabled:
                continue
            params = entry.definition.parameters
            param_str = ", ".join(
                f"{p.name}: {p.type}" + ("?" if not p.required else "")
                for p in params
            )
            if lang == "zh":
                lines.append(f"- {name}({param_str}): {entry.definition.description}")
            else:
                lines.append(f"- {name}({param_str}): {entry.definition.description}")
        return "\n".join(lines)

    # ── 内部方法 ──────────────────────────────────────────────────────

    @staticmethod
    def _infer_parameters(func: Callable) -> list[ToolParameter]:
        """
        从 Python 函数签名自动推导工具参数

        使用类型注解和 docstring 来生成 ToolParameter。
        """
        params = []
        sig = inspect.signature(func)

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls"):
                continue

            # 类型映射
            type_map = {
                int: "integer",
                float: "number",
                str: "string",
                bool: "boolean",
                list: "array",
                dict: "object",
            }
            param_type = "string"
            if param.annotation != inspect.Parameter.empty:
                py_type = param.annotation
                param_type = type_map.get(py_type, "string")

            # 是否必填
            required = param.default == inspect.Parameter.empty

            # 描述 (从 docstring 提取会很复杂，使用默认值)
            description = f"{param_name} 参数"

            params.append(ToolParameter(
                name=param_name,
                type=param_type,
                description=description,
                required=required,
            ))

        return params


# ═══════════════════════════════════════════════════════════════════════════════
# 工具条目
# ═══════════════════════════════════════════════════════════════════════════════

class ToolEntry:
    """工具条目 — 包含定义和执行函数"""

    def __init__(
        self,
        definition: ToolDefinition,
        handler: Callable[..., Any],
        category: str = "general",
        enabled: bool = True,
    ):
        self.definition = definition
        self.handler = handler
        self.category = category
        self.enabled = enabled

    def __repr__(self):
        return f"ToolEntry({self.definition.name}, {self.category}, enabled={self.enabled})"


# ═══════════════════════════════════════════════════════════════════════════════
# Minecraft 工具预定义 — 基础集合
# ═══════════════════════════════════════════════════════════════════════════════

MINECRAFT_TOOL_DEFINITIONS: list[ToolDefinition] = [
    # ── 移动类 ──
    ToolDefinition(
        name="moveTo",
        description="移动到指定坐标。Bot 会寻路到达目标位置。",
        parameters=[
            ToolParameter("x", "integer", "目标 X 坐标"),
            ToolParameter("y", "integer", "目标 Y 坐标"),
            ToolParameter("z", "integer", "目标 Z 坐标"),
        ],
    ),

    # ── 方块操作类 ──
    ToolDefinition(
        name="mineBlock",
        description="挖掘指定位置的方块",
        parameters=[
            ToolParameter("x", "integer", "方块 X 坐标"),
            ToolParameter("y", "integer", "方块 Y 坐标"),
            ToolParameter("z", "integer", "方块 Z 坐标"),
        ],
    ),
    ToolDefinition(
        name="placeBlock",
        description="在指定位置放置方块。需要手持对应方块。",
        parameters=[
            ToolParameter("x", "integer", "放置位置 X 坐标"),
            ToolParameter("y", "integer", "放置位置 Y 坐标"),
            ToolParameter("z", "integer", "放置位置 Z 坐标"),
            ToolParameter("block_name", "string", "方块名称 (如 dirt, stone, oak_planks)"),
            ToolParameter("facing", "string", "放置朝向: N/S/E/W/up/down", required=False),
        ],
    ),

    # ── 物品/库存类 ──
    ToolDefinition(
        name="getInventory",
        description="查看自己的库存。返回物品列表及数量。",
        parameters=[],
    ),
    ToolDefinition(
        name="equipItem",
        description="将物品装备到指定槽位",
        parameters=[
            ToolParameter("item_name", "string", "物品名称"),
            ToolParameter("slot", "string", "目标槽位: hand/head/torso/legs/feet/off-hand"),
        ],
    ),
    ToolDefinition(
        name="craftItem",
        description="使用合成台合成物品",
        parameters=[
            ToolParameter("item_name", "string", "要合成的物品名称"),
            ToolParameter("count", "integer", "合成数量", required=False),
        ],
    ),
    ToolDefinition(
        name="smeltItem",
        description="使用熔炉烧炼物品",
        parameters=[
            ToolParameter("input_item", "string", "输入物品名称"),
            ToolParameter("fuel_item", "string", "燃料物品名称", required=False),
            ToolParameter("count", "integer", "烧炼数量", required=False),
        ],
    ),

    # ── 实体/战斗类 ──
    ToolDefinition(
        name="attackEntity",
        description="攻击指定实体 (敌对生物)",
        parameters=[
            ToolParameter("entity_name", "string", "实体名称 (如 zombie, skeleton, creeper)"),
        ],
    ),
    ToolDefinition(
        name="scanNearbyEntities",
        description="扫描附近的实体 (玩家、生物、物品等)",
        parameters=[
            ToolParameter("radius", "integer", "扫描半径 (格)", required=False),
            ToolParameter("entity_type", "string", "实体类型过滤 (如 player, mob, item)", required=False),
        ],
    ),

    # ── 世界信息类 ──
    ToolDefinition(
        name="scanNearbyBlocks",
        description="扫描附近的方块",
        parameters=[
            ToolParameter("radius", "integer", "扫描半径 (格)", required=False),
            ToolParameter("block_name", "string", "要查找的方块名称 (如 chest, crafting_table, furnace)", required=False),
        ],
    ),
    ToolDefinition(
        name="getWorldInfo",
        description="获取当前世界信息: 时间、天气、位置、生命值、食物值等",
        parameters=[],
    ),
    ToolDefinition(
        name="findBlock",
        description="搜索指定方块在周围的位置",
        parameters=[
            ToolParameter("block_name", "string", "方块名称"),
            ToolParameter("max_distance", "integer", "最大搜索距离 (格)", required=False),
        ],
    ),

    # ── 交互类 ──
    ToolDefinition(
        name="openChest",
        description="打开指定位置的箱子并查看/取出物品",
        parameters=[
            ToolParameter("x", "integer", "箱子 X 坐标"),
            ToolParameter("y", "integer", "箱子 Y 坐标"),
            ToolParameter("z", "integer", "箱子 Z 坐标"),
        ],
    ),
    ToolDefinition(
        name="interactBlock",
        description="与方块交互 (打开合成台、熔炉、门等)",
        parameters=[
            ToolParameter("x", "integer", "方块 X 坐标"),
            ToolParameter("y", "integer", "方块 Y 坐标"),
            ToolParameter("z", "integer", "方块 Z 坐标"),
        ],
    ),

    # ── 聊天类 ──
    ToolDefinition(
        name="sendChat",
        description="在 Minecraft 聊天中发送消息",
        parameters=[
            ToolParameter("message", "string", "要发送的消息内容"),
        ],
    ),

    # ── 系统类 ──
    ToolDefinition(
        name="finalAnswer",
        description="任务完成后的最终回复。在完成任务或遇到无法解决的困难时调用此工具。",
        parameters=[
            ToolParameter("summary", "string", "任务执行摘要，描述完成了什么、遇到了什么问题"),
            ToolParameter("success", "boolean", "任务是否成功完成"),
        ],
    ),
    ToolDefinition(
        name="wait",
        description="暂停等待指定时间 (用于等待方块掉落、生物生成等)",
        parameters=[
            ToolParameter("seconds", "integer", "等待时间 (秒)"),
        ],
    ),

    # ── Phase 6: 扩展工具 (12 个) ──

    ToolDefinition(
        name="followPlayer",
        description="跟随指定玩家，保持指定距离。自动寻路到玩家位置并持续跟随。",
        parameters=[
            ToolParameter("player_name", "string", "要跟随的玩家名称"),
            ToolParameter("distance", "integer", "保持的距离 (格，默认 3)", required=False),
        ],
    ),
    ToolDefinition(
        name="guardArea",
        description="在指定区域内巡逻并自动攻击敌对生物。Agent 会在区域内来回移动，发现怪物时主动攻击。",
        parameters=[
            ToolParameter("center_x", "integer", "区域中心 X 坐标"),
            ToolParameter("center_z", "integer", "区域中心 Z 坐标"),
            ToolParameter("radius", "integer", "巡逻半径 (格)"),
            ToolParameter("duration", "integer", "巡逻时长 (秒，默认 60)", required=False),
        ],
    ),
    ToolDefinition(
        name="sortInventory",
        description="按指定方式整理库存。可以按物品名称、类型或数量排序。",
        parameters=[
            ToolParameter("sort_by", "string", "排序方式: name/type/count", enum=["name", "type", "count"]),
        ],
    ),
    ToolDefinition(
        name="autoFish",
        description="自动钓鱼。持续钓鱼直到库存满、指定时间到或收到停止指令。",
        parameters=[
            ToolParameter("max_duration", "integer", "最大钓鱼时长 (秒, 默认 120)", required=False),
        ],
    ),
    ToolDefinition(
        name="buildShape",
        description="构建几何形状 (圆形、正方形、直线、平台)。按指定材料和尺寸建造。",
        parameters=[
            ToolParameter("shape", "string", "形状类型: circle/square/line/platform", enum=["circle", "square", "line", "platform"]),
            ToolParameter("material", "string", "建材名称 (如 stone, oak_planks)"),
            ToolParameter("size", "integer", "尺寸 (圆的半径/正方形边长/线长度/平台边长)"),
            ToolParameter("center_x", "integer", "中心/起点 X 坐标"),
            ToolParameter("center_y", "integer", "中心/起点 Y 坐标"),
            ToolParameter("center_z", "integer", "中心/起点 Z 坐标"),
            ToolParameter("hollow", "boolean", "是否中空 (仅 platform/square 支持, 默认 false)", required=False),
        ],
    ),
    ToolDefinition(
        name="copyBuild",
        description="复制现有建筑结构到新位置。先扫描源区域，再在目标位置重建。",
        parameters=[
            ToolParameter("from_x1", "integer", "源区域起点 X"),
            ToolParameter("from_y1", "integer", "源区域起点 Y"),
            ToolParameter("from_z1", "integer", "源区域起点 Z"),
            ToolParameter("from_x2", "integer", "源区域终点 X"),
            ToolParameter("from_y2", "integer", "源区域终点 Y"),
            ToolParameter("from_z2", "integer", "源区域终点 Z"),
            ToolParameter("to_x", "integer", "目标起点 X"),
            ToolParameter("to_y", "integer", "目标起点 Y"),
            ToolParameter("to_z", "integer", "目标起点 Z"),
        ],
    ),
    ToolDefinition(
        name="landscaping",
        description="地形改造: 平整/挖掘/填充指定区域。用于大范围地形操作。",
        parameters=[
            ToolParameter("operation", "string", "操作类型: flatten/dig/fill", enum=["flatten", "dig", "fill"]),
            ToolParameter("x1", "integer", "区域起点 X"),
            ToolParameter("z1", "integer", "区域起点 Z"),
            ToolParameter("x2", "integer", "区域终点 X"),
            ToolParameter("z2", "integer", "区域终点 Z"),
            ToolParameter("material", "string", "填充材料 (仅 fill 操作需要)", required=False),
            ToolParameter("target_y", "integer", "目标高度 (flatten 操作需要)", required=False),
        ],
    ),
    ToolDefinition(
        name="pathBuild",
        description="在两点之间建造路径。自动生成直线或 L 形路径。",
        parameters=[
            ToolParameter("from_x", "integer", "起点 X"),
            ToolParameter("from_y", "integer", "起点 Y"),
            ToolParameter("from_z", "integer", "起点 Z"),
            ToolParameter("to_x", "integer", "终点 X"),
            ToolParameter("to_y", "integer", "终点 Y"),
            ToolParameter("to_z", "integer", "终点 Z"),
            ToolParameter("material", "string", "路面材料 (如 cobblestone, stone_bricks)"),
            ToolParameter("width", "integer", "路面宽度 (默认 1)", required=False),
        ],
    ),
    ToolDefinition(
        name="takeScreenshot",
        description="从 Agent 视角截图。保存当前视角的画面。",
        parameters=[],
    ),
    ToolDefinition(
        name="checkWeather",
        description="检查当前天气和时间。返回是否下雨/雷暴/晴天及游戏时间。",
        parameters=[],
    ),
    ToolDefinition(
        name="countNearby",
        description="统计附近指定类型的实体或方块数量。用于快速了解周围环境。",
        parameters=[
            ToolParameter("target_type", "string", "统计目标类型: entity/block"),
            ToolParameter("target_name", "string", "目标名称 (如 zombie, chest, coal_ore, 留空统计全部)", required=False),
            ToolParameter("radius", "integer", "统计半径 (格, 默认 32)", required=False),
        ],
    ),
    ToolDefinition(
        name="escort",
        description="护送玩家安全到达指定位置。沿途保护玩家免受怪物攻击。",
        parameters=[
            ToolParameter("player_name", "string", "要护送的玩家名称"),
            ToolParameter("dest_x", "integer", "目的地 X"),
            ToolParameter("dest_y", "integer", "目的地 Y"),
            ToolParameter("dest_z", "integer", "目的地 Z"),
            ToolParameter("combat_mode", "boolean", "是否主动攻击沿途怪物 (默认 true)", required=False),
        ],
    ),
]
