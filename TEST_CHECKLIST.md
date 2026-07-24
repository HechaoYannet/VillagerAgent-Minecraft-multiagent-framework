# VillagerAgent 手动验证清单

## Phase 0 — 清理旧进程

```powershell
# 杀掉所有 python 进程
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 2

# 确认端口已释放
netstat -ano | findstr "5000"
netstat -ano | findstr "8080"
# 应该无输出
```

---

## Phase 1 — EasyAuth 全局密码

在 **Minecraft 服务端控制台** (`E:\otherProject\mc-server`) 执行：

```
/auth setGlobalPassword test123
```

验证配置：
```powershell
Select-String -Path "E:\otherProject\mc-server\config\EasyAuth\main.conf" -Pattern "enable-global-password|single-use-global-password"
```
期望输出：
```
enable-global-password=true    # ← 启用全局密码
single-use-global-password=false  # ← false=disableRegister, 仅允许 /login
```

---

## Phase 2 — 配置文件验证

```powershell
cd E:\otherProject\VillagerAgent-Minecraft-multiagent-framework

# 1. 模型名检查 (应为 deepseek-chat)
python -c "import yaml; c=yaml.safe_load(open('config/default.yaml')); assert c['llm']['model']=='deepseek-chat', f'错误: {c[\"llm\"][\"model\"]}'; print('✅ 模型名:', c['llm']['model'])"

# 2. YAML 语法检查
python -c "import yaml; yaml.safe_load(open('config/default.yaml')); print('✅ default.yaml OK')"
python -c "import yaml; yaml.safe_load(open('config/secrets.template.yaml')); print('✅ secrets.template.yaml OK')"

# 3. Python 语法检查
python -c "import py_compile; py_compile.compile('main.py', doraise=True); py_compile.compile('env/minecraft_server.py', doraise=True); py_compile.compile('src/web/app.py', doraise=True); py_compile.compile('tests/test_e2e.py', doraise=True); print('✅ 全部语法 OK')"

# 4. Web 应用导入检查
python -c "from src.web.app import app; print('✅ Web 应用 OK,', len(app.routes), '条路由')"
```

---

## Phase 3 — 启动 Flask Bot + EasyAuth

```powershell
cd E:\otherProject\VillagerAgent-Minecraft-multiagent-framework

$env:EASYAUTH_PASSWORD = "test123"
$env:EASYAUTH_FORCE_LOGIN = "true"

python env/minecraft_server.py --host localhost --port 25565 --local_port 5000 -U VillagerAgent
```

**期望输出**（关键行）：

```
Agent VillagerAgent login None at localhost:25565
prismarine-viewer 不可用 (缺少 canvas 模块), Web 查看器已禁用    ← 正常
[EasyAuth] 模块级注册 (force_login=True)                        ← 必现
[EasyAuth] login 事件触发, 2s 后主动发送 /login ...              ← 必现
[EasyAuth] 已发送: /login ***                                   ← 必现
```

> **注意**: `[JSE] Mineflayer detected that you are using a deprecated event (physicTick)!` 是已知无害警告。

---

## Phase 4 — 验证 Bot 连接状态

> **重要**: Windows 上 `curl` 是 PowerShell 的 `Invoke-WebRequest` 别名，请使用 `curl.exe` 或 `Invoke-RestMethod`。

```powershell
# 方法 A: 使用 curl.exe (真正的 curl)
curl.exe http://localhost:5000/post_ping

# 方法 B: 使用 Invoke-RestMethod (PowerShell 原生)
Invoke-RestMethod -Uri http://localhost:5000/post_ping
```

**✅ 期望**:
```json
{"message":"pong","status":true}
```

**❌ 如果是**:
```json
{"message":"timeout","status":false}
```
→ 回到 Phase 3，检查 EasyAuth 密码是否正确，MC 服务端是否在线。

---

## Phase 5 — Flask API 端点验证

> **注意**: 以下命令使用 `Invoke-RestMethod`（PowerShell 原生），避免 `curl` 别名问题。

```powershell
# 1. 世界状态 (应返回 bot 位置、生命值等信息)
Invoke-RestMethod -Method POST -Uri http://localhost:5000/post_environment_dict

# 2. 实体查询
$body = @{name=""} | ConvertTo-Json
Invoke-RestMethod -Method POST -Uri http://localhost:5000/post_entity -Body $body -ContentType "application/json"

# 3. 发送聊天消息
$body = @{msg="[VillagerAgent] 手动验证 - 连接正常"} | ConvertTo-Json
Invoke-RestMethod -Method POST -Uri http://localhost:5000/post_chat -Body $body -ContentType "application/json"

# 4. Phase 7 兼容 API
Invoke-RestMethod -Uri http://localhost:5000/api/world
```

> ⚠️ 如果 API 返回异常且 MC 服务端日志出现 "Player already online"，说明 `log_activity` 装饰器触发了 bot 重建冲突。
> 此时需回到 Phase 0 全部重来一次。

---

## Phase 6 — Web 管理后台

**启动 Web** (新开终端)：
```powershell
cd E:\otherProject\VillagerAgent-Minecraft-multiagent-framework
python main.py --web-only
```

打开浏览器访问：

| URL | 验证内容 |
|-----|---------|
| `http://localhost:8080` | 控制台仪表盘 — 页面加载，无空白/报错 |
| `http://localhost:8080/agents` | Agent 管理列表 |
| `http://localhost:8080/chat` | 对话界面 |
| `http://localhost:8080/logs` | 日志查看器 |
| `http://localhost:8080/health` | 健康检查 → `{"status":"ok"}` |

---

## Phase 7 — E2E 测试

```powershell
cd E:\otherProject\VillagerAgent-Minecraft-multiagent-framework

# Mock 模式 (不依赖服务器)
python tests/test_e2e.py --mock
# 期望: 20+/22 通过

# REAL 模式 (需要 Phase 3 的 bot 在运行)
$env:EASYAUTH_PASSWORD = "test123"
$env:EASYAUTH_FORCE_LOGIN = "true"
python tests/test_e2e.py --real
# 期望: 34+/36 通过
```

**REAL 模式关键测试项**：
| 测试 | 期望 |
|------|------|
| Bridge REAL — world state | ✅ 输出 bot 位置和生命值 |
| Bridge REAL — position | ✅ `[x, y, z]` |
| GET /post_ping | ✅ `{"message":"pong","status":true}` |
| Bot — 世界状态 | ✅ |
| Bot — 天气查询 | ✅ |
| Bot — send_chat | ✅ |

---

## Phase 8 — 单元测试

```powershell
python -m pytest tests/test_all.py -v
# 期望: 5 passed, 6 skipped
```

---

## Phase 9 — 清理

```powershell
# Ctrl+C 停止 Phase 3 的 bot
# Ctrl+C 停止 Phase 6 的 web (如果启动了)

# 确认端口释放
netstat -ano | findstr "5000"
netstat -ano | findstr "8080"
```

---

## 结果记录

| Phase | 项目 | 结果 |
|-------|------|------|
| 0 | 旧进程清理 | ☐ |
| 1 | EasyAuth 密码设置 | ☐ |
| 2 | 配置文件验证 | ☐ |
| 3 | Bot 启动 + EasyAuth | ☐ |
| 4 | Bot ping 验证 | ☐ |
| 5 | Flask API 端点 | ☐ |
| 6 | Web 管理后台 | ☐ |
| 7 | E2E Mock | ☐ |
| 7 | E2E REAL | ☐ |
| 8 | 单元测试 | ☐ |
| 9 | 清理 | ☐ |

---

## 已知无害警告

| 警告 | 说明 |
|------|------|
| `prismarine-viewer 不可用 (缺少 canvas 模块)` | Windows 上 `canvas` npm 包编译失败，不影响 bot 功能 |
| `[JSE] Mineflayer detected that you are using a deprecated event (physicTick)` | mineflayer v4 重命名了事件名，不影响功能 |
