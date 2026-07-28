# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

VillagerAgent is an LLM-driven Minecraft AI companion system based on the ACL 2024 [paper](https://arxiv.org/abs/2406.05720). Agents join a Minecraft server, respond to `@ai` chat commands, and execute actions (move, mine, craft, etc.) via a Mineflayer bot.

## Commands

```bash
# Setup (creates .venv, installs Python + Node.js deps, generates config/secrets.yaml)
./scripts/setup.sh           # Linux/macOS/WSL
.\scripts\setup.ps1          # Windows PowerShell
.\scripts\setup.ps1 -Dev     # includes pytest, black, ruff, mypy

# Run
./scripts/run.sh --mock      # MOCK mode (no Minecraft server needed)
./scripts/run.sh             # REAL mode (needs MC server + Flask bridge)
python main.py --web-only    # Web dashboard only
python main.py --agent-only  # Agent only (bridge must already be running)

# Tests
python -m pytest tests/test_all.py -v     # Unit/integration tests
python tests/test_e2e.py --mock           # End-to-end (MOCK)
python tests/test_e2e.py --real           # End-to-end (REAL, needs live MC server)

# Lint/format
black src/ tests/
ruff check src/ tests/
mypy src/
```

## Architecture

### Entry point (`main.py`)

`main.py` loads YAML config (merged with `config/secrets.yaml`, overridden by env vars), then starts two asyncio tasks: the Agent system (`run_agent`) and the FastAPI web dashboard (`run_web`). When both run together, a single `AgentController` is shared between them. Supports `--web-only`, `--agent-only`, `--mock` (via scripts), and `--debug` flags.

### Core event-driven framework (`src/core/`)

**EventBus** (`event_bus.py`) — Priority async pub/sub bus. Event priority order (highest first): `INTERRUPT > USER_INPUT > CHAT > WORLD_CHANGE > TIMER > AGENT_STATE > SYSTEM`. One bus is shared by all agents and the bridge.

**Agent** (`agent.py`) — State machine: `IDLE → LISTENING → THINKING → ACTING → REFLECTING → IDLE`. The agent subscribes to the EventBus for `USER_INPUT`, `INTERRUPT`, `CHAT`, and `TIMER` events. The core loop (`_process_user_input`) implements a tool-call loop: send messages+tools to LLM → if text reply, respond and finish → if tool_calls, execute via bridge, feed results back, loop. Max `max_tool_steps` iterations (default 8). Interrupts are checked between every step.

**Controller** (`controller.py`) — Creates the shared EventBus and MinecraftBridge, then spawns one or more Agent instances as asyncio Tasks. Runs a periodic timer loop that publishes `TIMER` events for world-state polling and proactive behavior checks.

**Bridge** (`bridge.py`) — Three modes:
- `REAL`: HTTP calls to Flask server (`env/minecraft_server.py` :5000) → Mineflayer → Minecraft
- `MOCK`: Simulated world state with randomized position jitter; tools return mock success
- `DISABLED`: Pure LLM chat, no Minecraft interaction

The bridge also handles `@ai` message routing, `@enable-llm`/`@disable-llm` toggles, and world-state polling.

**Tools** (`tools.py`) — `ToolRegistry` manages 17 Minecraft tools registered as OpenAI function-calling JSON schemas. Tools are defined with Chinese descriptions; categories include movement, block manipulation, inventory, entity interaction, world query, and system. Execution is delegated to `bridge.execute()`.

**ConversationMemory** (`conversation.py`) — Short-term message history with a ring buffer (default 24 messages). Builds the LLM message list: system prompt (with personality + Minecraft knowledge card) → world state snapshot → truncated history → current user input. Tool results are capped at 800 chars.

**Planning** (`planning.py`) — `TaskPlanner` does a pre-execution LLM call to generate a structured JSON plan (prerequisites, steps, estimated steps, confidence, fallback). Skipped for casual chat and obvious single-step commands. Planning result is injected as context into the agent's message list.

**WorldConfig** (`world_config.py`) — Markdown files at `data/world/{name}.md` serve as per-world "CLAUDE.md", storing locations, rules, preferences, and event history. Loaded at agent startup and injected into the system prompt.

**LongTermMemory** (`long_term_memory.py`) — JSON-persisted memory (`data/memory/{world}.json`) for timeline events, known locations, and player profiles. Survives across sessions.

**Interaction** (`interaction.py`) — Response formatting (prefixes, emojis, progress bars), proactive chat triggers on long idle, and response mode selection (command vs chat vs status report).

### LLM layer (`src/llm/`)

- `base.py` — Abstract interfaces: `Message` types (System/User/Assistant/Tool), `ChatResult`, `ToolCall`, `TokenUsage`, `ToolParameter`. All frozen dataclasses.
- `openai_compat.py` — `OpenAICompatClient`: unified client for any OpenAI-compatible API (DeepSeek, GPT, Qwen, vLLM). Supports reasoning_content extraction (DeepSeek thinking tokens), native function calling, streaming. Configures `enable_thinking` per model.
- `factory.py` — Model factory routing: `gemini` models → Google client; everything else → `OpenAICompatClient`. Reads `model`, `api_base`, `api_key`, `enable_thinking` from config.
- `retry.py` — Exponential backoff with jitter + circuit breaker pattern for LLM API calls.

### Minecraft bridge server (`env/minecraft_server.py`)

A Flask HTTP server that wraps a Mineflayer (Node.js) bot via JSPyBridge. Exposes:
- `GET /api/world` — world state (position, health, food, inventory, nearby entities)
- `GET /api/chat/new` — new chat messages since last poll
- `POST /api/action` — execute a bot action (move, mine, place, craft, etc.)
- `POST /api/chat/send` — send a chat message

### Web dashboard (`src/web/`)

FastAPI app on :8080 with Jinja2 templates. Routes: `/` dashboard, `/agents`, `/chat`, `/logs`. REST API at `/api/agents`, WebSocket at `/ws/agent/{name}` for real-time state streaming.

### Prompts and personality (`src/prompts/`)

- `system_prompts.py` — Core agent system prompt, Minecraft knowledge card, personality text builder. Uses `{{placeholders}}` for template variable injection.
- `personality.py` — Personality trait definitions and behavior modifiers.
- `emotions.py` — `EmotionEngine`: mood state machine (valence + arousal) that decays over time, spikes on events. Generates prompt fragments for LLM context.

### Configuration

`config/default.yaml` — Main config. Key sections: `minecraft` (host/port), `agents` (count, name, token limits, planning toggle), `llm` (model, api_base, enable_thinking), `web` (host/port), `logging`, `bridge` (mode, flask host/port).

`config/secrets.yaml` — API keys (gitignored). Template at `config/secrets.template.yaml`.

Environment variable overrides: `MINECRAFT_HOST`, `MINECRAFT_PORT`, `LLM_API_KEY` (or `OPENAI_API_KEY`), `BRIDGE_MODE`.

## Key design decisions

- **No LangChain**: Tools use raw OpenAI function-calling JSON schemas, not LangChain decorators. The LLM client is a thin wrapper around `openai.AsyncOpenAI`.
- **Agent name filtering**: In chat, only messages starting with `@ai` trigger the agent (configurable in bridge). Messages from the agent itself are filtered to prevent feedback loops.
- **Token economy**: Conversation history capped at 24 messages, tool results truncated at 800 chars, proactive behaviors use rule-based templates by default (`proactive_llm: false`), planning skipped for casual chat and single-step commands.
- **Dual server architecture**: The Flask bridge (`env/minecraft_server.py`) and FastAPI main app (`main.py`) run as separate processes. In REAL mode, the bridge must be started first (scripts handle this automatically).
- **Mock-first development**: The MOCK bridge mode enables full agent development and testing without a Minecraft server. World state is static with slight position jitter to simulate movement.
