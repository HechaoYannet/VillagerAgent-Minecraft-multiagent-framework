"""
情绪引擎 — Phase 5 人格系统增强

为 Agent 添加情感状态，影响对话风格和行为决策。

情绪模型:
- 7 种基础情绪: 开心😊 / 平静😐 / 担忧😟 / 兴奋🤩 / 疲倦😴 / 好奇🤔 / 自豪😎
- 情绪强度: 0.0-1.0 (随时间衰减)
- 事件驱动转换: 任务成功→开心, 失败→担忧, 发现稀有物品→兴奋
- 性格影响: 不同性格对事件的敏感度不同

用法:
    engine = EmotionEngine(personality_type="热情")
    engine.on_task_success()     # → 开心 +0.3, 自豪 +0.2
    engine.on_player_praise()    # → 开心 +0.4
    engine.on_failure()          # → 担忧 +0.3
    current = engine.current_mood  # → "开心"
    prompt = engine.to_prompt_fragment()  # → "当前情绪: 开心😊 (0.7)"
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 情绪定义
# ═══════════════════════════════════════════════════════════════════════════════

class Emotion(Enum):
    """7 种基础情绪"""
    HAPPY = "开心"        # 😊 任务成功, 玩家表扬
    CALM = "平静"         # 😐 默认状态
    WORRIED = "担忧"      # 😟 任务失败, 低生命值
    EXCITED = "兴奋"      # 🤩 发现稀有物品, 击败 Boss
    TIRED = "疲倦"        # 😴 长时间工作
    CURIOUS = "好奇"      # 🤔 探索新区域
    PROUD = "自豪"        # 😎 完成重要成就


# Emoji 映射
EMOTION_EMOJI = {
    Emotion.HAPPY: "😊",
    Emotion.CALM: "😐",
    Emotion.WORRIED: "😟",
    Emotion.EXCITED: "🤩",
    Emotion.TIRED: "😴",
    Emotion.CURIOUS: "🤔",
    Emotion.PROUD: "😎",
}

# 情绪对应的语气描述
EMOTION_TONE = {
    Emotion.HAPPY: "语气轻快, 愿意多说几句",
    Emotion.CALM: "语气平稳, 简洁务实",
    Emotion.WORRIED: "语气谨慎, 提醒风险",
    Emotion.EXCITED: "语气热情, 充满能量",
    Emotion.TIRED: "语气慵懒, 话变少了",
    Emotion.CURIOUS: "语气好奇, 喜欢问问题",
    Emotion.PROUD: "语气自信, 可能小小炫耀",
}


# ═══════════════════════════════════════════════════════════════════════════════
# 性格 → 情绪敏感度映射
# ═══════════════════════════════════════════════════════════════════════════════

PERSONALITY_EMOTION_MODIFIERS = {
    # "热情": 更容易开心和兴奋
    "热情": {Emotion.HAPPY: 1.5, Emotion.EXCITED: 1.3, Emotion.WORRIED: 0.7},
    "热情开朗": {Emotion.HAPPY: 1.5, Emotion.EXCITED: 1.3, Emotion.WORRIED: 0.7},
    # "冷静": 情绪波动小
    "冷静": {Emotion.HAPPY: 0.7, Emotion.EXCITED: 0.6, Emotion.WORRIED: 0.6, Emotion.PROUD: 0.8},
    "沉稳": {Emotion.HAPPY: 0.7, Emotion.EXCITED: 0.6, Emotion.WORRIED: 0.6, Emotion.PROUD: 0.8},
    # "好奇": 更容易好奇和兴奋
    "好奇": {Emotion.CURIOUS: 1.5, Emotion.EXCITED: 1.2},
    "顽皮": {Emotion.HAPPY: 1.2, Emotion.EXCITED: 1.4, Emotion.CURIOUS: 1.3},
    # "可靠": 更自豪, 更少担忧
    "可靠": {Emotion.PROUD: 1.3, Emotion.WORRIED: 0.5, Emotion.CALM: 1.2},
    "稳重": {Emotion.PROUD: 1.3, Emotion.WORRIED: 0.5, Emotion.CALM: 1.2},
    # "暴躁": 更容易担忧和疲倦
    "暴躁": {Emotion.WORRIED: 1.4, Emotion.TIRED: 1.3, Emotion.HAPPY: 0.6},
}


@dataclass
class EmotionState:
    """单个情绪的状态"""
    emotion: Emotion
    intensity: float = 0.0  # 0.0-1.0
    last_triggered: float = 0.0  # 最后触发时间


# ═══════════════════════════════════════════════════════════════════════════════

class EmotionEngine:
    """
    情绪引擎 — 管理 Agent 的情感状态

    情绪随时间衰减 (半衰期 5 分钟)。
    事件触发情绪变化，强度受性格调节。

    用法:
        engine = EmotionEngine(personality_type="热情")
        engine.update()  # 每 TIMER 周期调用, 衰减情绪
        engine.on_task_success()
        mood = engine.current_mood  # → Emotion.HAPPY
    """

    # 情绪衰减半衰期 (秒)
    DECAY_HALF_LIFE = 300  # 5 分钟

    # 情绪阈值 (超过此值才认为是"处于该情绪")
    MOOD_THRESHOLD = 0.25

    def __init__(self, personality_type: str = "热情"):
        self.personality_type = personality_type
        self._modifiers = PERSONALITY_EMOTION_MODIFIERS.get(
            personality_type, {}
        )

        # 初始化所有情绪
        self._states: dict[Emotion, EmotionState] = {
            e: EmotionState(emotion=e, intensity=0.0)
            for e in Emotion
        }

        # 平静是默认情绪 (有一定基础强度)
        self._states[Emotion.CALM].intensity = 0.3

        # 事件计数 (影响情绪衰减)
        self._event_count = 0
        self._last_update = time.monotonic()

    # ── 情绪更新 ──────────────────────────────────────────────────────

    def update(self):
        """每周期调用 — 情绪自然衰减"""
        now = time.monotonic()
        elapsed = now - self._last_update
        self._last_update = now

        # 指数衰减: intensity *= 0.5^(elapsed / half_life)
        decay_factor = 0.5 ** (elapsed / self.DECAY_HALF_LIFE)

        for state in self._states.values():
            if state.emotion == Emotion.CALM:
                # 平静回归基线
                state.intensity = state.intensity * decay_factor + 0.3 * (1 - decay_factor)
            else:
                state.intensity *= decay_factor
                # 低于阈值时清零 (避免浮点微小值)
                if state.intensity < 0.01:
                    state.intensity = 0.0

    # ── 事件触发器 ────────────────────────────────────────────────────

    def on_task_success(self, difficulty: float = 0.5):
        """任务成功 → 开心 + 自豪"""
        self._add_emotion(Emotion.HAPPY, 0.3 * difficulty)
        self._add_emotion(Emotion.PROUD, 0.2 * difficulty)
        self._event_count += 1

    def on_task_failure(self):
        """任务失败 → 担忧"""
        self._add_emotion(Emotion.WORRIED, 0.35)
        self._sub_emotion(Emotion.HAPPY, 0.2)
        self._event_count += 1

    def on_discovery(self, rarity: str = "common"):
        """发现物品/地点 → 兴奋 + 好奇"""
        rarity_bonus = {"common": 0.2, "uncommon": 0.4, "rare": 0.6, "epic": 0.8}
        bonus = rarity_bonus.get(rarity, 0.2)
        self._add_emotion(Emotion.EXCITED, bonus)
        self._add_emotion(Emotion.CURIOUS, bonus * 0.7)
        self._event_count += 1

    def on_player_praise(self):
        """玩家表扬 → 开心 +0.4"""
        self._add_emotion(Emotion.HAPPY, 0.4)
        self._sub_emotion(Emotion.WORRIED, 0.3)
        self._event_count += 1

    def on_player_criticism(self):
        """玩家批评 → 担忧 +0.3"""
        self._add_emotion(Emotion.WORRIED, 0.3)
        self._sub_emotion(Emotion.HAPPY, 0.3)
        self._event_count += 1

    def on_danger_detected(self, severity: float = 0.5):
        """检测到危险 (怪物/低生命值) → 担忧"""
        self._add_emotion(Emotion.WORRIED, 0.4 * severity)
        self._event_count += 1

    def on_long_idle(self):
        """长时间空闲 → 好奇 (想找事做) 或 疲倦"""
        if random.random() < 0.5:
            self._add_emotion(Emotion.CURIOUS, 0.2)
        else:
            self._add_emotion(Emotion.TIRED, 0.15)
        self._event_count += 1

    def on_achievement(self, importance: float = 0.5):
        """完成重要成就 → 兴奋 + 自豪"""
        self._add_emotion(Emotion.EXCITED, 0.5 * importance)
        self._add_emotion(Emotion.PROUD, 0.6 * importance)
        self._event_count += 1

    def on_help_player(self):
        """帮助了玩家 → 开心 + 自豪"""
        self._add_emotion(Emotion.HAPPY, 0.25)
        self._add_emotion(Emotion.PROUD, 0.15)
        self._event_count += 1

    # ── 内部方法 ──────────────────────────────────────────────────────

    def _add_emotion(self, emotion: Emotion, amount: float):
        """增加情绪强度 (受性格调节)"""
        modifier = self._modifiers.get(emotion, 1.0)
        state = self._states[emotion]
        state.intensity = min(1.0, state.intensity + amount * modifier)
        state.last_triggered = time.monotonic()

    def _sub_emotion(self, emotion: Emotion, amount: float):
        """减少情绪强度"""
        state = self._states[emotion]
        state.intensity = max(0.0, state.intensity - amount)

    # ── 查询 ──────────────────────────────────────────────────────────

    @property
    def current_mood(self) -> Emotion:
        """当前主导情绪"""
        best = Emotion.CALM
        best_intensity = 0.0
        for state in self._states.values():
            if state.intensity > best_intensity and state.intensity >= self.MOOD_THRESHOLD:
                best = state.emotion
                best_intensity = state.intensity
        return best

    @property
    def mood_intensity(self) -> float:
        """当前主导情绪的强度"""
        return self._states[self.current_mood].intensity

    def get_top_emotions(self, n: int = 2) -> list[tuple[Emotion, float]]:
        """获取最强的 N 个情绪"""
        sorted_states = sorted(
            self._states.values(),
            key=lambda s: s.intensity,
            reverse=True,
        )
        return [(s.emotion, s.intensity) for s in sorted_states[:n]
                if s.intensity >= self.MOOD_THRESHOLD]

    # ── 提示词生成 ───────────────────────────────────────────────────

    def to_prompt_fragment(self) -> str:
        """
        生成情绪提示词片段 (注入 System Prompt)

        LLM 据此调整回复的语气。
        """
        mood = self.current_mood
        intensity = self.mood_intensity
        emoji = EMOTION_EMOJI.get(mood, "")
        tone = EMOTION_TONE.get(mood, "")

        if intensity < self.MOOD_THRESHOLD:
            return f"你当前心情平静 {emoji}。"

        lines = [f"## 当前情绪: {mood.value}{emoji} (强度: {intensity:.1%})"]

        # 多情绪描述
        top = self.get_top_emotions(3)
        if len(top) > 1:
            emotions_str = ", ".join(
                f"{e.value}{EMOTION_EMOJI.get(e, '')}" for e, _ in top
            )
            lines.append(f"情绪组合: {emotions_str}")

        lines.append(f"语气: {tone}")

        # 行为建议
        if mood == Emotion.WORRIED:
            lines.append("你可能想提醒玩家注意安全。")
        elif mood == Emotion.EXCITED:
            lines.append("你的回复可以更热情一些！")
        elif mood == Emotion.CURIOUS:
            lines.append("你可以主动提问或探索周围。")
        elif mood == Emotion.TIRED:
            lines.append("你可以提议休息或做点轻松的事。")

        return "\n".join(lines)

    def to_prompt_short(self) -> str:
        """简短情绪提示 (用于聊天前缀)"""
        mood = self.current_mood
        emoji = EMOTION_EMOJI.get(mood, "")
        if self.mood_intensity < self.MOOD_THRESHOLD:
            return ""
        return f"{emoji}"

    # ── 序列化 ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "personality_type": self.personality_type,
            "mood": self.current_mood.value,
            "mood_intensity": self.mood_intensity,
            "emotions": {
                e.value: {"intensity": round(s.intensity, 3)}
                for e, s in self._states.items()
            },
            "event_count": self._event_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EmotionEngine":
        engine = cls(personality_type=data.get("personality_type", "热情"))
        emotions_data = data.get("emotions", {})
        for e_name, e_data in emotions_data.items():
            for emotion in Emotion:
                if emotion.value == e_name:
                    engine._states[emotion].intensity = e_data.get("intensity", 0.0)
        engine._event_count = data.get("event_count", 0)
        return engine
