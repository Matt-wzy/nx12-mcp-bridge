# install-agent.ps1
# 安装统一 NX Agent Skill + Plan Runner。

[CmdletBinding()]
param(
    [string]$AgentProfile = "",
    [string]$RepoRoot = "",
    [string]$PythonExe = "",
    [string]$Workspace = "",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = $PSScriptRoot }
$RepoRoot = (Resolve-Path $RepoRoot).Path
$skillSource = Join-Path $RepoRoot "skills\nx-agent"
$runnerSource = Join-Path $RepoRoot "agent\nx-mcp-plan-runner"

if ([string]::IsNullOrWhiteSpace($Workspace)) {
    if (-not [string]::IsNullOrWhiteSpace($env:NX_MCP_WORKSPACE)) { $Workspace = $env:NX_MCP_WORKSPACE }
    else { $Workspace = Join-Path $env:USERPROFILE "NX_MCP_WORKSPACE" }
}
New-Item -ItemType Directory -Force -Path $Workspace | Out-Null
$Workspace = (Resolve-Path $Workspace).Path

if (-not $PythonExe) {
    $repoPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $repoPy) { $PythonExe = $repoPy }
    else { $PythonExe = (Get-Command python -ErrorAction Stop | Select-Object -First 1).Source }
}

if (-not (Test-Path (Join-Path $skillSource "SKILL.md"))) { throw "仓库缺少统一 Skill: $skillSource\SKILL.md" }
if (-not (Test-Path (Join-Path $runnerSource "runner.py"))) { throw "仓库缺少 Runner: $runnerSource\runner.py" }

$verifyScript = Join-Path $RepoRoot "agent\verify_agent_pack.py"
if (-not (Test-Path $verifyScript)) { throw "缺少 Agent Pack 校验脚本: $verifyScript" }
& $PythonExe $verifyScript
if ($LASTEXITCODE -ne 0) { throw "Agent Pack 静态完整性检查失败" }

function Find-AgentSkillTargets {
    param([string]$RequestedProfile)
    $roots=@()
    if ($env:LOCALAPPDATA) { $roots += $env:LOCALAPPDATA }
    if ($env:APPDATA -and $env:APPDATA -ne $env:LOCALAPPDATA) { $roots += $env:APPDATA }
    $targets=@()
    foreach($root in ($roots|Select-Object -Unique)){
        foreach($appDir in (Get-ChildItem $root -Directory -Force -ErrorAction SilentlyContinue)){
            $userData=Join-Path $appDir.FullName "User Data"
            if(-not (Test-Path $userData)){ continue }
            $profiles=Get-ChildItem $userData -Directory -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq "Default" -or $_.Name -like "Profile*" }
            foreach($p in $profiles){
                if($RequestedProfile -and $p.Name -ne $RequestedProfile){ continue }
                foreach($runtime in (Get-ChildItem $p.FullName -Directory -Force -ErrorAction SilentlyContinue)){
                    $agentWorkspace=Join-Path $runtime.FullName "agent_mode\workspace"
                    if(Test-Path $agentWorkspace){
                        $targets += [PSCustomObject]@{
                            ProfileName=$p.Name
                            SkillRoot=(Join-Path $agentWorkspace ".user_skills")
                            LastWrite=$p.LastWriteTime
                        }
                    }
                }
            }
        }
    }
    return $targets
}

$candidates=@(Find-AgentSkillTargets -RequestedProfile $AgentProfile)
if($candidates.Count -eq 0){ throw "未找到 Agent Skill 工作区。请先启动一次支持本地 Agent Skill 的客户端并进入 Agent/工作任务模式。" }
$chosen=$candidates | Sort-Object LastWrite -Descending | Select-Object -First 1
$skillRoot=$chosen.SkillRoot
New-Item -ItemType Directory -Force -Path $skillRoot | Out-Null
Write-Host "[Profile] 选择: $($chosen.ProfileName)"
Write-Host "[Profile] Skill 安装目标: $skillRoot"

# nx-agent 自身采用 clean-copy，防止已删除的旧 reference 残留。
$dst=Join-Path $skillRoot "nx-agent"
if(Test-Path $dst){ Remove-Item -Recurse -Force $dst }
robocopy $skillSource $dst /E /NFL /NDL /NJH /NJS | Out-Null
if($LASTEXITCODE -ge 8){ throw "复制 nx-agent 失败（robocopy rc=$LASTEXITCODE）" }
Write-Host "[Skill] 已安装: nx-agent"

# 清理旧版 4 个独立 Skill，仅删除已知旧目录。
$legacy=@("nx-modeling","nx-engineering-drawing-reader","nx-mcp-modeling-planner","nx-mcp-pipeline")
foreach($name in $legacy){
    $p=Join-Path $skillRoot $name
    if(Test-Path $p){ Remove-Item -Recurse -Force $p; Write-Host "[Skill] 已移除旧版独立 Skill: $name" }
}

# Runner 跟随实际 Workspace。
$runnerDst=Join-Path $Workspace "nx-mcp-plan-runner"
New-Item -ItemType Directory -Force -Path $runnerDst | Out-Null
robocopy $runnerSource $runnerDst /E /IS /NFL /NDL /NJH /NJS | Out-Null
if($LASTEXITCODE -ge 8){ throw "复制 Runner 失败（robocopy rc=$LASTEXITCODE）" }

# 冻结运行路径，避免 Agent 每次递归搜索 Python / repo。
$runtimeConfig = [ordered]@{
    schema_version = 1
    repo_root       = $RepoRoot
    nx_mcp_src      = (Join-Path $RepoRoot "src")
    python_exe      = $PythonExe
    workspace_root  = $Workspace
}
$runtimeConfigPath = Join-Path $runnerDst "runtime-config.json"
$runtimeConfig | ConvertTo-Json | Set-Content -LiteralPath $runtimeConfigPath -Encoding UTF8
Write-Host "[Runner] 已安装: $runnerDst"
Write-Host "[Runner] 运行配置: $runtimeConfigPath"

$requiredSkillFiles = @(
    "SKILL.md",
    "references\text-modeling.md",
    "references\drawing-reader.md",
    "references\modeling-planner.md",
    "references\nx-drawing-rules.md",
    "references\nx-mcp-rules.md",
    "references\topology-safety.md",
    "references\runner-contract.md",
    "references\certified-tool-contract.json",
    "references\pipeline-contract.md",
    "references\chinese-output.md",
    "examples\example-output.json",
    "examples\modeling-plan-example.json",
    "examples\pipeline-state-example.json"
)
foreach($rel in $requiredSkillFiles){
    if(-not (Test-Path (Join-Path $dst $rel))){ throw "Skill 安装不完整，缺少: $rel" }
}

$md=Join-Path $dst "SKILL.md"
$nameLine=Select-String -Path $md -Pattern '^name:\s*(\S+)' | Select-Object -First 1
if(-not $nameLine -or $nameLine.Matches[0].Groups[1].Value.Trim() -ne "nx-agent"){ throw "nx-agent frontmatter name 校验失败" }
if(-not (Test-Path (Join-Path $runnerDst "runner.py"))){ throw "runner.py 缺失" }
if(-not (Test-Path (Join-Path $runnerDst "plan_schema.json"))){ throw "plan_schema.json 缺失" }

@(
    (Join-Path $dst "references\certified-tool-contract.json"),
    (Join-Path $dst "examples\example-output.json"),
    (Join-Path $dst "examples\modeling-plan-example.json"),
    (Join-Path $dst "examples\pipeline-state-example.json"),
    $runtimeConfigPath
) | ForEach-Object {
    try { Get-Content -Raw -LiteralPath $_ | ConvertFrom-Json | Out-Null }
    catch { throw "JSON 校验失败: $_" }
}

if(-not $SkipTests){
    Push-Location $runnerDst
    try{
        & $PythonExe tests/test_plan_resolution.py
        if($LASTEXITCODE -ne 0){ throw "Runner test_plan_resolution 失败" }
        & $PythonExe tests/test_bbox_report.py
        if($LASTEXITCODE -ne 0){ throw "Runner test_bbox_report 失败" }
    } finally { Pop-Location }
}

Write-Host ""
Write-Host "Agent Pack 安装成功"
Write-Host "NX Agent Skill：已安装（统一入口）"
Write-Host "Plan Runner：已安装"
Write-Host "Workspace：$Workspace"
