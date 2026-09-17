@echo off
REM NX_MCP_Loader build script (no admin needed, output next to this script)
REM NX base dir detection order:
REM   1. UGII_BASE_DIR env
REM   2. Windows Registry (HKLM\SOFTWARE\WOW6432Node\Siemens\NX *\UGII_BASE_DIR, then HKLM\SOFTWARE\Siemens\...)
REM   3. PATH (where ugraf.exe, walk up from NXBIN)
REM   4. %ProgramFiles%\Siemens\NX*  (highest version that validates)
REM   5. %ProgramFiles(x86)%\Siemens\NX*
REM Every candidate is accepted only if NXBIN\managed\NXOpen.dll exists.
setlocal enabledelayedexpansion

set "OUT=%~dp0"
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" (
  echo ERROR: .NET Framework 4.x csc.exe not found.
  exit /b 1
)

set "BASE="
goto :detect

:accept
REM %1 = candidate root; validate NXBIN\managed\NXOpen.dll
if exist "%~1\NXBIN\managed\NXOpen.dll" set "BASE=%~1"
if defined BASE (exit /b 0) else (exit /b 1)

:detect
REM 1. UGII_BASE_DIR
if defined UGII_BASE_DIR (
  call :accept "%UGII_BASE_DIR%"
  if defined BASE goto :build
)

REM 2. Registry
for %%H in (HKLM\SOFTWARE\WOW6432Node\Siemens HKLM\SOFTWARE\Siemens) do (
  for /f "delims=" %%k in ('reg query "%%H" 2^>nul') do (
    if not defined BASE (
      echo %%k | findstr /i /c:"\NX " >nul && (
        for /f "tokens=2,*" %%v in ('reg query "%%k" /v UGII_BASE_DIR 2^>nul') do (
          call :accept "%%w"
        )
      )
    )
  )
)
if defined BASE goto :build

REM 3. PATH: where ugraf.exe, walk up from NXBIN
for /f "delims=" %%u in ('where ugraf.exe 2^>nul') do (
  if not defined BASE (
    for %%p in ("%%~dpu..") do call :accept "%%~fp"
  )
)
if defined BASE goto :build

REM 4/5. ProgramFiles Siemens NX*, sorted by version descending.
REM PowerShell enumerates/sorts; build.bat validates each candidate via :accept.
set "PSCMD=powershell -NoProfile -Command "$d=@();$n=@();foreach($r in @($env:ProgramFiles,${env:ProgramFiles(x86)})){if($r -and(Test-Path \"$r\Siemens\")){Get-ChildItem -Directory \"$r\Siemens\NX*\" -ErrorAction SilentlyContinue|ForEach-Object{if($_.Name -match 'NX\s+(\d+(?:\.\d+)*)'){$v=$matches[1];try{$vv=[version]($v+'.0')}catch{$vv=[version]'0.0'};$d+=[pscustomobject]@{V=$vv;P=$_.FullName}}else{$n+=$_.FullName}}}};$d|Sort-Object V -Descending|ForEach-Object{$_.P};$n|ForEach-Object{$_}""
for /f "delims=" %%d in ('%PSCMD% 2^>nul') do (
  if not defined BASE call :accept "%%d"
)
if defined BASE goto :build

echo ERROR: NX base dir not found. Set UGII_BASE_DIR to your NX install root.
exit /b 1

:build
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
