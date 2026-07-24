"""
游戏内交互管理器 — Phase 5 交互体验

管理 Agent 与玩家的游戏内交互:
- 聊天格式: [伙伴] Alice: 消息
- 进度报告: "挖掘中... ████░░░░ 60%"
- 情绪表情动作: /me 命令, 潜行/跳跃信号
- 主动对话: 根据情绪和上下文发起话题
- 响应格式自适应: 区分指令响应/聊天/状态汇报

用法:
    interaction = InteractionManager(agent_name="伙伴", emotion_engine=engine)
    msg = interaction.format_response("我在 x=200 找到了钻石！")
    # → "[伙伴] Bot 😊: 我在 x=200 找到了钻石！"

    report = interaction.progress_report("挖掘", done=6, total=10)
    # → "[伙伴] 挖掘进度: ██████░░░░ 60% (6/10)"

    proactive = interaction.check_proactive(emotion, idle_seconds=120)
    # → "嘿！有什么需要帮忙的吗？"
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 交互配置
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class InteractionConfig:
    """交互行为配置"""
    agent_name: str = "伙伴"
    chat_prefix: str = ""  # "", "[{name}]", "/msg {player}"
    use_emojis: bool = True
    use_progress_reports: bool = True
    proactive_chat: bool = True  # 是否主动发起对话
    proactive_interval: float = 300.0  # 主动对话最小间隔 (秒)
    progress_report_interval: float = 60.0  # 进度报告最小间隔

    @property
    def name_tag(self) -> str:
        """聊天名称标签"""
        return f"[{self.agent_name}]"


# ═══════════════════════════════════════════════════════════════════════════════
# 主动对话模板
# ═══════════════════════════════════════════════════════════════════════════════

# 不同情境的主动对话模板
PROACTIVE_TEMPLATES = {
    "greeting": [
        "大家好！今天有什么计划吗？",
        "嘿！需要帮忙的话随时叫我~",
        "又是美好的一天！准备做点什么？",
    ],
    "idle": [
        "周围好安静啊...有人吗？",
        "我在附近逛逛，有事随时叫我！",
        "看起来没什么事，要不要去探险？",
    ],
    "discovery": [
        "哇！我在 {location} 发现了 {thing}！要来看看吗？",
        "大家注意！{location} 那边好像有 {thing}！",
    ],
    "danger": [
        "⚠️ 小心！附近有 {threat}！",
        "有危险！我看到 {threat} 了，建议不要靠近 {location}。",
    ],
    "completion": [
        "搞定了！{task_summary} ✅",
        "完成啦！{task_summary}。还有什么需要吗？",
    ],
    "need_help": [
        "这个任务有点难...有没有人能帮忙？",
        "我卡住了 😅 有人看到 {thing} 在哪里吗？",
    ],
    "weather": [
        "今天天气不错！适合出去逛逛~",
        "外面下雨了...要不要在家整理一下库存？",
        "天黑了，小心怪物哦！",
    ],
}


# ═══════════════════════════════════════════════════════════════════════════════

class InteractionManager:
    """
    游戏内交互管理器

    管理 Agent 与玩家的沟通方式:
    - 消息格式化 (统一 [Name] 标签)
    - 进度报告 (进度条样式)
    - 情绪表情集成
    - 主动对话决策

    用法:
        im = InteractionManager(agent_name="Alice", emotion_engine=engine)
        msg = im.format_response("你好！")
        await im.report_progress(bridge, "挖掘", 6, 10)
    """

    def __init__(
        self,
        agent_name: str = "伙伴",
        emotion_engine=None,  # EmotionEngine
        config: Optional[InteractionConfig] = None,
    ):
        self.agent_name = agent_name
        self.emotion_engine = emotion_engine
        self.config = config or InteractionConfig(agent_name=agent_name)

        # 状态
        self._last_proactive_time = 0.0
        self._last_progress_time = 0.0
        self._interaction_count = 0
        self._recent_tasks: list[dict] = []  # 最近的任务记录

    # ── 消息格式化 ────────────────────────────────────────────────────

    def format_response(self, content: str, recipient: str = "") -> str:
        """
        格式化回复消息

        格式: [AgentName] [Emoji]: Content

        Args:
            content: 回复内容
            recipient: 回复对象 (玩家名, 可选)
        """
        parts = [self.config.name_tag]

        # 情绪 emoji
        if self.config.use_emojis and self.emotion_engine:
            emoji = self.emotion_engine.to_prompt_short()
            if emoji:
                parts.append(f" {emoji}")

        if recipient:
            parts.append(f" → {recipient}")

        parts.append(f": {content}")
        return "".join(parts)

    def format_thought(self, thought: str) -> str:
        """格式化思考过程 (展示给玩家)"""
        return f"[{self.agent_name} 的想法]: {thought}"

    def format_action(self, action: str) -> str:
        """格式化表情动作 (/me)"""
        return f"/me {action}"

    # ── 进度报告 ─────────────────────────────────────────────────────

    def progress_bar(self, done: int, total: int, width: int = 10) -> str:
        """生成进度条"""
        if total <= 0:
            return "░░░░░░░░░░"
        ratio = min(done / total, 1.0)
        filled = int(ratio * width)
        return "█" * filled + "░" * (width - filled)

    async def report_progress(
        self,
        bridge,  # MinecraftBridge
        task_name: str,
        done: int,
        total: int,
        force: bool = False,
    ):
        """
        发送进度报告

        Args:
            bridge: MinecraftBridge (用于发送消息)
            task_name: 任务名称
            done: 已完成数
            total: 总数
            force: 是否强制发送 (忽略间隔限制)

        格式: [AgentName] task_name 进度: ██████░░░░ 60% (6/10)
        """
        if not self.config.use_progress_reports:
            return

        now = time.monotonic()
        if not force and (now - self._last_progress_time) < self.config.progress_report_interval:
            return

        self._last_progress_time = now
        bar = self.progress_bar(done, total)
        pct = int(done / total * 100) if total > 0 else 0
        msg = f"{task_name} 进度: {bar} {pct}% ({done}/{total})"
        formatted = self.format_response(msg)
        await bridge.send_chat(formatted)

    # ── 任务记录 ──────────────────────────────────────────────────────

    def record_task(self, description: str, success: bool, tool_steps: int):
        """记录任务"""
        self._recent_tasks.append({
            "description": description[:100],
            "success": success,
            "steps": tool_steps,
            "time": time.monotonic(),
        })
        if len(self._recent_tasks) > 20:
            self._recent_tasks = self._recent_tasks[-20:]
        self._interaction_count += 1

    # ── 主动对话 ──────────────────────────────────────────────────────

    def check_proactive(
        self,
        idle_seconds: float = 0.0,
        world_time: str = "day",
        nearby_players: int = 0,
    ) -> Optional[str]:
        """
        检查是否应该主动发起对话

        返回: 要发送的消息 (None = 不发起)

        决策因素:
        - 空闲时间 (超过 proactive_interval 才考虑)
        - 上次主动对话时间
        - 附近玩家数量
        - 世界时间
        """
        if not self.config.proactive_chat:
            return None

        now = time.monotonic()
        if (now - self._last_proactive_time) < self.config.proactive_interval:
            return None

        # 只有附近有玩家时才主动对话
        if nearby_players == 0:
            return None

        self._last_proactive_time = now

        # 根据情境选择模板
        if world_time == "sunrise":
            template_key = "greeting"
        elif world_time in ("night", "midnight"):
            template_key = "weather"
        elif random.random() < 0.3:
            template_key = "idle"
        else:
            template_key = "greeting"

        templates = PROACTIVE_TEMPLATES.get(template_key, PROACTIVE_TEMPLATES["greeting"])
        return random.choice(templates)

    def discovery_announcement(self, location: str, thing: str) -> str:
        """发现公告"""
        templates = PROACTIVE_TEMPLATES["discovery"]
        msg = random.choice(templates).format(location=location, thing=thing)
        return self.format_response(msg)

    def danger_alert(self, threat: str, location: str = "") -> str:
        """危险警告"""
        templates = PROACTIVE_TEMPLATES["danger"]
        msg = random.choice(templates).format(threat=threat, location=location or "这里")
        return self.format_response(msg)

    def completion_message(self, task_summary: str) -> str:
        """完成公告"""
        templates = PROACTIVE_TEMPLATES["completion"]
        msg = random.choice(templates).format(task_summary=task_summary)
        return self.format_response(msg)

    def help_request(self, thing: str = "") -> str:
        """求助"""
        templates = PROACTIVE_TEMPLATES["need_help"]
        msg = random.choice(templates).format(thing=thing or "需要的东西")
        return self.format_response(msg)

    # ── 响应模式选择 ──────────────────────────────────────────────────

    def choose_response_mode(
        self,
        is_command: bool = False,
        is_question: bool = False,
        emotion_level: float = 0.0,
    ) -> str:
        """
        选择响应模式

        Returns: "brief" | "normal" | "enthusiastic" | "worried" | "tired"
        """
        if is_command:
            return "brief"  # 指令响应: 简短务实

        if self.emotion_engine:
            from src.prompts.emotions import Emotion
            mood = self.emotion_engine.current_mood
            if mood == Emotion.EXCITED:
                return "enthusiastic"
            if mood == Emotion.WORRIED:
                return "worried"
            if mood == Emotion.TIRED:
                return "tired"

        if is_question:
            return "normal"

        return "normal"

    def get_response_instruction(self, mode: str) -> str:
        """获取响应模式对应的 LLM 指令"""
        instructions = {
            "brief": "用简短的一两句话回复。",
            "normal": "用自然的语气回复。",
            "enthusiastic": "用热情的语气回复，可以多说几句！",
            "worried": "语气保持谨慎，注意提醒安全。",
            "tired": "语气慵懒一些，回复简短。",
        }
        return instructions.get(mode, "用自然的语气回复。")

    # ── 状态 ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "interaction_count": self._interaction_count,
            "last_proactive": self._last_proactive_time,
            "recent_tasks": len(self._recent_tasks),
        }
