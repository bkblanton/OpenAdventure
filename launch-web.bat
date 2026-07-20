@echo off
setlocal

cd /d "%~dp0"

where uv >nul 2>&1
if errorlevel 1 (
    echo OpenAdventure needs uv, but it was not found on your PATH.
    echo Install it from https://docs.astral.sh/uv/getting-started/installation/
    echo Then close this window and run launch-web.bat again.
    echo.
    pause
    exit /b 1
)

echo Starting OpenAdventure...
uv run openadventure web %*
set "OPENADVENTURE_EXIT_CODE=%ERRORLEVEL%"

if not "%OPENADVENTURE_EXIT_CODE%"=="0" (
    echo.
    echo OpenAdventure stopped with exit code %OPENADVENTURE_EXIT_CODE%.
    pause
)

exit /b %OPENADVENTURE_EXIT_CODE%
