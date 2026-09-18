# install.ps1
# NX_MCP-Enhanced + Agent Pack 一体化安装器
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -DoubaoProfile "Profile 7"

[CmdletBinding()]
param(
    [string]$DoubaoProfile = "",
    [string]$RepoRoot = "",
    [switch]$SkipAgentTests
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

Write-Host ""
Write-Host "========================================"
Write-Host " NX_MCP-Enhanced 一体化安装"
Write-Host "========================================"
Write-Host "[仓库] $RepoRoot"

# ---------------------------------------------------------------
# 1. 基础环境
# ---------------------------------------------------------------
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pythonCmd) {
    throw "未找到 Python。请先安装 Python 3.10+ 并加入 PATH。"
}
$pythonExe = $pythonCmd.Source
$pythonVersionText = (& $pythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')").Trim()
$pythonVersion = [version]$pythonVersionText
if ($pythonVersion -lt [version]"3.10.0") {
    throw "Python 版本过低：$pythonVersionText。需要 Python 3.10+。"
}
Write-Host "[环境] Python $pythonVersionText"

# ---------------------------------------------------------------
# 2. Python sidecar / venv
# ---------------------------------------------------------------
$venvDir = Join-Path $RepoRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "[Python] 创建虚拟环境..."
    & $pythonExe -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        throw "创建 Python 虚拟环境失败（exit=$LASTEXITCODE）"
    }
}
else {
    Write-Host "[Python] 复用已有虚拟环境: $venvDir"
}

Write-Host "[Python] 安装 NX_MCP-Enhanced 及依赖..."
Push-Location $RepoRoot
try {
    & $venvPython -m pip install -e ".[dev]"
    if ($LASTEXITCODE -ne 0) {
        throw "pip 安装失败（exit=$LASTEXITCODE）"
    }
}
finally {
    Pop-Location
}

& $venvPython -c "import nx_mcp; print('[OK] nx_mcp import 成功')"
if ($LASTEXITCODE -ne 0) {
    throw "nx_mcp import 验证失败"
}

# ---------------------------------------------------------------
# 3. Workspace
# ---------------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($env:NX_MCP_WORKSPACE)) {
    $workspace = Join-Path $env:USERPROFILE "NX_MCP_WORKSPACE"
}
else {
    $workspace = $env:NX_MCP_WORKSPACE
}
New-Item -ItemType Directory -Force -Path $workspace | Out-Null
$env:NX_MCP_WORKSPACE = $workspace
$env:NX_MCP_BACKEND = "auto"
Write-Host "[Workspace] $workspace"

# ---------------------------------------------------------------
# 3.1 NX user directory
# ---------------------------------------------------------------
# NX 只有在进程启动时知道 UGII_USER_DIR。旧安装器在该变量未设置时只是把
# Loader 复制到 ~/.nx_mcp_user/startup，却没有告诉 NX 去那里加载，导致首次
# 安装后 Loader 日志不存在。这里统一确定目录，并把 UGII_USER_DIR 持久化到
# 当前用户环境，同时写入当前 PowerShell 进程。
$userUgii = [Environment]::GetEnvironmentVariable("UGII_USER_DIR", "User")
$machineUgii = [Environment]::GetEnvironmentVariable("UGII_USER_DIR", "Machine")

if (-not [string]::IsNullOrWhiteSpace($env:UGII_USER_DIR)) {
    $nxUserDir = $env:UGII_USER_DIR
}
elseif (-not [string]::IsNullOrWhiteSpace($userUgii)) {
    $nxUserDir = $userUgii
}
elseif (-not [string]::IsNullOrWhiteSpace($machineUgii)) {
    $nxUserDir = $machineUgii
}
else {
    $nxUserDir = Join-Path $env:USERPROFILE ".nx_mcp_user"
}

$nxUserDir = [Environment]::ExpandEnvironmentVariables($nxUserDir)
New-Item -ItemType Directory -Force -Path $nxUserDir | Out-Null

# 如果当前用户级变量与实际使用目录不一致，则持久化。这样从开始菜单/桌面
# 新启动的 NX 也能继承正确的 UGII_USER_DIR。
if ($userUgii -ne $nxUserDir) {
    [Environment]::SetEnvironmentVariable("UGII_USER_DIR", $nxUserDir, "User")
    # setx 会通知 Windows 环境已变化；失败不影响注册表写入结果。
    try {
        & setx.exe UGII_USER_DIR "$nxUserDir" 2>&1 | Out-Null
    }
    catch {
        Write-Host "[提示] setx 通知失败，但用户级 UGII_USER_DIR 已写入。"
    }
}

$env:UGII_USER_DIR = $nxUserDir
Write-Host "[NX] UGII_USER_DIR=$nxUserDir"

# ---------------------------------------------------------------
# 4. Build + deploy resident C# Loader
# ---------------------------------------------------------------
$buildBat = Join-Path $RepoRoot "loader\build.bat"
if (-not (Test-Path $buildBat)) {
    throw "缺少 Loader 构建脚本: $buildBat"
}

Write-Host "[Loader] 构建 NX_MCP_Loader.dll..."
Push-Location (Split-Path $buildBat -Parent)
try {
    & $env:ComSpec /c "build.bat"
    if ($LASTEXITCODE -ne 0) {
        throw "Loader 构建失败（exit=$LASTEXITCODE）。请确认 Siemens NX 已安装且 NXOpen .NET 文件可被检测。"
    }
}
finally {
    Pop-Location
}

$builtLoader = Join-Path $RepoRoot "loader\NX_MCP_Loader.dll"
if (-not (Test-Path $builtLoader)) {
    throw "Loader 构建完成但未找到 DLL: $builtLoader"
}

# 始终部署到当前已经设置/持久化的 UGII_USER_DIR\startup。
$startupDir = Join-Path $nxUserDir "startup"
New-Item -ItemType Directory -Force -Path $startupDir | Out-Null

$deployedLoader = Join-Path $startupDir "NX_MCP_Loader.dll"
Copy-Item -Force $builtLoader $deployedLoader

$srcHash = (Get-FileHash $builtLoader -Algorithm SHA256).Hash
$dstHash = (Get-FileHash $deployedLoader -Algorithm SHA256).Hash
if ($srcHash -ne $dstHash) {
    throw "Loader 部署校验失败：源 DLL 与目标 DLL SHA256 不一致"
}
Write-Host "[Loader] 已部署: $deployedLoader"
Write-Host "[OK] Loader SHA256 校验通过"

# ---------------------------------------------------------------
# 5. Agent Pack（Drawing Reader + Planner + Pipeline + Runner）
# ---------------------------------------------------------------
$agentInstaller = Join-Path $RepoRoot "install-agent.ps1"
if (-not (Test-Path $agentInstaller)) {
    throw "缺少 Agent Pack 安装器: $agentInstaller"
}

Write-Host "[Agent Pack] 安装工程图自动建模能力..."
$agentParams = @{
    RepoRoot  = $RepoRoot
    PythonExe = $venvPython
}
if (-not [string]::IsNullOrWhiteSpace($DoubaoProfile)) {
    $agentParams["DoubaoProfile"] = $DoubaoProfile
}
if ($SkipAgentTests) {
    $agentParams["SkipTests"] = $true
}
& $agentInstaller @agentParams

# ---------------------------------------------------------------
# 6. 完成
# ---------------------------------------------------------------
Write-Host ""
Write-Host "========================================"
Write-Host " 一体化安装成功"
Write-Host "========================================"
Write-Host "NX_MCP-Enhanced 核心：已安装"
Write-Host "Python sidecar：已安装"
Write-Host "UGII_USER_DIR：已配置"
Write-Host "C# Loader：已构建并部署"
Write-Host "工程图读取 Skill：已安装"
Write-Host "建模规划 Skill：已安装"
Write-Host "一键总控 Skill：已安装"
Write-Host "通用 Plan Runner：已安装"
Write-Host "Workspace：$workspace"
Write-Host "NX 用户目录：$nxUserDir"
Write-Host ""

$nxRunning = Get-Process -Name ugraf -ErrorAction SilentlyContinue
if ($nxRunning) {
    Write-Host "[重要] Siemens NX 在安装前已经运行。请保存工作并重启 NX 一次，使新的 UGII_USER_DIR 和 Loader DLL 生效。"
}
else {
    Write-Host "[下一步] 启动 Siemens NX。新进程会读取 UGII_USER_DIR 并自动加载 Loader。"
}

Write-Host "然后重新打开/新建一个豆包对话，上传二维机械工程图并发送："
Write-Host "开始建模"
Write-Host ""
