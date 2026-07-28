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
# Minecraft 工具预定义 — 18 个, 与 env/minecraft_server.py /api/action 路由一一对应
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
        name="getWorldInfo",
        description="获取当前世界信息: 时间、天气、位置、生命值、食物值、附近方块列表等",
        parameters=[],
    ),
    ToolDefinition(
        name="findBlock",
        description="搜索指定方块在周围的位置，返回每个方块的距离和是否可到达。自动从小范围开始渐进搜索，最远1000格。",
        parameters=[
            ToolParameter("block_name", "string", "方块名称"),
            ToolParameter("count", "integer", "最多返回数量 (默认 5)", required=False),
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

    # ── 扩展工具 (有 Flask 桥接实现) ──

    ToolDefinition(
        name="followPlayer",
        description="跟随指定玩家，保持指定距离。自动寻路到玩家位置并持续跟随。",
        parameters=[
            ToolParameter("player_name", "string", "要跟随的玩家名称"),
            ToolParameter("distance", "integer", "保持的距离 (格，默认 3)", required=False),
        ],
    ),
]
