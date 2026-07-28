"""
全量集成测试

测试核心模块的正确性:
- EventBus: 发布/订阅/优先级/历史/内存泄漏
- ConversationMemory: 构建消息/截断/工具描述注入
- MinecraftBridge: MOCK模式/工具执行
- WorldConfig: 加载/保存/位置/事件
- LongTermMemory: 事件/位置/玩家档案
- EmotionEngine: 情绪触发/提示词生成
- ToolRegistry: 17工具 OpenAI Schema (与 Flask /api/action 一一对应)
- TaskPlanner: LLM 规划接口存在性
- main.py: 入口正确性
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows GBK 控制台兼容: 强制 UTF-8 输出
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import asyncio
import tempfile

import inspect

PASS = 0
FAIL = 0

def check(name, condition, msg=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}: {msg}")


# ═══════════════════════════════════════════════════════════════
# 1. EventBus
# ═══════════════════════════════════════════════════════════════
async def test_eventbus():
    print("\n1. EventBus")
    from src.core.event_bus import (
        EventBus, EventType, Event,
        make_user_input, make_interrupt,
    )

    bus = EventBus(history_size=200)
    received = []

    async def handler(event):
        received.append(event)

    bus.subscribe(EventType.USER_INPUT, handler, target="Bot")
    await bus.start()
    check("subscription", bus.subscriber_count["USER_INPUT"] == 1)

    await bus.publish(make_user_input("test", target="Bot"))
    await asyncio.sleep(0.2)
    check("event delivery", len(received) == 1)
    check("event history", len(bus.get_history()) == 1)

    # Memory leak fix
    evt = Event(type=EventType.USER_INPUT, target="Bot", data={"msg": "x"})
    future = await bus.request(evt)
    await asyncio.sleep(0.1)
    check("memory leak fix", evt.id not in bus._pending_requests,
          f"pending_requests={len(bus._pending_requests)}")

    await bus.stop()


# ═══════════════════════════════════════════════════════════════
# 2. ConversationMemory
# ═══════════════════════════════════════════════════════════════
async def test_memory():
    print("\n2. ConversationMemory")
    from src.core.conversation import ConversationMemory, WorldStateSnapshot

    mem = ConversationMemory(agent_name="TestBot", max_history=10)
    mem.update_tool_descriptions("tool: moveTo, mineBlock")
    check("tool_descriptions", mem._tool_descriptions != "",
          "should not be empty after fix")

    mem.add_user_message("hello", player="Steve")
    mem.add_assistant_message("hi!")
    mem.add_tool_result("c1", "getWorldInfo", {"status": True})
    msgs = mem.build_messages("mine")
    check("build_messages", len(msgs) >= 5, f"got {len(msgs)}")

    for i in range(20):
        mem.add_user_message(f"msg{i}")
    check("truncation", mem.message_count <= 10,
          f"got {mem.message_count}")


# ═══════════════════════════════════════════════════════════════
# 3. MinecraftBridge (MOCK)
# ═══════════════════════════════════════════════════════════════
async def test_bridge():
    print("\n3. MinecraftBridge (MOCK)")
    from src.core.event_bus import EventBus
    from src.core.bridge import MinecraftBridge, BridgeMode

    bus = EventBus()
    bridge = MinecraftBridge(event_bus=bus, mode=BridgeMode.MOCK)
    await bridge.start()

    world = await bridge.get_world_state()
    check("get_world_state", "my_position" in world)

    result = await bridge.execute("moveTo", {"x": 10, "y": 64, "z": 20})
    check("execute(moveTo)", result.status is True)

    check("_mock_execute async", inspect.iscoroutinefunction(bridge._mock_execute))

    await bridge.stop()


# ═══════════════════════════════════════════════════════════════
# 4. WorldConfig
# ═══════════════════════════════════════════════════════════════
async def test_world_config():
    print("\n4. WorldConfig")
    from src.core.world_config import WorldConfig

    with tempfile.TemporaryDirectory() as tmp:
        wc = WorldConfig(world_name="test", config_dir=tmp)
        await wc.load()
        await wc.add_location("base", "main base", 100, 64, 200)
        await wc.add_event("found diamonds", category="mining")

        prompt = wc.to_system_prompt()
        check("to_system_prompt", "test" in prompt and "base" in prompt)

        found = wc.find_location("base")
        check("find_location", found is not None and found.x == 100)


# ═══════════════════════════════════════════════════════════════
# 5. LongTermMemory
# ═══════════════════════════════════════════════════════════════
async def test_ltm():
    print("\n5. LongTermMemory")
    from src.core.long_term_memory import LongTermMemory

    with tempfile.TemporaryDirectory() as tmp:
        ltm = LongTermMemory(world_name="test", data_dir=tmp)
        await ltm.load()
        await ltm.record_event("test event", tags=["test"], importance=3)
        await ltm.remember_location("test_loc", 0, 64, 0, "test loc", tags=["test"])
        await ltm.record_interaction("Steve")

        check("timeline", ltm.event_count >= 1)
        check("locations", ltm.location_count == 1)
        check("players", ltm.player_count == 1)

        locs = ltm.find_locations_by_tag("test")
        check("find_by_tag", len(locs) == 1)


# ═══════════════════════════════════════════════════════════════
# 6. ToolRegistry (17 个工具, 与 Flask /api/action 一一对应)
# ═══════════════════════════════════════════════════════════════
FLASK_ROUTE_MAP_TOOLS = {
    'moveTo', 'followPlayer', 'mineBlock', 'placeBlock', 'getInventory',
    'getWorldInfo', 'scanNearbyEntities', 'findBlock', 'equipItem',
    'attackEntity', 'openChest', 'interactBlock', 'craftItem', 'smeltItem',
    'finalAnswer', 'wait', 'sendChat',
}

def test_tools():
    print("\n6. Tools")
    from src.core.tools import MINECRAFT_TOOL_DEFINITIONS, ToolRegistry

    names = {t.name for t in MINECRAFT_TOOL_DEFINITIONS}
    check("17 tools", len(MINECRAFT_TOOL_DEFINITIONS) == 17,
          f"got {len(MINECRAFT_TOOL_DEFINITIONS)}")
    check("tools match flask route_map", names == FLASK_ROUTE_MAP_TOOLS,
          f"diff: {names ^ FLASK_ROUTE_MAP_TOOLS}")

    # OpenAI schemas
    registry = ToolRegistry()
    for td in MINECRAFT_TOOL_DEFINITIONS:
        registry.register(td.name, td.description, list(td.parameters),
                         category="test", handler=lambda **k: {"status": True})
    schemas = [t.to_openai_schema() for t in registry.get_openai_tools()]
    check("openai schemas", len(schemas) == 17 and all("function" in s for s in schemas))


# ═══════════════════════════════════════════════════════════════
# 7. EmotionEngine
# ═══════════════════════════════════════════════════════════════
def test_emotions():
    print("\n7. EmotionEngine")
    from src.prompts.emotions import EmotionEngine, Emotion

    engine = EmotionEngine(personality_type="热情")
    engine.on_task_success(0.8)
    engine.on_player_praise()
    mood = engine.current_mood
    check("mood triggered", mood in (Emotion.HAPPY, Emotion.PROUD),
          f"mood={mood.value}")

    frag = engine.to_prompt_fragment()
    has_emotion = any(w in frag for w in ["开心", "自豪", "😊", "😎"])
    check("prompt fragment", has_emotion, f"fragment={frag[:50]}")


# ═══════════════════════════════════════════════════════════════
# 8. Planning
# ═══════════════════════════════════════════════════════════════
def test_planning():
    print("\n8. Planning")
    from src.core.planning import TaskPlanner

    # quick_plan/update_after_step 等模板方法已在清理中移除,
    # 只验证 LLM 规划接口存在
    check("plan method", callable(getattr(TaskPlanner, "plan", None)))
    check("analyze_environment method", callable(getattr(TaskPlanner, "analyze_environment", None)))
    check("planning_enabled prop", isinstance(TaskPlanner.planning_enabled, property))


# ═══════════════════════════════════════════════════════════════
# 9. main.py
# ═══════════════════════════════════════════════════════════════
def test_main():
    print("\n9. main.py")
    import main
    check("run_agent exists", hasattr(main, "run_agent"))
    check("run_web exists", hasattr(main, "run_web"))

    src = inspect.getsource(main.run_agent)
    check("no TODO stub", "TODO" not in src, "run_agent still has TODO!")
    check("AgentController wired", "create_controller_from_config" in src)


# ═══════════════════════════════════════════════════════════════
# 10. All imports
# ═══════════════════════════════════════════════════════════════
def test_imports():
    print("\n10. All imports")
    modules = [
        ("src.llm.base", "LLM base"),
        ("src.llm.retry", "Retry"),
        ("src.llm.openai_compat", "OpenAI compat"),
        ("src.llm.factory", "Factory"),
        ("src.llm", "LLM package"),
        ("src.core.event_bus", "EventBus"),
        ("src.core.agent", "Agent"),
        ("src.core.controller", "Controller"),
        ("src.core.bridge", "Bridge"),
        ("src.core.conversation", "Conversation"),
        ("src.core.tools", "Tools"),
        ("src.core.world_config", "WorldConfig"),
        ("src.core.long_term_memory", "LTM"),
        ("src.core.planning", "Planning"),
        ("src.core.interaction", "Interaction"),
        ("src.core.structured_logging", "StructuredLog"),
        ("src.core", "Core package"),
        ("src.prompts.system_prompts", "Prompts"),
        ("src.prompts.emotions", "Emotions"),
        ("src.prompts.personality", "Personality"),
        ("src.prompts", "Prompts package"),
        ("src.utils.serialize", "Serialize"),
        ("src.web.app", "Web app"),
        ("main", "main entry"),
    ]
    for mod, desc in modules:
        try:
            __import__(mod)
            check(f"import {mod}", True)
        except Exception as e:
            check(f"import {mod}", False, f"{type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════
async def main():
    print("=" * 60)
    print("VillagerAgent 全量集成测试")
    print("=" * 60)

    await test_eventbus()
    await test_memory()
    await test_bridge()
    await test_world_config()
    await test_ltm()
    test_tools()
    test_emotions()
    test_planning()
    test_main()
    test_imports()

    print()
    print("=" * 60)
    total = PASS + FAIL
    print(f"通过: {PASS}/{total}  |  失败: {FAIL}/{total}")
    if FAIL == 0:
        print("🎉 全部测试通过!")
    else:
        print(f"❌ {FAIL} 个测试失败")
    print("=" * 60)
    return FAIL == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    exit(0 if success else 1)
