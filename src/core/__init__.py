"""
核心 Agent 框架 — Phase 2-4 事件驱动 + 持久化记忆 + 预规划

组件:
- EventBus: 优先级异步事件总线
- AsyncBaseAgent: 事件驱动的 Minecraft AI Agent
- AgentController: Agent 生命周期管理器
- MinecraftBridge: Minecraft ↔ EventBus 桥接
- ConversationMemory: 短期对话记忆 (Phase 2)
- ToolRegistry: JSON Schema 工具注册表 (Phase 3)
- WorldConfig: 世界 CLAUDE.md 管理 (Phase 4)
- LongTermMemory: JSON 持久化记忆 (Phase 4)
- TaskPlanner: 任务预规划引擎 (Phase 4)
"""

from src.core.event_bus import (  # noqa: F401
    EventBus,
    Event,
    EventType,
    make_interrupt,
    make_user_input,
    make_chat,
    make_timer,
    make_world_change,
)

from src.core.agent import (  # noqa: F401
    AsyncBaseAgent,
    AgentState,
    AgentStats,
)

from src.core.controller import (  # noqa: F401
    AgentController,
    AgentConfig,
    create_controller_from_config,
)

from src.core.bridge import (  # noqa: F401
    MinecraftBridge,
    BridgeMode,
    BridgeResult,
)

from src.core.conversation import (  # noqa: F401
    ConversationMemory,
    WorldStateSnapshot,
)

from src.core.tools import (  # noqa: F401
    ToolRegistry,
    ToolEntry,
    MINECRAFT_TOOL_DEFINITIONS,
)

# Phase 4: 持久化记忆与预规划
from src.core.world_config import (  # noqa: F401
    WorldConfig,
    WorldInfo,
    LocationEntry,
    EventEntry,
)

from src.core.long_term_memory import (  # noqa: F401
    LongTermMemory,
    TimelineEvent,
    KnownLocation,
    PlayerProfile,
)

from src.core.planning import (  # noqa: F401
    TaskPlanner,
    TaskPlan,
    EnvAnalysis,
)
