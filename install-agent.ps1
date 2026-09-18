# install-agent.ps1
# 安装统一 NX Agent Skill + Plan Runner。

[CmdletBinding()]
param(
    [string]$AgentProfile = "",
    [string]$RepoRoot = "",
    [string]$PythonExe = "",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = $PSScriptRoot }
$RepoRoot = (Resolve-Path $RepoRoot).Path
$skillSource = Join-Path $RepoRoot "skills\nx-agent"
$runnerSource = Join-Path $RepoRoot "agent\nx-mcp-plan-runner"

if (-not (Test-Path (Join-Path $skillSource "SKILL.md"))) { throw "仓库缺少统一 Skill: $skillSource\SKILL.md" }
if (-not (Test-Path (Join-Path $runnerSource "runner.py"))) { throw "仓库缺少 Runner: $runnerSource\runner.py" }

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
            $profiles=Get-ChildItem $userData -Directory -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -eq "Default" -or $_.Name -like "Profile*" }
            foreach($p in $profiles){
                if($RequestedProfile -and $p.Name -ne $RequestedProfile){ continue }
                foreach($runtime in (Get-ChildItem $p.FullName -Directory -Force -ErrorAction SilentlyContinue)){
                    $workspace=Join-Path $runtime.FullName "agent_mode\workspace"
                    if(Test-Path $workspace){
                        $targets += [PSCustomObject]@{
                            ProfileName=$p.Name
                            SkillRoot=(Join-Path $workspace ".user_skills")
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
Write-Host "[Profile] Skill 安装目标: $skillRoot"

$dst=Join-Path $skillRoot "nx-agent"
robocopy $skillSource $dst /E /IS /NFL /NDL /NJH /NJS | Out-Null
if($LASTEXITCODE -ge 8){ throw "复制 nx-agent 失败（robocopy rc=$LASTEXITCODE）" }
Write-Host "[Skill] 已安装: nx-agent"

# 清理旧版 4 个独立 Skill，仅删除已知旧目录，不影响其它用户 Skill。
$legacy=@("nx-modeling","nx-engineering-drawing-reader","nx-mcp-modeling-planner","nx-mcp-pipeline")
foreach($name in $legacy){
    $p=Join-Path $skillRoot $name
    if(Test-Path $p){ Remove-Item -Recurse -Force $p; Write-Host "[Skill] 已移除旧版独立 Skill: $name" }
}

$runnerDst=Join-Path $env:USERPROFILE "NX_MCP_WORKSPACE\nx-mcp-plan-runner"
New-Item -ItemType Directory -Force -Path (Split-Path $runnerDst -Parent) | Out-Null
robocopy $runnerSource $runnerDst /E /IS /NFL /NDL /NJH /NJS | Out-Null
if($LASTEXITCODE -ge 8){ throw "复制 Runner 失败（robocopy rc=$LASTEXITCODE）" }

$md=Join-Path $dst "SKILL.md"
$nameLine=Select-String -Path $md -Pattern '^name:\s*(\S+)' | Select-Object -First 1
if(-not $nameLine -or $nameLine.Matches[0].Groups[1].Value.Trim() -ne "nx-agent"){ throw "nx-agent frontmatter name 校验失败" }
if(-not (Test-Path (Join-Path $runnerDst "runner.py"))){ throw "runner.py 缺失" }
if(-not (Test-Path (Join-Path $runnerDst "plan_schema.json"))){ throw "plan_schema.json 缺失" }

if(-not $SkipTests){
    if(-not $PythonExe){
        $repoPy=Join-Path $RepoRoot ".venv\Scripts\python.exe"
        if(Test-Path $repoPy){ $PythonExe=$repoPy } else { $PythonExe=(Get-Command python).Source }
    }
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
