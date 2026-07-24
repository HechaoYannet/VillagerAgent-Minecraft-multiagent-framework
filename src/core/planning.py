"""
预规划系统 — Phase 4 任务规划引擎

Agent 在行动前先进行预规划:
1. 环境扫描 → 获取当前状态 (位置/库存/附近/时间)
2. 需求分析 → 理解任务目标 + 推理前提条件
3. 计划生成 → 生成步骤列表
4. 资源检查 → 验证可用资源
5. 执行 → 逐步执行 + 动态调整

规划结果注入到 Agent 的系统提示词前缀，让 LLM 在推理时参考。

用法:
    planner = TaskPlanner(llm=client, memory=ltm, world_config=wc)
    plan = await planner.plan("制作一把钻石镐")
    # → TaskPlan(steps=["1. 检查库存中是否有钻石", "2. 如果没有,去矿井挖钻石", ...])

架构:
    TaskPlanner
    ├── plan(task_description) → TaskPlan
    ├── analyze_environment() → EnvAnalysis
    └── _build_planning_prompt() → str
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from src.core.bridge import MinecraftBridge
from src.core.conversation import ConversationMemory
from src.core.long_term_memory import LongTermMemory
from src.core.world_config import WorldConfig
from src.llm.base import SystemMessage, UserMessage

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class EnvAnalysis:
    """环境分析结果"""
    position: Optional[list[float]] = None
    health: float = 20.0
    food: float = 20.0
    time_of_day: str = "day"
    inventory_summary: str = ""
    nearby_blocks_of_interest: list[dict] = field(default_factory=list)
    nearby_entities: list[dict] = field(default_factory=list)
    known_locations_nearby: list[dict] = field(default_factory=list)
    timestamp: float = field(default_factory=time.monotonic)

    def to_text(self) -> str:
        lines = [
            "## 环境分析",
            f"- 位置: {self.position}",
            f"- 生命值: {self.health}/20, 食物: {self.food}/20",
            f"- 时间: {self.time_of_day}",
            f"- 库存: {self.inventory_summary or '未查询'}",
        ]
        if self.nearby_blocks_of_interest:
            blocks = ", ".join(
                f"{b.get('name', '?')}@{b.get('position')}"
                for b in self.nearby_blocks_of_interest[:5]
            )
            lines.append(f"- 附近方块: {blocks}")
        if self.nearby_entities:
            entities = ", ".join(
                e.get("name", "?") for e in self.nearby_entities[:5]
            )
            lines.append(f"- 附近实体: {entities}")
        if self.known_locations_nearby:
            locs = ", ".join(
                f"{l.get('name', '?')}(距离: {l.get('distance', 0):.0f}m)"
                for l in self.known_locations_nearby[:3]
            )
            lines.append(f"- 附近已知位置: {locs}")
        return "\n".join(lines)


@dataclass
class TaskPlan:
    """任务计划"""
    task_description: str
    steps: list[str] = field(default_factory=list)  # 中文步骤
    prerequisites: list[str] = field(default_factory=list)  # 前提条件
    required_resources: list[str] = field(default_factory=list)  # 所需资源
    estimated_steps: int = 0
    confidence: float = 1.0  # 0-1, 计划置信度
    fallback_plan: Optional[str] = None  # 备用计划
    reasoning: str = ""  # DeepSeek 思考过程
    timestamp: float = field(default_factory=time.monotonic)

    def to_text(self) -> str:
        """生成计划文本 (注入到系统提示词)"""
        lines = [f"## 当前计划: {self.task_description}"]

        if self.reasoning:
            lines.append(f"\n*思考过程*: {self.reasoning[:500]}")

        if self.prerequisites:
            lines.append(f"\n**前提条件**: {', '.join(self.prerequisites)}")

        if self.required_resources:
            lines.append(f"**所需资源**: {', '.join(self.required_resources)}")

        lines.append(f"\n**置信度**: {self.confidence:.0%}")

        if self.steps:
            lines.append("\n**执行步骤**:")
            for i, step in enumerate(self.steps, 1):
                lines.append(f"{i}. {step}")

        if self.fallback_plan:
            lines.append(f"\n**备用计划**: {self.fallback_plan}")

        lines.append(f"\n*预计 {self.estimated_steps} 步完成*")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# 任务规划器
# ═══════════════════════════════════════════════════════════════════════════════

# 规划专用系统提示词
PLANNING_SYSTEM_PROMPT = """你是 Minecraft 中的任务规划助手。你需要根据当前环境和目标，生成一个清晰、可执行的步骤计划。

## 规划原则
1. **由近及远**: 从当前位置开始,逐步推进
2. **先检查后行动**: 先检查库存/附近,再决定是否需要采集
3. **考虑安全**: 夜间/低生命值时优先保护自己
4. **利用记忆**: 参考已知位置和过往经验
5. **保持灵活**: 计划应留有调整空间

## 返回格式
返回 JSON:
{
    "prerequisites": ["前提条件1", "前提条件2"],
    "required_resources": ["资源1", "资源2"],
    "steps": ["步骤1", "步骤2", "步骤3"],
    "estimated_steps": 3,
    "confidence": 0.85,
    "fallback_plan": "如果失败,替代方案是..."
}

## 示例
任务: 制作一把石镐
环境: 在森林中(x=100,64,200), 库存有木棍x5, 附近有橡树和石头

{
    "prerequisites": ["需要圆石x3", "需要木棍x2", "需要工作台"],
    "required_resources": ["oak_planks", "cobblestone"],
    "steps": [
        "1. 检查库存和工作台位置",
        "2. 如果没有圆石,去附近石头处挖掘3个圆石",
        "3. 如果没有工作台,用4个木板合成一个",
        "4. 在工作台用3圆石+2木棍合成石镐"
    ],
    "estimated_steps": 4,
    "confidence": 0.95,
    "fallback_plan": "如果没有石头,用木板合成木镐代替"
}"""


class TaskPlanner:
    """
    任务预规划器

    在执行任务前, 基于环境扫描和长期记忆生成步骤计划。
    计划以结构化文本注入 LLM 系统提示词, 引导 Agent 行为。

    用法:
        planner = TaskPlanner(llm=client, memory=ltm, world_config=wc, bridge=bridge)
        plan = await planner.plan("制作钻石镐")
        plan_text = plan.to_text()  # 注入 system prompt
    """

    def __init__(
        self,
        llm: Any,  # OpenAICompatClient
        memory: LongTermMemory,
        world_config: WorldConfig,
        bridge: MinecraftBridge,
        conversation: ConversationMemory,
    ):
        self.llm = llm
        self.memory = memory
        self.world_config = world_config
        self.bridge = bridge
        self.conversation = conversation

        self._last_plan: Optional[TaskPlan] = None
        self._planning_enabled = True

    # ── 环境分析 ──────────────────────────────────────────────────────

    async def analyze_environment(self) -> EnvAnalysis:
        """
        分析当前环境状态

        从多个数据源聚合:
        - ConversationMemory 中的世界状态快照
        - LongTermMemory 中的已知位置
        - 可选的主动扫描 (附近方块/实体)
        """
        ws = self.conversation.world_state
        analysis = EnvAnalysis()

        if ws:
            analysis.position = ws.position
            analysis.health = ws.health
            analysis.food = ws.food
            analysis.time_of_day = ws.time_of_day
            analysis.inventory_summary = ws.inventory_summary

        # 查找附近已知位置
        if ws and ws.position:
            pos = ws.position
            nearby = self.memory.find_nearest_location(
                pos[0], pos[1], pos[2], min_results=5
            )
            analysis.known_locations_nearby = [
                {
                    "name": loc.name,
                    "description": loc.description,
                    "position": [loc.x, loc.y, loc.z],
                    "distance": (
                        (loc.x - pos[0]) ** 2
                        + (loc.y - pos[1]) ** 2
                        + (loc.z - pos[2]) ** 2
                    ) ** 0.5,
                }
                for loc in nearby
            ]

        return analysis

    # ── 规划 ──────────────────────────────────────────────────────────

    async def plan(self, task_description: str) -> TaskPlan:
        """
        为任务生成执行计划

        1. 环境扫描
        2. 构建规划提示词 (含环境 + 长期记忆 + 世界知识)
        3. 调用 LLM 生成 JSON 计划
        4. 解析 + 验证 + 缓存
        """
        if not self._planning_enabled:
            return TaskPlan(task_description=task_description)

        # 1. 环境扫描
        env = await self.analyze_environment()

        # 2. 构建提示词
        prompt = self._build_planning_prompt(task_description, env)

        # 3. 调用 LLM (使用 reasoning 模式)
        try:
            result = await self.llm.chat(
                messages=[
                    SystemMessage(content=PLANNING_SYSTEM_PROMPT),
                    UserMessage(content=prompt),
                ],
                temperature=0.0,
                max_tokens=1024,
            )

            # 4. 解析 JSON
            plan = self._parse_plan(result.content or "{}", task_description)

            # 注入 reasoning
            if result.reasoning:
                plan.reasoning = result.reasoning

        except Exception as e:
            logger.warning(f"规划 LLM 调用失败: {e}")
            plan = TaskPlan(
                task_description=task_description,
                steps=[f"执行: {task_description}"],
                confidence=0.3,
                fallback_plan="LLM 规划失败, 直接尝试执行",
            )

        # 5. 缓存
        self._last_plan = plan
        return plan

    def _build_planning_prompt(self, task: str, env: EnvAnalysis) -> str:
        """构建规划提示词"""
        parts = [f"## 任务\n{task}", ""]

        # 环境分析
        parts.append(env.to_text())
        parts.append("")

        # 已知位置 (世界配置)
        if self.world_config.is_loaded:
            locs = self.world_config.locations[:10]
            if locs:
                parts.append("## 世界已知位置")
                for loc in locs:
                    parts.append(f"- {loc.name}: {loc.description} "
                                 f"(x={loc.x:.0f}, y={loc.y:.0f}, z={loc.z:.0f})")
                parts.append("")

        # 长期记忆
        recent_events = self.memory.get_recent_events(5, min_importance=1)
        if recent_events:
            parts.append("## 最近事件")
            for e in recent_events:
                parts.append(f"- {e.description}")
            parts.append("")

        # 玩家偏好
        if self.world_config.preferences:
            parts.append(f"## 玩家偏好\n{'; '.join(self.world_config.preferences)}")
            parts.append("")

        parts.append("请生成执行计划 (JSON 格式)。")
        return "\n".join(parts)

    def _parse_plan(self, content: str, task: str) -> TaskPlan:
        """解析 LLM 返回的 JSON 计划"""
        try:
            # 尝试直接解析
            data = json.loads(content)
        except json.JSONDecodeError:
            # 尝试提取 JSON 块
            from src.utils.serialize import extract_info
            extracted = extract_info(content)
            if extracted:
                data = extracted[0]
            else:
                data = {}

        return TaskPlan(
            task_description=task,
            steps=data.get("steps", [f"执行: {task}"]),
            prerequisites=data.get("prerequisites", []),
            required_resources=data.get("required_resources", []),
            estimated_steps=data.get("estimated_steps", len(data.get("steps", []))),
            confidence=data.get("confidence", 0.5),
            fallback_plan=data.get("fallback_plan"),
        )

    # ── 快速规划 (无需 LLM, 启发式) ──────────────────────────────────

    def quick_plan(self, task_description: str) -> TaskPlan:
        """
        快速规划 — 不调用 LLM, 基于模板匹配

        用于简单/常见任务, 节省 token。
        """
        steps, prerequisites, resources = self._match_template(task_description)

        return TaskPlan(
            task_description=task_description,
            steps=steps or [f"1. 分析任务: {task_description}", "2. 检查环境", "3. 执行操作", "4. 验证结果"],
            prerequisites=prerequisites,
            required_resources=resources,
            estimated_steps=len(steps) if steps else 4,
            confidence=0.5 if steps else 0.3,
        )

    def _match_template(self, task: str) -> tuple[list[str], list[str], list[str]]:
        """简单模板匹配"""
        task_lower = task.lower()

        # 挖掘类
        if any(kw in task_lower for kw in ["挖", "dig", "mine", "采集"]):
            return (
                ["1. 扫描附近可挖掘方块", "2. 移动到目标位置", "3. 挖掘目标方块", "4. 收集掉落物"],
                ["需要正确的工具"],
                ["pickaxe", "shovel"],
            )

        # 合成类
        if any(kw in task_lower for kw in ["合成", "制作", "craft", "make"]):
            return (
                ["1. 检查库存中是否有原料", "2. 如果没有,采集或寻找原料", "3. 找到或放置工作台", "4. 在工作台合成目标物品"],
                ["需要工作台"],
                [],
            )

        # 建造类
        if any(kw in task_lower for kw in ["建造", "build", "放置", "place"]):
            return (
                ["1. 确认建造方案和位置", "2. 检查所需材料", "3. 准备材料(采集/合成)", "4. 按方案放置方块"],
                ["需要足够材料"],
                [],
            )

        # 探索类
        if any(kw in task_lower for kw in ["探索", "explore", "找", "find", "寻找"]):
            return (
                ["1. 确定搜索目标", "2. 检查已知位置是否有线索", "3. 向目标方向移动", "4. 扫描周围环境"],
                [],
                [],
            )

        return ([], [], [])

    # ── 计划调整 ──────────────────────────────────────────────────────

    def update_after_step(self, step_result: dict):
        """
        根据步骤执行结果调整计划

        如果某步失败, 降低置信度并考虑备用计划。
        """
        if self._last_plan is None:
            return

        success = step_result.get("status", False)
        if not success:
            # 降低置信度
            self._last_plan.confidence *= 0.7
            logger.info(
                f"计划置信度降至 {self._last_plan.confidence:.0%}"
                + (f", 考虑备用: {self._last_plan.fallback_plan}"
                   if self._last_plan.fallback_plan else "")
            )

    # ── 查询 ──────────────────────────────────────────────────────────

    @property
    def last_plan(self) -> Optional[TaskPlan]:
        return self._last_plan

    @property
    def planning_enabled(self) -> bool:
        return self._planning_enabled

    def disable(self):
        self._planning_enabled = False
        logger.info("预规划已禁用")

    def enable(self):
        self._planning_enabled = True
        logger.info("预规划已启用")
