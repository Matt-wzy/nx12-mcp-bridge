# install-agent.ps1
# 安装 NX Agent Pack：文字描述建模 + 二维机械工程图自动建模。
# 正常用户请优先运行根目录 install.ps1；本脚本也可单独用于重装 Agent Pack。

[CmdletBinding()]
param(
    [string]$DoubaoProfile = "",
    [string]$RepoRoot = "",
    [string]$PythonExe = "",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------
# 0. 定位仓库
# ---------------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = $PSScriptRoot }
$RepoRoot = (Resolve-Path $RepoRoot).Path
$skillsSource = Join-Path $RepoRoot "skills"
$runnerSource = Join-Path $RepoRoot "agent\nx-mcp-plan-runner"
$requiredSkills = @(
    "nx-modeling",
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
# 1. 检测 Doubao Profile
# ---------------------------------------------------------------
$doubaoRoot = Join-Path $env:LOCALAPPDATA "Doubao\User Data"
if (-not (Test-Path $doubaoRoot)) {
    throw "未找到 Doubao User Data 目录: $doubaoRoot。请先安装并启动一次豆包电脑版。"
}

function Get-UserSkillsPath {
    param([string]$ProfileName)
    $profileDir = Join-Path $doubaoRoot $ProfileName
    if (-not (Test-Path $profileDir)) { return $null }
    return (Join-Path $profileDir ".doubao\agent_mode\workspace\.user_skills")
}

$skillRoot = $null
if (-not [string]::IsNullOrWhiteSpace($DoubaoProfile)) {
    $skillRoot = Get-UserSkillsPath -ProfileName $DoubaoProfile
    if (-not $skillRoot) {
        throw "指定的 Doubao Profile 不存在: $DoubaoProfile"
    }
    Write-Host "[Profile] 手动指定: $DoubaoProfile"
}
else {
    $dirs = Get-ChildItem -Path $doubaoRoot -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq "Default" -or $_.Name -like "Profile*" }

    if (-not $dirs -or $dirs.Count -eq 0) {
        throw "未在 $doubaoRoot 中找到 Default / Profile *"
    }

    # 优先使用已经存在 .user_skills 的 Profile；否则选择最近使用的 Profile 并创建目录。
    $withSkills = @()
    foreach ($d in $dirs) {
        $candidate = Get-UserSkillsPath -ProfileName $d.Name
        if ($candidate -and (Test-Path $candidate)) {
            $withSkills += $d
        }
    }

    if ($withSkills.Count -gt 0) {
        $chosenDir = $withSkills | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        Write-Host "[Profile] 使用最近的有效 Skill Profile: $($chosenDir.Name)"
    }
    else {
        $chosenDir = $dirs | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        Write-Host "[Profile] 尚无 .user_skills，选择最近使用的 Profile 并初始化: $($chosenDir.Name)"
    }
    $skillRoot = Get-UserSkillsPath -ProfileName $chosenDir.Name
}

New-Item -ItemType Directory -Force -Path $skillRoot | Out-Null
Write-Host "[Profile] Skill 安装目标: $skillRoot"

# ---------------------------------------------------------------
# 2. 安装四个 Skill（只覆盖同名目录，不清空 .user_skills）
# ---------------------------------------------------------------
foreach ($s in $requiredSkills) {
    $src = Join-Path $skillsSource $s
    $dst = Join-Path $skillRoot $s
    robocopy $src $dst /E /IS /NFL /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "复制 Skill 失败: $s（robocopy rc=$LASTEXITCODE）" }
    Write-Host "[Skill] 已安装: $s"
}

# ---------------------------------------------------------------
# 3. 安装 Runner
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

if (-not (Test-Path (Join-Path $runnerDst "runner.py"))) {
    Write-Host "[FAIL] runner.py 缺失"
    $fail++
}
if (-not (Test-Path (Join-Path $runnerDst "plan_schema.json"))) {
    Write-Host "[FAIL] plan_schema.json 缺失"
    $fail++
}

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $repoVenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $repoVenvPython) {
        $PythonExe = $repoVenvPython
    }
    else {
        $pythonCmd = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($pythonCmd) { $PythonExe = $pythonCmd.Source }
    }
}

if (-not $SkipTests -and $fail -eq 0) {
    if ([string]::IsNullOrWhiteSpace($PythonExe) -or -not (Test-Path $PythonExe)) {
        Write-Host "[FAIL] 未找到可用于 Runner 测试的 Python: $PythonExe"
        $fail++
    }
    else {
        $oldEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        Push-Location $runnerDst
        try {
            & $PythonExe tests/test_plan_resolution.py 2>&1 | Out-Host
            $rc1 = $LASTEXITCODE
            & $PythonExe tests/test_bbox_report.py 2>&1 | Out-Host
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
}
elseif ($SkipTests) {
    Write-Host "[SKIP] Runner 单元测试已跳过"
}

if ($fail -ne 0) {
    Write-Host ""
    Write-Host "Agent Pack 安装未完成（存在 $fail 处验证失败），请检查上方 [FAIL] 输出。"
    exit 1
}

Write-Host ""
Write-Host "Agent Pack 安装成功"
Write-Host "文字建模 Skill：已安装"
Write-Host "工程图读取 Skill：已安装"
Write-Host "建模规划 Skill：已安装"
Write-Host "一键总控 Skill：已安装"
Write-Host "通用 Runner：已安装"
