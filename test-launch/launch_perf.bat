@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM =====================================================================
REM  test-launch\launch_perf.bat
REM  One-click launcher for the performance-detection script
REM  (measure_game_load.py) which lives in test-launch\py-script\ under the project root.
REM
REM  This is a self-contained launcher: copy the whole "ningbingjue"
REM  folder anywhere and this still works, because it resolves the
REM  project root from its own location (no hard-coded paths).
REM
REM  Arguments are passed straight through to measure_game_load.py:
REM    launch_perf.bat                            -> auto-detect foreground game
REM    launch_perf.bat --exe eldenring.exe
REM    launch_perf.bat --duration 180 --no-gpu
REM =====================================================================

REM Resolve project root = parent of this .bat's folder (absolute, no trailing slash)
pushd "%~dp0.." >nul 2>&1
set "ROOT=%CD%"
popd

set "PY=%ROOT%\runtime\python\python.exe"
if not exist "%PY%" set "PY=python"

set "SCRIPT=%ROOT%\test-launch\py-script\measure_game_load.py"
if not exist "%SCRIPT%" (
    echo [ERROR] performance script not found: %SCRIPT%
    pause
    exit /b 1
)

REM No args: default to auto-detect the foreground game window.
if "%~1"=="" (
    "%PY%" "%SCRIPT%" --auto
) else (
    "%PY%" "%SCRIPT%" %*
)
set RC=%errorlevel%
if not "%RC%"=="0" (
    echo [ERROR] measure_game_load.py exited with code %RC%
)
echo.
echo [done] press any key to close
pause >nul
exit /b %RC%
