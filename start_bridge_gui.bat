@echo off
rem ============================================================
rem NX MCP - Visual Bridge Launcher (GUI mode)
rem Starts Siemens NX with the NX MCP visual bridge journal,
rem then models step by step via MCP tools. Release the GUI
rem with the nx_release tool, or press Stop in NX's Journal dialog.
rem
rem Paths are auto-detected:
rem   NX install  : UGII_BASE_DIR env -> PATH (ugraf.exe) -> common dirs
rem   Workspace   : NX_MCP_WORKSPACE env -> %USERPROFILE%\NX_MCP_WORKSPACE
rem ============================================================
setlocal enabledelayedexpansion

rem ---------- auto-detect NX install ----------
set "NX_BASE="
if defined UGII_BASE_DIR set "NX_BASE=%UGII_BASE_DIR%"
rem machine-level env (works even before Explorer refreshes its environment)
if not defined NX_BASE for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v UGII_BASE_DIR 2^>nul') do if not defined NX_BASE set "NX_BASE=%%b"
if not defined NX_BASE (
    where ugraf.exe >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%i in ('where ugraf.exe') do set "NX_BIN_DIR=%%~dpi"
        for %%i in ("!NX_BIN_DIR!..") do set "NX_BASE=%%~fi"
    )
)
if not defined NX_BASE (
    if exist "%ProgramFiles%\Siemens" for /d %%i in ("%ProgramFiles%\Siemens\NX*") do if not defined NX_BASE set "NX_BASE=%%~fi"
    if exist "%ProgramW6432%\Siemens" for /d %%i in ("%ProgramW6432%\Siemens\NX*") do if not defined NX_BASE set "NX_BASE=%%~fi"
)
if not defined NX_BASE (
    echo [NX_MCP] ERROR: could not auto-detect the NX installation.
    echo [NX_MCP] Set the UGII_BASE_DIR environment variable to your NX install dir
    echo [NX_MCP] (for example: setx UGII_BASE_DIR "<path-to-your-NX-install>")
    pause
    exit /b 1
)
set "UGII_BASE_DIR=%NX_BASE%"
set "UGII_ROOT_DIR=%NX_BASE%\NXBIN"

rem ---------- optional license server (override if yours differs) ----------
if defined UGS_LICENSE_SERVER goto :have_lic
if not exist "%NX_BASE%\UGII\ugraf.exe" set "UGS_LICENSE_SERVER=27800@localhost"
set "UGII_LICENSE_FILE=%UGS_LICENSE_SERVER%"
set "LM_LICENSE_FILE=%UGS_LICENSE_SERVER%"
:have_lic

set "PATH=%NX_BASE%\nxbin;%NX_BASE%\ugii;%PATH%"

rem ---------- workspace ----------
if not defined NX_MCP_WORKSPACE set "NX_MCP_WORKSPACE=%USERPROFILE%\NX_MCP_WORKSPACE"
if not exist "%NX_MCP_WORKSPACE%" mkdir "%NX_MCP_WORKSPACE%"
set "NX_MCP_ALLOW_UNVERIFIED_PYTHON_BRIDGE=1"
set "NX_MCP_BRIDGE_STOP_FILE=%NX_MCP_WORKSPACE%\stop.txt"

rem journal lives next to this launcher (repo-relative, no machine paths)
set "JOURNAL=%~dp0examples\start_nx_bridge_gui.py"

echo [NX_MCP] NX install : %NX_BASE%
echo [NX_MCP] Workspace  : %NX_MCP_WORKSPACE%
echo [NX_MCP] Journal    : %JOURNAL%
cd /d "%NX_BASE%\UGII"
"%NX_BASE%\NXBIN\ugraf.exe" -g "%JOURNAL%" > "%NX_MCP_WORKSPACE%\bridge_gui.log" 2>&1
