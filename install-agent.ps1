# install-agent.ps1
# 一次性安装“二维机械工程图 → Siemens NX”Agent Pack。
# 内容：三个已验证 Skill（nx-engineering-drawing-reader / nx-mcp-modeling-planner /
#       nx-mcp-pipeline）+ 通用 Plan Runner（nx-mcp-plan-runner）。
# 不修改 NX_MCP-Enhanced 核心、Loader、named pipe / resident Loader 架构。
#
# 用法：
#   .\install-agent.ps1
#   .\install-agent.ps1 -DoubaoProfile "Profile 7"
#
# 参数：
#   -DoubaoProfile  手动指定 Doubao Profile 名称（Default 或 Profile *），跳过自动检测
#   -RepoRoot       仓库根目录（默认取本脚本所在目录）

[CmdletBinding()]
param(
    [string]$DoubaoProfile = "",
    [string]$RepoRoot     = ""
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------
# 0. 定位仓库
# ---------------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = $PSScriptRoot }
$skillsSource = Join-Path $RepoRoot "skills"
$runnerSource = Join-Path $RepoRoot "agent\nx-mcp-plan-runner"
$requiredSkills = @(
    "nx-engineering-drawing-reader",
    "nx-mcp-modeling-planner",
    "nx-mcp-pipeline"
)

foreach ($s in $requiredSkills) {
    if (-not (Test-Path (Join-Path $skillsSource "$s\SKILL.md"))) {
        throw "仓库缺少 Skill: $s（预期 $skillsSource\$s\SKILL.md）"
    }
}
if (-not (Test-Path (Join-Path $runnerSource "runner.py"))) {
    throw "仓库缺少 Runner: $runnerSource\runner.py"
}
Write-Host "[仓库] $RepoRoot"

# ---------------------------------------------------------------
# 1. 检测 Doubao Profile（不写死任何 Profile）
# ---------------------------------------------------------------
$doubaoRoot = Join-Path $env:LOCALAPPDATA "Doubao\User Data"
if (-not (Test-Path $doubaoRoot)) {
    throw "未找到 Doubao User Data 目录: $doubaoRoot"
}

function Get-UserSkillsRoot {
    param([string]$ProfileName)
    $profileDir = Join-Path $doubaoRoot $ProfileName
    if (-not (Test-Path $profileDir)) { return $null }
    $candidate = Join-Path $profileDir ".doubao\agent_mode\workspace\.user_skills"
    if (Test-Path $candidate) { return $candidate }
    return $null
}

$skillRoot = $null
if (-not [string]::IsNullOrWhiteSpace($DoubaoProfile)) {
    # 手动指定
    $skillRoot = Get-UserSkillsRoot -ProfileName $DoubaoProfile
    if (-not $skillRoot) {
        throw "指定的 Profile 不存在或其中没有 .doubao\agent_mode\workspace\.user_skills: $DoubaoProfile"
    }
    Write-Host "[Profile] 手动指定: $DoubaoProfile"
}
else {
    # 自动检测：仅收集“已包含 .user_skills”的 Default / Profile *
    $valid = @()
    $dirs = Get-ChildItem -Path $doubaoRoot -Directory -Force -ErrorAction SilentlyContinue
    foreach ($d in $dirs) {
        if ($d.Name -eq "Default" -or $d.Name -like "Profile*") {
            if (Get-UserSkillsRoot -ProfileName $d.Name) { $valid += $d.Name }
        }
    }
    if ($valid.Count -eq 0) {
        throw "未在 $doubaoRoot 中找到包含 .user_skills 的 Doubao Profile（Default / Profile *）"
    }
    if ($valid.Count -eq 1) {
        $chosen = $valid[0]
        Write-Host "[Profile] 仅发现一个有效 Skill 根目录，自动使用: $chosen"
    }
    else {
        # 多个：优先选择最近使用的（按 Profile 目录 LastWriteTime）
        $chosen = $valid | Sort-Object { (Get-Item (Join-Path $doubaoRoot $_)).LastWriteTime } -Descending | Select-Object -First 1
        Write-Host "[Profile] 发现多个有效 Profile，选择最近使用的: $chosen"
    }
    $skillRoot = Get-UserSkillsRoot -ProfileName $chosen
}
Write-Host "[Profile] Skill 安装目标: $skillRoot"

# ---------------------------------------------------------------
# 2. 安装三个 Skill（只覆盖同名目录，不清空 .user_skills）
# ---------------------------------------------------------------
foreach ($s in $requiredSkills) {
    $src = Join-Path $skillsSource $s
    $dst = Join-Path $skillRoot $s
    robocopy $src $dst /E /IS /NFL /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "复制 Skill 失败: $s（robocopy rc=$LASTEXITCODE）" }
    Write-Host "[Skill] 已安装: $s"
}

# ---------------------------------------------------------------
# 3. 安装 Runner（统一位置 %USERPROFILE%\NX_MCP_WORKSPACE\nx-mcp-plan-runner）
# ---------------------------------------------------------------
$runnerDst = Join-Path $env:USERPROFILE "NX_MCP_WORKSPACE\nx-mcp-plan-runner"
New-Item -ItemType Directory -Force -Path (Split-Path $runnerDst -Parent) | Out-Null
robocopy $runnerSource $runnerDst /E /IS /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) { throw "复制 Runner 失败（robocopy rc=$LASTEXITCODE）" }
Write-Host "[Runner] 已安装: $runnerDst"

# ---------------------------------------------------------------
# 4. 轻量验证（不启动 NX，不执行真实建模）
# ---------------------------------------------------------------
$fail = 0

# 4.1 三个 SKILL.md 存在 + frontmatter name 正确
foreach ($s in $requiredSkills) {
    $md = Join-Path $skillRoot "$s\SKILL.md"
    if (-not (Test-Path $md)) {
        Write-Host "[FAIL] 缺少 $s\SKILL.md"
        $fail++
    }
    else {
        $nameLine = Select-String -Path $md -Pattern '^name:\s*(\S+)' | Select-Object -First 1
        if (-not $nameLine) {
            Write-Host "[FAIL] $s 的 SKILL.md 缺少 frontmatter name"
            $fail++
        }
        else {
            $fmName = $nameLine.Matches[0].Groups[1].Value.Trim()
            if ($fmName -ne $s) {
                Write-Host "[FAIL] $s frontmatter name 不匹配: $fmName"
                $fail++
            }
            else {
                Write-Host "[OK] $s -> name=$fmName"
            }
        }
    }
}

# 4.2 Runner 关键文件存在
if (-not (Test-Path (Join-Path $runnerDst "runner.py"))) {
    Write-Host "[FAIL] runner.py 缺失"
    $fail++
}
if (-not (Test-Path (Join-Path $runnerDst "plan_schema.json"))) {
    Write-Host "[FAIL] plan_schema.json 缺失"
    $fail++
}

# 4.3 Runner 单元测试（不触 NX）
if ($fail -eq 0) {
    $oldEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    Push-Location $runnerDst
    try {
        python tests/test_plan_resolution.py 2>&1 | Out-Host
        $rc1 = $LASTEXITCODE
        python tests/test_bbox_report.py 2>&1 | Out-Host
        $rc2 = $LASTEXITCODE
    }
    finally {
        Pop-Location
        $ErrorActionPreference = $oldEAP
    }
    if ($rc1 -ne 0 -or $rc2 -ne 0) {
        Write-Host "[FAIL] Runner 单元测试未全部通过（test_plan_resolution=$rc1, test_bbox_report=$rc2）"
        $fail++
    }
    else {
        Write-Host "[OK] Runner 单元测试通过"
    }
}

if ($fail -ne 0) {
    Write-Host ""
    Write-Host "Agent Pack 安装未完成（存在 $fail 处验证失败），请检查上方 [FAIL] 输出。"
    exit 1
}

# ---------------------------------------------------------------
# 5. 完成
# ---------------------------------------------------------------
Write-Host ""
Write-Host "Agent Pack 安装成功"
Write-Host "工程图读取 Skill：已安装"
Write-Host "建模规划 Skill：已安装"
Write-Host "一键总控 Skill：已安装"
Write-Host "通用 Runner：已安装"
Write-Host ""
Write-Host "重新打开一个豆包对话后，"
Write-Host "上传二维机械工程图并发送："
Write-Host "开始建模"
Write-Host "即可使用。"
