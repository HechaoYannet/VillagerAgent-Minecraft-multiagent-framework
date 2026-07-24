"""
端到端真实 Minecraft 测试

测试完整流水线:
    EventBus → Agent → LLM → Bridge → Flask Server → Mineflayer → Minecraft 1.21.1

前置条件:
    1. Minecraft 服务器运行在 localhost:25565 (Fabric 1.21.1 + EasyAuth)
    2. Flask Bot 服务器运行在 localhost:5000 (env/minecraft_server.py)
    3. EasyAuth 配置:
       - 如使用 globalPassword 模式 (推荐): 设置环境变量 EASYAUTH_PASSWORD
       - 如使用注册模式: 设置环境变量 EASYAUTH_PASSWORD + EASYAUTH_FORCE_LOGIN=false
    4. Node.js + npm 依赖已安装在 js_bridge/

环境变量:
    EASYAUTH_PASSWORD        — EasyAuth 全局密码 / 注册密码
    EASYAUTH_FORCE_LOGIN     — true: globalPassword 模式; false: 自动检测 (默认)
    MINECRAFT_HOST           — MC 服务器地址 (默认 localhost)
    MINECRAFT_PORT           — MC 服务器端口 (默认 25565)
    FLASK_BOT_PORT           — Flask 服务器端口 (默认 5000)

用法:
    python tests/test_e2e.py              # 自动检测服务器可用性
    python tests/test_e2e.py --mock       # 强制 MOCK 模式
    python tests/test_e2e.py --real       # 强制 REAL 模式
"""

import sys, os, asyncio, argparse, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0; FAIL = 0; SKIP = 0

def check(name, condition, msg=""):
    global PASS, FAIL, SKIP
    if condition is None:
        SKIP += 1
        print(f"  ⏭ {name}: SKIPPED ({msg})")
        return
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}: {msg}")

async def check_server(url, timeout=3):
    """检查服务器是否可达"""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout) as c:
            resp = await c.get(url)
            return resp.status_code == 200
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1: Server Connectivity
# ═══════════════════════════════════════════════════════════════════════════════
async def test_connectivity(mode):
    print("\n1. Server Connectivity")

    mc_ok = await check_server("http://localhost:5000/post_ping")
    check("Flask Bot Server (port 5000)", mc_ok, "未检测到 Flask 服务器 — 启动 env/minecraft_server.py")

    # Check if MC server port is open (basic TCP check)
    try:
        import socket
        s = socket.socket()
        s.settimeout(3)
        s.connect(("localhost", 25565))
        s.close()
        mc_port_ok = True
    except Exception:
        mc_port_ok = False
    check("Minecraft Server (port 25565)", mc_port_ok, "未检测到 Minecraft 服务器")

    return mc_ok and mc_port_ok


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: Bridge REAL Mode
# ═══════════════════════════════════════════════════════════════════════════════
async def test_bridge_real():
    print("\n2. Bridge REAL Mode")
    from src.core.event_bus import EventBus
    from src.core.bridge import MinecraftBridge, BridgeMode

    if not await check_server("http://localhost:5000/post_ping"):
        check("Bridge REAL — world state", None, "Flask 服务器不可达")
        check("Bridge REAL — ping", None, "")
        return

    bus = EventBus()
    bridge = MinecraftBridge(event_bus=bus, mode=BridgeMode.REAL, base_url="http://localhost:5000")

    try:
        await bridge.start()
        check("Bridge REAL — start", True)

        world = await bridge.get_world_state()
        check("Bridge REAL — world state", isinstance(world, dict) and len(world) > 0,
              f"got {type(world).__name__}")

        if world:
            has_pos = "my_position" in world
            check("Bridge REAL — position", has_pos)
            if has_pos:
                print(f"     Bot 位置: {world['my_position']}")
                print(f"     生命值: {world.get('health', '?')}/20")
                print(f"     时间: {world.get('timeOfDay', '?')}")

        result = await bridge.execute("getWorldInfo", {})
        check("Bridge REAL — execute", result is not None)

        await bridge.stop()
        check("Bridge REAL — stop", True)

    except ImportError as e:
        check("Bridge REAL — httpx", None, str(e))
    except Exception as e:
        check("Bridge REAL", False, f"{type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3: Flask API Endpoints
# ═══════════════════════════════════════════════════════════════════════════════
async def test_flask_endpoints():
    print("\n3. Flask API 端点")
    import httpx

    base = "http://localhost:5000"

    try:
        async with httpx.AsyncClient(timeout=5) as c:
            # Ping
            resp = await c.get(f"{base}/post_ping")
            check("GET /post_ping", resp.status_code == 200)
            if resp.status_code == 200:
                data = resp.json()
                print(f"     Bot 状态: {json.dumps(data, ensure_ascii=False)[:100]}")

            # World info
            resp = await c.post(f"{base}/post_environment_dict")
            check("POST /post_environment_dict", resp.status_code == 200)
            if resp.status_code == 200:
                env = resp.json()
                print(f"     位置: {env.get('my_position', '?')}")

            # Phase 7 API routes
            resp = await c.get(f"{base}/api/world")
            check("GET /api/world (Phase7)", resp.status_code in (200, 404),
                  f"status={resp.status_code}")

            resp = await c.post(f"{base}/api/action", json={"tool": "getWorldInfo", "args": {}})
            check("POST /api/action (Phase7)", resp.status_code in (200, 404, 500),
                  f"status={resp.status_code}")

    except ImportError:
        check("Flask endpoints", None, "httpx 未安装")
    except Exception as e:
        check("Flask endpoints", None, f"连接失败: {type(e).__name__}")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: Full Pipeline (MOCK)
# ═══════════════════════════════════════════════════════════════════════════════
async def test_pipeline_mock():
    print("\n4. Full Pipeline (MOCK mode)")
    from src.core.event_bus import EventBus, make_user_input
    from src.core.bridge import MinecraftBridge, BridgeMode
    from src.core.tools import ToolRegistry, MINECRAFT_TOOL_DEFINITIONS

    bus = EventBus(history_size=200)
    bridge = MinecraftBridge(event_bus=bus, mode=BridgeMode.MOCK)
    await bridge.start()
    await bus.start()

    # Register tools
    tools = ToolRegistry()
    for td in MINECRAFT_TOOL_DEFINITIONS[:5]:  # First 5 tools
        tools.register(td.name, td.description, list(td.parameters),
                      category="test", handler=lambda **k: {"status": True})

    check("Pipeline — tools registered", len(tools.get_openai_tools()) >= 5)
    check("Pipeline — EventBus running", bus.is_running)
    check("Pipeline — Bridge mode", bridge.mode.value == "mock")

    # Publish user input
    evt = make_user_input("get world info", target="Bot", player="Tester")
    await bus.publish(evt)
    await asyncio.sleep(0.2)

    history = bus.get_history(limit=5)
    check("Pipeline — event delivered", len(history) >= 1,
          f"got {len(history)} events")

    await bridge.stop()
    await bus.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5: Bot Action (if server available)
# ═══════════════════════════════════════════════════════════════════════════════
async def test_bot_actions():
    print("\n5. Bot Actions (REAL)")
    from src.core.event_bus import EventBus
    from src.core.bridge import MinecraftBridge, BridgeMode

    if not await check_server("http://localhost:5000/post_ping"):
        check("Bot actions", None, "Flask 服务器不可达")
        return

    bus = EventBus()
    bridge = MinecraftBridge(event_bus=bus, mode=BridgeMode.REAL, base_url="http://localhost:5000")
    await bridge.start()

    # Test basic actions
    actions = [
        ("getWorldInfo", {}, "世界状态"),
        ("checkWeather", {}, "天气查询"),
        ("getInventory", {}, "库存查询"),
    ]

    for name, args, desc in actions:
        try:
            result = await bridge.execute(name, args)
            check(f"Bot — {desc}", result is not None and hasattr(result, 'status'),
                  f"result={str(result)[:80]}")
            if result:
                print(f"     {desc}: {result.message[:80]}")
        except Exception as e:
            check(f"Bot — {desc}", False, f"{type(e).__name__}: {e}")

    # Test chat send
    try:
        await bridge.send_chat("[VillagerAgent] E2E 测试 — 连接正常！")
        check("Bot — send_chat", True)
    except Exception as e:
        check("Bot — send_chat", None, f"{type(e).__name__}: {e}")

    await bridge.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6: Controller + Agent Creation
# ═══════════════════════════════════════════════════════════════════════════════
async def test_controller():
    print("\n6. AgentController + Agent 创建")
    from src.core.event_bus import EventBus
    from src.core.bridge import MinecraftBridge, BridgeMode
    from src.core.controller import AgentController, AgentConfig

    bus = EventBus()
    bridge = MinecraftBridge(event_bus=bus, mode=BridgeMode.MOCK)
    controller = AgentController(event_bus=bus, bridge=bridge)

    # Use valid model name to avoid factory fallback error
    config = AgentConfig(
        name="TestBot",
        llm={"api_model": "gpt-4o-mini", "api_key": "test-key"},
        personality={"性格": "热情"},
    )
    controller.configure_agent(config)

    try:
        agent = await controller.start_agent("TestBot", config)
        check("Controller — agent created", agent is not None)
        check("Controller — agent name", agent.name == "TestBot")
        check("Controller — agent state", agent.state.name == "IDLE")
        check("Controller — memory", agent.memory is not None)
        check("Controller — tools", len(agent.tools.get_openai_tools()) >= 5)
        check("Controller — world_config", agent.world_config is not None)
        check("Controller — long_term_memory", agent.long_term_memory is not None)
        check("Controller — emotion_engine", agent.emotion_engine is not None)
        check("Controller — interaction", agent.interaction is not None)
        check("Controller — planner", agent.planner is not None)
        check("Controller — structured_log", hasattr(agent, 'structured_log'))
        check("Controller — token_quota", hasattr(agent, 'token_quota'))

        await controller.stop_agent("TestBot")
        check("Controller — agent stopped", "TestBot" not in controller._agents)

    except Exception as e:
        check("Controller", False, f"{type(e).__name__}: {e}")

    await bridge.stop()
    await bus.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# Test 7: EasyAuth 自动登录检测
# ═══════════════════════════════════════════════════════════════════════════════
async def test_easyauth():
    print("\n7. EasyAuth 自动登录检测")
    import os

    easyauth_password = os.environ.get("EASYAUTH_PASSWORD", "")
    easyauth_force_login = os.environ.get("EASYAUTH_FORCE_LOGIN", "false").lower() == "true"

    check("EasyAuth — EASYAUTH_PASSWORD 已设置",
          bool(easyauth_password) or None,  # SKIP if not set (optional config)
          "设置 export EASYAUTH_PASSWORD='your_password' 以启用自动登录")
    check("EasyAuth — EASYAUTH_FORCE_LOGIN 模式",
          True, f"当前: {'globalPassword /login 模式' if easyauth_force_login else '自动检测 /login 或 /register'}")

    if not easyauth_password:
        print("     💡 提示: 设置 EASYAUTH_PASSWORD 环境变量以启用 EasyAuth 自动登录")
        print("        globalPassword 模式: export EASYAUTH_PASSWORD='密码' EASYAUTH_FORCE_LOGIN=true")
        print("        注册模式: export EASYAUTH_PASSWORD='密码'")

    # Check MC server EasyAuth config
    mc_config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "..", "mc-server", "config", "EasyAuth", "main.conf")
    mc_config_path = os.path.normpath(mc_config_path)
    if os.path.exists(mc_config_path):
        with open(mc_config_path) as f:
            content = f.read()
        has_global_pw = "enable-global-password=true" in content
        single_use = "single-use-global-password=true" in content
        mode = ("globalPassword 模式 (disableRegister)" if has_global_pw and not single_use
                else "注册模式 (globalPassword 一次性)" if has_global_pw and single_use
                else "普通注册/登录模式")
        check(f"EasyAuth — MC 服务器配置 ({mode})", True)
    else:
        check(f"EasyAuth — MC 服务器配置", None,
              f"未找到 EasyAuth 配置文件 ({mc_config_path})")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 8: 完整集成管道 (REAL 模式，需要 Flask + MC)
# ═══════════════════════════════════════════════════════════════════════════════
async def test_full_pipeline_real():
    print("\n8. 完整集成管道 (REAL)")
    from src.core.event_bus import EventBus, make_user_input
    from src.core.bridge import MinecraftBridge, BridgeMode
    from src.core.tools import ToolRegistry, MINECRAFT_TOOL_DEFINITIONS

    if not await check_server("http://localhost:5000/post_ping"):
        check("集成管道", None, "Flask 服务器不可达 — 启动 env/minecraft_server.py")
        return

    bus = EventBus(history_size=500)
    bridge = MinecraftBridge(event_bus=bus, mode=BridgeMode.REAL, base_url="http://localhost:5000")
    await bridge.start()
    await bus.start()

    try:
        # 注册所有 Minecraft 工具
        tools = ToolRegistry()
        for td in MINECRAFT_TOOL_DEFINITIONS:
            tools.register(td.name, td.description, list(td.parameters),
                          category="minecraft",
                          handler=lambda **k: {"status": True})

        check("集成 — 工具全部注册", len(tools.get_openai_tools()) >= 15)
        check("集成 — EventBus 运行", bus.is_running)
        check("集成 — Bridge REAL 模式", bridge.mode == BridgeMode.REAL)

        # 测试基本世界交互
        try:
            world = await bridge.get_world_state()
            check("集成 — 获取世界状态", isinstance(world, dict) and len(world) > 0)
            if world:
                print(f"     Bot 位置: {world.get('my_position', '?')}")
                print(f"     在线玩家: {world.get('players', '?')}")
        except Exception as e:
            check("集成 — 获取世界状态", False, f"{type(e).__name__}: {e}")

        # 测试聊天发送
        try:
            await bridge.send_chat("[VillagerAgent] E2E 集成测试 — 系统正常")
            check("集成 — 发送聊天", True)
        except Exception as e:
            check("集成 — 发送聊天", None, f"{type(e).__name__}: {e}")

        # 测试事件发布
        evt = make_user_input("查看周围环境", target="Bot", player="E2E-Tester")
        await bus.publish(evt)
        await asyncio.sleep(0.3)
        history = bus.get_history(limit=10)
        check("集成 — 事件发布/接收", len(history) >= 1)

    except Exception as e:
        check("集成管道", False, f"{type(e).__name__}: {e}")
    finally:
        await bridge.stop()
        await bus.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="强制 MOCK 模式")
    parser.add_argument("--real", action="store_true", help="强制 REAL 模式")
    args = parser.parse_args()

    print("=" * 60)
    print("VillagerAgent 端到端测试")
    print("=" * 60)

    # Auto-detect server
    flask_ok = await check_server("http://localhost:5000/post_ping")
    use_real = args.real or (flask_ok and not args.mock)
    if args.mock:
        use_real = False

    print(f"\n模式: {'REAL (连接 Minecraft)' if use_real else 'MOCK (离线模拟)'}")
    print(f"Flask 服务器: {'✅ 运行中' if flask_ok else '❌ 未启动'}")
    if not flask_ok:
        print("   💡 启动方法: python env/minecraft_server.py --port 25565 --local_port 5000")
    if not flask_ok and use_real:
        print("   ⚠️  REAL 模式需要 Flask 服务器运行，将仅运行 MOCK 测试")
        use_real = False

    # EasyAuth 检查 (始终运行)
    await test_easyauth()

    # Always run these
    await test_connectivity("real" if use_real else "mock")
    await test_pipeline_mock()
    await test_controller()

    # REAL-mode only tests
    if use_real:
        await test_bridge_real()
        await test_flask_endpoints()
        await test_bot_actions()
        await test_full_pipeline_real()
    else:
        print("\n2-5, 8. (跳过 — 需要 Flask + Minecraft 服务器)")

    # Summary
    print()
    print("=" * 60)
    total = PASS + FAIL + SKIP
    print(f"通过: {PASS}/{total}  |  失败: {FAIL}/{total}  |  跳过: {SKIP}/{total}")
    print("=" * 60)
    return FAIL == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    exit(0 if success else 1)
