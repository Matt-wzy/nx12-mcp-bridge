@echo off
REM NX_MCP_Loader build script (no admin needed, output next to this script)
setlocal

rem --- auto-detect NX base dir: UGII_BASE_DIR -> standard Program Files install ---
set "BASE=%UGII_BASE_DIR%"
if defined BASE goto :base_ok
if exist "%ProgramFiles%\Siemens\NX 2506" set "BASE=%ProgramFiles%\Siemens\NX 2506"
if defined BASE goto :base_ok
if exist "%ProgramFiles(x86)%\Siemens\NX 2506" set "BASE=%ProgramFiles(x86)%\Siemens\NX 2506"
if defined BASE goto :base_ok
echo ERROR: NX base dir not found. Set UGII_BASE_DIR to your NX 2506 install root.
exit /b 1
:base_ok

set "OUT=%~dp0"
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" (
  echo ERROR: .NET Framework 4.x csc.exe not found.
  exit /b 1
)

"%CSC%" /nologo /target:library /out:"%OUT%NX_MCP_Loader.dll" ^
  /reference:"%BASE%\NXBIN\managed\NXOpen.dll" ^
  /reference:"%BASE%\NXBIN\managed\NXOpenUI.dll" ^
  /reference:"%BASE%\NXBIN\managed\NXOpen.Utilities.dll" ^
  /reference:System.Windows.Forms.dll ^
  /reference:System.Core.dll ^
  /reference:System.dll ^
  "%OUT%NX_MCP_Loader.cs"

if errorlevel 1 (
  echo BUILD FAILED
  exit /b 1
)
echo BUILD OK: %OUT%NX_MCP_Loader.dll
exit /b 0
