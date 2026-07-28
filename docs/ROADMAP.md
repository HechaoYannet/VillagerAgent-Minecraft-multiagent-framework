# 未来清单 (Roadmap)

本次清理（2026-07）从代码库中移除了以下功能痕迹。它们在当时是**只有 Schema/配置、没有真实实现**的"幻觉代码"。如果未来要恢复，请以此清单为准，**先实现、再接配置、最后写文档**，不要反向操作。

---

## 1. Minecraft 工具（已从 `src/core/tools.py` 删除的 11 个）

删除原因：在 LLM 工具表里注册了 OpenAI Schema，但 `env/minecraft_server.py` 的 `/api/action` 路由没有对应实现，REAL 模式下调用一律返回 `400 未知工具`。

| 工具 | 设想功能 | 实现要点 |
|---|---|---|
| `guardArea` | 区域巡逻 + 主动攻击怪物 | 需 Mineflayer pvp 插件持续状态机，非单次 HTTP 调用 |
| `sortInventory` | 库存排序整理 | Mineflayer 原生无排序 API，需模拟点击 |
| `autoFish` | 持续自动钓鱼 | `env/env_api.py` 有 fishing 片段但未接路由 |
| `buildShape` | 几何形状建造 (圆/方/线/平台) | 需独立的建造规划模块 |
| `copyBuild` | 扫描源区域并复制到新位置 | 需要 3D 结构扫描 + 重建，工作量大 |
| `landscaping` | 大范围地形平整/挖掘/填充 | 需批量方块操作队列 |
| `pathBuild` | 两点间自动铺路 | 寻路 + 放置结合 |
| `takeScreenshot` | Agent 视角截图 | prismarine-viewer 在 Windows 编译失败，已移 optional |
| `checkWeather` | 天气/时间查询 | **最易实现**: `/api/world` 已返回天气字段，加一行路由映射即可 |
| `countNearby` | 统计附近实体/方块数量 | 可在 `getWorldInfo` 结果上做聚合 |
| `escort` | 护送玩家到目的地 | 需 follow + combat 组合状态机 |

恢复步骤：① `env/minecraft_server.py` 的 `/api/action` `route_map` + `param_map` 加实现 → ② `src/core/tools.py` 加 Schema → ③ `tests/test_all.py` 工具计数断言 +1。

## 2. Token 配额系统（已删除 `src/core/token_quota.py`）

删除原因：`AgentController` 构造了 `TokenQuotaManager` 并赋给 `agent.token_quota`，但 `agent.py` 从未读取，配额检查/限流从未生效。配置段 `quota.*` 同步删除。

恢复要点：在 `agent.py` 的 LLM 调用前后接入 `check()` / `record_usage()`，并提供配额耗尽时的降级回复。

## 3. GitHub 配置热重载（已删除 `src/core/hot_reload.py`）

删除原因：`main.py` 调用 `controller.run_forever()` 时从不传 `hot_reload_config`，启用路径是断的。对游戏 Agent 属过度设计。

## 4. 工具调用优化器（已删除 `src/core/tool_optimizer.py`）

删除原因：全仓库零实例化。内含硬编码的合成配方表（`CRAFTING_CHAINS`）和工具↔方块映射，属于易过时的幻觉温床。如需"批量挖掘"等能力，建议在 LLM prompt 层做规划，而非硬编码。

## 5. 向量长期记忆（chromadb / faiss）

删除原因：`config/default.yaml`、`pyproject [memory]`、`docker-compose.yml` 的 chromadb 服务全部就位，但仓库中**零 `import chromadb`**。`src/core/long_term_memory.py` 目前用 JSON 文件持久化，已够用。

## 6. Web 管理后台鉴权

删除原因：`secrets.template.yaml` 曾声明 `web.username/password/jwt_secret`，但 `src/web/` 无任何认证代码。Web 后台当前**无鉴权**，仅建议在可信内网使用。

## 7. Google Gemini 模型支持（已删除 `src/llm/google_model.py`）

删除原因：该文件是旧 `model/google_model.py` 的拷贝，未实现新的 `AsyncChatModel` 接口（缺 `chat()` / `chat_with_tools()`），且反向依赖已删除的 `model/` 包。如需 Gemini，请按 `src/llm/base.py` 的 `AsyncChatModel` ABC 重新实现。

---

## 已知无害警告

| 警告 | 说明 |
|---|---|
| `prismarine-viewer 不可用 (缺少 canvas 模块)` | Windows 上 `canvas` npm 包编译失败，已移入 `optionalDependencies`，不影响 Bot 功能 |
| `[JSE] Mineflayer detected that you are using a deprecated event (physicTick)` | mineflayer v4 重命名事件，不影响功能 |
