# =============================================
# VillagerAgent — 启动 (Windows PowerShell)
# =============================================
# 用法:
#   .\scripts\run.ps1                        # 使用默认 config
#   .\scripts\run.ps1 -AgentOnly             # 仅 Agent (手动启动桥接)
#   .\scripts\run.ps1 -WebOnly               # 仅 Web 后台
#   .\scripts\run.ps1 -Mock                  # MOCK 模式 (无需 MC 服务器)
#
# 环境变量:
#   $env:MINECRAFT_HOST = "你的MC服务器IP"
#   $env:LLM_API_KEY = "sk-your-api-key"
# =============================================

param(
    [switch]$AgentOnly,
    [switch]$WebOnly,
    [switch]$Mock,
    [switch]$Disabled,
    [switch]$Debug,
    [string]$Config = "config/default.yaml"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
Set-Location $ProjectDir

# ── 默认值 ──
$MinecraftHost = if ($env:MINECRAFT_HOST) { $env:MINECRAFT_HOST } else { "localhost" }
$MinecraftPort = if ($env:MINECRAFT_PORT) { $env:MINECRAFT_PORT } else { "25565" }
$BridgePort    = if ($env:BRIDGE_PORT)    { $env:BRIDGE_PORT    } else { "5000" }
$WebPort       = if ($env:WEB_PORT)       { $env:WEB_PORT       } else { "8080" }
$AgentUsername = if ($env:AGENT_USERNAME) { $env:AGENT_USERNAME } else { "VillagerAgent" }
$BridgeMode    = "real"

if ($Mock)     { $BridgeMode = "mock" }
if ($Disabled) { $BridgeMode = "disabled" }

# ═══════════════════════════════════════════════════════════════════
# 激活虚拟环境
# ═══════════════════════════════════════════════════════════════════
$VenvDir = Join-Path $ProjectDir ".venv"
$ActivateScript = Join-Path $VenvDir "Scripts" "Activate.ps1"
if (-not (Test-Path $ActivateScript)) {
    $ActivateScript = Join-Path $VenvDir "bin" "Activate.ps1"
}
if (Test-Path $ActivateScript) {
    . $ActivateScript
} else {
    Write-Host "[ERROR] 虚拟环境未找到, 请先运行: .\scripts\setup.ps1" -ForegroundColor Red
    exit 1
}

# 获取 venv 中 Python 的绝对路径 (Start-Process 需要)
$VenvPython = Join-Path $VenvDir "Scripts" "python.exe"
if (-not (Test-Path $VenvPython)) {
    $VenvPython = Join-Path $VenvDir "bin" "python"
}
if (-not (Test-Path $VenvPython)) {
    $VenvPython = (Get-Command python).Source
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "🏰 VillagerAgent — 启动" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Minecraft:   ${MinecraftHost}:${MinecraftPort}"
Write-Host "桥接端口:    ${BridgePort}"
Write-Host "Web 端口:    ${WebPort}"
Write-Host "Bridge 模式: ${BridgeMode}"
Write-Host "Agent 名称:  ${AgentUsername}"
Write-Host "========================================"
Write-Host ""

# ═══════════════════════════════════════════════════════════════════
# 启动桥接服务器 (除非 -WebOnly / -Mock / -Disabled)
# ═══════════════════════════════════════════════════════════════════
$BridgeProcess = $null
$MainArgs = @()

if ($AgentOnly)   { $MainArgs += "--agent-only" }
if ($WebOnly)     { $MainArgs += "--web-only" }
if ($Debug)       { $MainArgs += "--debug" }
if ($Config)      { $MainArgs += "--config"; $MainArgs += $Config }

if ((-not $WebOnly) -and ($BridgeMode -eq "real")) {
    Write-Host "[INFO] [1/3] 启动 Minecraft 桥接服务器..." -ForegroundColor Cyan

    # 确保日志目录存在
    $LogDir = Join-Path $ProjectDir "logs"
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

    $BridgeLogFile = Join-Path $LogDir "bridge.log"
    $BridgeErrFile = Join-Path $LogDir "bridge_error.log"

    # 使用 Start-Process 在后台启动, -PassThru 返回进程对象
    $BridgeProcess = Start-Process -FilePath $VenvPython `
        -ArgumentList @(
            "env/minecraft_server.py",
            "--host", $MinecraftHost,
            "--port", $MinecraftPort,
            "--local_port", $BridgePort,
            "-U", $AgentUsername
        ) `
        -NoNewWindow `
        -PassThru `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $BridgeLogFile `
        -RedirectStandardError $BridgeErrFile

    Write-Host "        桥接服务器 PID: $($BridgeProcess.Id)" -ForegroundColor DarkGray
    Write-Host "[INFO] [2/3] 等待桥接服务器就绪..." -ForegroundColor Cyan

    # 等待桥接服务器就绪 (最多 120 秒)
    $MaxRetries = 60
    $Ready = $false
    for ($i = 0; $i -lt $MaxRetries; $i++) {
        # 检查进程是否还活着
        if ($BridgeProcess.HasExited) {
            Write-Host "[ERROR] 桥接服务器意外退出 (exit code: $($BridgeProcess.ExitCode))" -ForegroundColor Red
            Write-Host "        查看日志: $BridgeLogFile" -ForegroundColor DarkGray
            exit 1
        }
        # 检查端口
        try {
            $Socket = New-Object System.Net.Sockets.TcpClient
            $Socket.Connect("localhost", [int]$BridgePort)
            $Socket.Close()
            $Ready = $true
            break
        } catch {
            Start-Sleep -Seconds 2
        }
    }

    if (-not $Ready) {
        Write-Host "[ERROR] 桥接服务器启动超时 (120s)" -ForegroundColor Red
        if (-not $BridgeProcess.HasExited) { $BridgeProcess.Kill() }
        exit 1
    }

    Write-Host "[OK]   桥接服务器就绪" -ForegroundColor Green
    Write-Host ""
}

# ═══════════════════════════════════════════════════════════════════
# 启动主程序
# ═══════════════════════════════════════════════════════════════════
Write-Host "[INFO] [3/3] 启动 VillagerAgent 主系统..." -ForegroundColor Cyan
Write-Host ""

# 清理桥接进程的 helper
function Stop-BridgeProcess {
    if ($BridgeProcess -and (-not $BridgeProcess.HasExited)) {
        Write-Host ""
        Write-Host "[INFO] 正在关闭桥接服务器 (PID: $($BridgeProcess.Id))..." -ForegroundColor Cyan
        $BridgeProcess.Kill()
        $BridgeProcess.WaitForExit(5000) | Out-Null
        Write-Host "[INFO] VillagerAgent 已关闭" -ForegroundColor Cyan
    }
}

# 使用 try/finally 确保 Ctrl+C 和正常退出都能清理
try {
    $env:BRIDGE_MODE = $BridgeMode
    & python main.py $MainArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[WARN] main.py 退出码: $LASTEXITCODE" -ForegroundColor Yellow
    }
} finally {
    Stop-BridgeProcess
}
