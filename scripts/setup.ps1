# =============================================
# VillagerAgent — 一键安装 (Windows PowerShell)
# =============================================
# 用法:
#   .\scripts\setup.ps1              # 基础安装
#   .\scripts\setup.ps1 -Dev         # 含开发工具 (pytest/black/ruff/mypy)
#
# 注意: 首次运行可能需要:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# =============================================

param(
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
Set-Location $ProjectDir

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "🏰 VillagerAgent — 环境安装" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ═══════════════════════════════════════════════════════════════════
# 1. 检查 Python
# ═══════════════════════════════════════════════════════════════════
Write-Host "[INFO] 检查 Python..." -ForegroundColor Cyan
$PythonCmd = $null
foreach ($cmd in @("python3.12", "python3.11", "python3", "python")) {
    $found = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($found) {
        $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        $major = [int](& $cmd -c "import sys; print(sys.version_info.major)")
        $minor = [int](& $cmd -c "import sys; print(sys.version_info.minor)")
        if ($major -ge 3 -and $minor -ge 11) {
            $PythonCmd = $cmd
            Write-Host "[OK]   找到 Python $ver ($($found.Source))" -ForegroundColor Green
            break
        }
    }
}

if (-not $PythonCmd) {
    Write-Host "[ERROR] 需要 Python >= 3.11, 但未找到" -ForegroundColor Red
    Write-Host "   安装: winget install Python.Python.3.11"
    Write-Host "   或:   https://www.python.org/downloads/"
    exit 1
}

# ═══════════════════════════════════════════════════════════════════
# 2. 检查 Node.js
# ═══════════════════════════════════════════════════════════════════
Write-Host "[INFO] 检查 Node.js (JSPyBridge 需要)..." -ForegroundColor Cyan
$NodeCmd = $null
$found = Get-Command node -ErrorAction SilentlyContinue
if ($found) {
    $NodeCmd = "node"
    $ver = & node --version 2>$null
    if ($ver) {
        $major = [int]($ver -replace 'v', '').Split('.')[0]
        if ($major -ge 18) {
            Write-Host "[OK]   找到 Node.js $ver ($($found.Source))" -ForegroundColor Green
        } else {
            Write-Host "[WARN]  Node.js $ver 版本较旧, 建议 >= 18" -ForegroundColor Yellow
        }
    }
}

if (-not $NodeCmd) {
    Write-Host "[WARN]  未找到 Node.js, JSPyBridge 需要 Node.js" -ForegroundColor Yellow
    Write-Host "   安装: winget install OpenJS.NodeJS.LTS"
    Write-Host "   或:   https://nodejs.org/"
    $response = Read-Host "是否继续安装 (Python 依赖仍可安装)? [y/N]"
    if ($response -notmatch '^[Yy]') {
        exit 1
    }
}

# ═══════════════════════════════════════════════════════════════════
# 3. 创建虚拟环境
# ═══════════════════════════════════════════════════════════════════
$VenvDir = Join-Path $ProjectDir ".venv"
if (Test-Path $VenvDir) {
    Write-Host "[INFO] 虚拟环境已存在: $VenvDir" -ForegroundColor Cyan
} else {
    Write-Host "[INFO] 创建虚拟环境..." -ForegroundColor Cyan
    & $PythonCmd -m venv $VenvDir
    Write-Host "[OK]   虚拟环境已创建" -ForegroundColor Green
}

# 激活
$ActivateScript = Join-Path $VenvDir "Scripts" "Activate.ps1"
if (Test-Path $ActivateScript) {
    . $ActivateScript
} else {
    $ActivateScript = Join-Path $VenvDir "bin" "Activate.ps1"
    if (Test-Path $ActivateScript) {
        . $ActivateScript
    }
}
Write-Host "[OK]   虚拟环境已激活" -ForegroundColor Green

# 升级 pip
pip install --upgrade pip --quiet

# ═══════════════════════════════════════════════════════════════════
# 4. 安装 Python 依赖
# ═══════════════════════════════════════════════════════════════════
Write-Host "[INFO] 安装 Python 依赖..." -ForegroundColor Cyan

if ($Dev) {
    pip install -e ".[dev]" --quiet
    Write-Host "[OK]   已安装: 核心 + 开发工具" -ForegroundColor Green
} else {
    pip install -e "." --quiet
    Write-Host "[OK]   已安装: 核心依赖" -ForegroundColor Green
}

# ═══════════════════════════════════════════════════════════════════
# 5. 安装 Node.js 依赖 (JSPyBridge 桥接)
# ═══════════════════════════════════════════════════════════════════
# package.json 源文件在 js_bridge/, 复制到根目录后安装
# (JSPyBridge require() 从工作目录解析 node_modules)
if ($NodeCmd) {
    Write-Host "[INFO] 安装 Node.js 依赖 (Mineflayer)..." -ForegroundColor Cyan
    # 复制 js_bridge/package.json 到根目录
    Copy-Item "js_bridge/package.json" "package.json" -Force
    # prismarine-viewer / socks5-client 是 optionalDependencies, 编译失败不会阻塞
    npm install --production --silent 2>$null
    if ($LASTEXITCODE -ne 0) {
        npm install --production
    }
    Write-Host "[OK]   Node.js 依赖已安装" -ForegroundColor Green
}

# ═══════════════════════════════════════════════════════════════════
# 6. 配置文件
# ═══════════════════════════════════════════════════════════════════
$SecretsFile = Join-Path $ProjectDir "config" "secrets.yaml"
$TemplateFile = Join-Path $ProjectDir "config" "secrets.template.yaml"
if (-not (Test-Path $SecretsFile)) {
    if (Test-Path $TemplateFile) {
        Copy-Item $TemplateFile $SecretsFile
        Write-Host "[OK]   已创建 config/secrets.yaml (从模板)" -ForegroundColor Green
        Write-Host "[WARN]  请编辑 config/secrets.yaml 填入你的 API 密钥!" -ForegroundColor Yellow
    }
} else {
    Write-Host "[OK]   config/secrets.yaml 已存在" -ForegroundColor Green
}

# ═══════════════════════════════════════════════════════════════════
# 7. 运行时目录
# ═══════════════════════════════════════════════════════════════════
$dirs = @("logs", "data/world", "data/memory", ".cache")
foreach ($d in $dirs) {
    $full = Join-Path $ProjectDir $d
    if (-not (Test-Path $full)) {
        New-Item -ItemType Directory -Path $full -Force | Out-Null
    }
}
Write-Host "[OK]   运行时目录已创建" -ForegroundColor Green

# ═══════════════════════════════════════════════════════════════════
# 完成
# ═══════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "✅ 安装完成!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "下一步:"
Write-Host "  1. 编辑 config/secrets.yaml 填入 API 密钥"
Write-Host "  2. 确保 Minecraft 服务器已启动"
Write-Host "  3. 运行: .\scripts\run.ps1"
Write-Host ""
Write-Host "或手动:"
Write-Host "  2. .\.venv\Scripts\Activate.ps1"
Write-Host "  3. python env/minecraft_server.py -H <MC_HOST> -P 25565 -LP 5000 -U VillagerAgent"
Write-Host "  4. 等桥接就绪后: python main.py"
Write-Host ""
Write-Host "环境变量 (可选):"
Write-Host '  $env:MINECRAFT_HOST = "你的MC服务器IP"'
Write-Host '  $env:LLM_API_KEY = "sk-your-api-key"'
Write-Host ""
