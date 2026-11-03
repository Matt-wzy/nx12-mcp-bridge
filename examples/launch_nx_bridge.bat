@echo off
REM ============================================================
REM  Launcher for NX 12 + NX_MCP resident Loader (+ bridge journal)
REM  无硬编码路径：NX 安装目录自动探测，其余相对 %USERPROFILE% 与本仓库。
REM  可用环境变量覆盖：
REM    NX_BASE           NX 安装根（默认自动探测 ProgramFiles\Siemens\NX*）
REM    UGII_USER_DIR     Loader 部署目录（默认 %USERPROFILE%\.nx_mcp_user）
REM    NX_MCP_WORKSPACE  工作区（默认 %USERPROFILE%\NX_MCP_WORKSPACE）
REM ============================================================
setlocal enabledelayedexpansion

REM --- 定位 NX 安装根（取能找到 NXBIN\managed\NXOpen.dll 的最高版本）---
if not defined NX_BASE (
  for /f "delims=" %%d in ('powershell -NoProfile -Command "$c=@();foreach($r in @($env:ProgramFiles,${env:ProgramFiles(x86)})){if($r -and(Test-Path \"$r\Siemens\")){Get-ChildItem -Directory \"$r\Siemens\NX*\" -ErrorAction SilentlyContinue|ForEach-Object{if(Test-Path (Join-Path $_.FullName 'NXBIN\managed\NXOpen.dll')){$c+=$_.FullName}}}};if($c.Count -gt 0){$c[$c.Count-1]}"') do set "NX_BASE=%%d"
)
if not defined NX_BASE (
  echo [ERROR] NX installation not found. Set NX_BASE to your NX install root.
  exit /b 1
)

REM --- 仓库根（本 bat 所在目录的上一级）---
for %%p in ("%~dp0..") do set "REPO_ROOT=%%~fp"

if not defined UGII_USER_DIR set "UGII_USER_DIR=%USERPROFILE%\.nx_mcp_user"
if not defined NX_MCP_WORKSPACE set "NX_MCP_WORKSPACE=%USERPROFILE%\NX_MCP_WORKSPACE"
if not exist "%UGII_USER_DIR%\startup" mkdir "%UGII_USER_DIR%\startup" 2>nul

set "PATH=%NX_BASE%\NXBIN;%PATH%"
set "PYTHONPATH=%REPO_ROOT%\src"

start "NX-MCP" /min cmd /c "%NX_BASE%\NXBIN\ugraf.exe" -g "%REPO_ROOT%\examples\start_nx_bridge_gui.py"
endlocal
