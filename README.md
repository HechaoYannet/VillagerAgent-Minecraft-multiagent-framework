# VillagerAgent — Minecraft AI 伙伴系统

基于 ACL 2024 论文 [VillagerAgent](https://arxiv.org/abs/2406.05720) 重构的游戏体验向 Agent 系统：让 LLM 驱动的 AI 伙伴陪你在 Minecraft 里聊天、干活、闲逛。

## 架构

```
main.py (FastAPI Web + asyncio 主循环)
  └─ src/core/
       ├─ controller.py   Agent 生命周期管理
       ├─ agent.py        事件驱动 Agent (IDLE→LISTENING→THINKING→ACTING)
       ├─ bridge.py       Minecraft ↔ EventBus 桥接 (REAL/MOCK/DISABLED)
       ├─ event_bus.py    优先级异步事件总线
       ├─ tools.py        17 个 Minecraft 工具 (OpenAI function calling)
       └─ ...
  └─ env/minecraft_server.py   Flask 桥 (:5000)
       └─ JSPyBridge → Mineflayer (Node.js) → Minecraft 1.21.1 服务器
```

三种 Bridge 模式（`config/default.yaml` 的 `bridge.mode` 或环境变量 `BRIDGE_MODE`）：

| 模式 | 说明 | 需要 MC 服务器? |
|---|---|---|
| `real` | 连接真实 Minecraft 服务器 | 是 |
| `mock` | 模拟世界，开发/测试用 | 否 |
| `disabled` | 纯 LLM 对话，无 MC 交互 | 否 |

## 快速开始

### 1. 安装

```bash
# Linux / macOS / WSL
./scripts/setup.sh

# Windows PowerShell
.\scripts\setup.ps1
```

安装内容：Python 依赖（`.venv`）+ Node.js 依赖（Mineflayer，复制 `js_bridge/package.json` 到根目录后 `npm install`）+ `config/secrets.yaml`。

### 2. 配置 API 密钥

编辑 `config/secrets.yaml`：

```yaml
llm:
  api_key: "sk-your-api-key"
```

或设置环境变量 `LLM_API_KEY` / `OPENAI_API_KEY`。默认模型 `deepseek-chat`（在 `config/default.yaml` 中修改 `llm.model` / `llm.api_base` 可换成任何 OpenAI 兼容 API）。

### 3. 启动

```bash
# MOCK 模式 (无需 MC 服务器, 先体验)
./scripts/run.sh --mock

# REAL 模式 (需要 Minecraft 1.21.1 服务器运行在 localhost:25565)
./scripts/run.sh

# 仅 Web 管理后台
python main.py --web-only

# 仅 Agent (需桥接已在运行)
python main.py --agent-only
```

Windows 用 `.\scripts\run.ps1 -Mock` / `.\scripts\run.ps1`。

启动后访问 Web 管理后台：http://localhost:8080

### 4. REAL 模式额外步骤

1. 启动 Minecraft 1.21.1 服务器（见下文）
2. `scripts/run.sh` 会自动先启动 Flask 桥（`env/minecraft_server.py`，:5000），等待就绪后再启动主系统
3. Agent 进入服务器后，在 MC 服务端控制台执行 `/op 伙伴`（或你的 Agent 名）授予权限
4. 游戏内聊天以 `@ai` 开头的消息会被 Agent 处理；`@disable-llm` / `@enable-llm` 可全局开关 LLM

如果 MC 服务器安装了 EasyAuth 模组，通过环境变量设置自动登录：

```bash
export EASYAUTH_PASSWORD=你的密码
export EASYAUTH_FORCE_LOGIN=true   # globalPassword 模式
```

## Minecraft 1.21.1 服务器搭建（简要）

1. 安装 Java 21+，下载 `minecraft_server.1.21.1.jar`
2. 首次运行 `java -Xmx2G -jar minecraft_server.1.21.1.jar nogui`，编辑生成的 `eula.txt` 改为 `eula=true`
3. 再次启动，确认能通过 `localhost:25565` 连接
4. 建议 `server.properties` 设置 `difficulty=peaceful`、超平坦世界便于测试

## 配置说明

`config/default.yaml` 只保留代码实际读取的键：

| 键 | 读取位置 |
|---|---|
| `minecraft.host/port` | `main.py`, `src/core/controller.py` |
| `agents.default_count/default_name_prefix` | `src/core/controller.py` |
| `llm.model/api_base/enable_thinking` | `src/llm/factory.py` |
| `web.enabled/host/port` | `main.py` |
| `logging.level/structured` | `main.py` |
| `bridge.mode` | `main.py` |

环境变量覆盖：`MINECRAFT_HOST` / `MINECRAFT_PORT` / `LLM_API_KEY` / `BRIDGE_MODE`。

## 测试

```bash
python tests/test_e2e.py --mock    # 端到端 (MOCK)
python -m pytest tests/test_all.py -v
```

## Docker

```bash
docker build -t villager-agent .
docker compose -f docker/docker-compose.yml up
```

## 致谢

原论文框架：[cnsdqd-dyb/VillagerAgent](https://github.com/cnsdqd-dyb/VillagerAgent)（ACL 2024）

```
@inproceedings{dong2024villageragent,
  title={VillagerAgent: A Graph-Based Multi-Agent Framework for Coordinating Complex Task Dependencies in Minecraft},
  author={Dong, Yubo and Zhu, Xukun and Pan, Zhengzhe and Zhu, Linchao and Yang, Yi},
  booktitle={Proceedings of the 62nd Annual Meeting of the Association for Computational Linguistics (ACL)},
  year={2024}
}
```
