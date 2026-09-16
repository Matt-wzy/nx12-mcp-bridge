@echo off
REM NX_MCP_Loader one-click uninstall: removes deployed plugin files only.
REM Keeps source, build script and client next to this script.
setlocal

set "STARTUP="
if defined UGII_USER_DIR set "STARTUP=%UGII_USER_DIR%\startup"
if not defined STARTUP set "STARTUP=%USERPROFILE%\.nx_mcp_user\startup"

echo Removing deployed NX_MCP_Loader files from %STARTUP% ...
if exist "%STARTUP%\NX_MCP_Loader.dll" del /f /q "%STARTUP%\NX_MCP_Loader.dll" && echo  - deleted NX_MCP_Loader.dll
if exist "%STARTUP%\NX_MCP_Loader.men" del /f /q "%STARTUP%\NX_MCP_Loader.men" && echo  - deleted NX_MCP_Loader.men

echo.
echo Done. Restart NX for the change to take effect.
echo Source and tooling kept at %~dp0
endlocal
