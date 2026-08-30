@echo off
setlocal EnableExtensions
REM =====================================================================
REM  test-launch\test_ryzenadj.bat
REM  One-click launcher for test-launch\py-script\test_ryzenadj.py
REM  Resolves project root from its own location.
REM
REM  Two ways to feed ryzenadj arguments:
REM    * From a command line:  test_ryzenadj.bat --max-gfxclk 800 --min-gfxclk 800
REM    * By double-clicking:   you'll be prompted to TYPE the arguments
REM                           (just press Enter for the default -i health check)
REM
REM  NOTE: 在 Strix Point (HX370 / AI 9 365) 上必须用 --max-gfxclk/--min-gfxclk
REM         钉 GPU 频率；旧的 --gfx-clk 是 Renoir Only，会被 SMU 忽略（设了=没设）。
REM  After each run you're asked "Run another command? (y/n)" so you can
REM  test several values (e.g. different --max-gfxclk) without re-launching.
REM
REM  The window NEVER auto-closes: it pauses at the end so you can read
REM  ryzenadj's return value. Pure ASCII / English only.
REM =====================================================================

set "RC=0"
set "RY_ARGS="

REM Project root = parent of this .bat's folder (test-launch -> project root)
pushd "%~dp0.." >nul 2>&1
set "ROOT=%CD%"
popd

set "PY=%ROOT%\runtime\python\python.exe"
if not exist "%PY%" set "PY=python"

set "SCRIPT=%ROOT%\test-launch\py-script\test_ryzenadj.py"
if not exist "%SCRIPT%" (
    echo [ERROR] test script not found: %SCRIPT%
    echo         Make sure py-script\test_ryzenadj.py exists next to this .bat.
    pause
    exit /b 1
)

"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python interpreter not found or not runnable: %PY%
    pause
    exit /b 1
)

REM No command-line args -> start in interactive input mode.
if "%~1"=="" goto INPUT
set "RY_ARGS=%*"
goto RUN

:INPUT
echo.
echo No arguments supplied on the command line.
echo Type the ryzenadj command to send right now.
echo   Example : --max-gfxclk 800 --min-gfxclk 800   (Strix Point 钉 GPU 频率)
echo   Blank   : run the default -i (read-only health check)
echo.
set "RY_ARGS="
set /p RY_ARGS=ryzenadj args> 

:RUN
echo Using python : %PY%
echo Script       : %SCRIPT%
echo.

"%PY%" "%SCRIPT%" %RY_ARGS%
set RC=%errorlevel%

echo.
echo ==========================================================
if "%RC%"=="0" echo  ryzenadj reported SUCCESS (exit code 0).
if "%RC%"=="1" echo  ryzenadj reported FAILURE (exit code 1).
if "%RC%"=="2" echo  Could NOT locate ryzenadj.exe (exit code 2).
if not "%RC%"=="0" if not "%RC%"=="1" if not "%RC%"=="2" echo  Script exited with code %RC%.
echo  Detailed ryzenadj calls are logged to:
echo    %ROOT%\logs\ryzenadj_invoke.log
echo ==========================================================
echo.

set /p AGAIN=Run another command? (y = yes, any other key = exit) 
if /i "%AGAIN%"=="y" goto INPUT

echo.
echo  [done] press any key to close this window
pause
exit /b %RC%
