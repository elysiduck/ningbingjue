@echo off
chcp 65001 >nul
setlocal EnableExtensions

REM Project root = parent dir of this .bat (test-launch) -> ningbingjue
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "ROOT=%ROOT%\.."

set "PY=%ROOT%\runtime\python\python.exe"
if not exist "%PY%" (
    echo [WARN] bundled python not found: %PY%
    echo        falling back to system python ...
    set "PY=python"
)

set "SCRIPT=%ROOT%\test-launch\py-script\p1_topo_probe.py"
if not exist "%SCRIPT%" (
    echo [ERROR] P1 script missing: %SCRIPT%
    echo         please keep the ningbingjue folder intact.
    pause
    exit /b 1
)

echo Running P1 probe: topology enumeration + hottest-core detection ...
echo.

"%PY%" "%SCRIPT%" --out "%~dp0p1_result.txt" --config "%~dp0p1_config.json" %*
set RC=%errorlevel%
echo.

if not "%RC%"=="0" (
    echo [ERROR] p1_topo_probe.py exited with code %RC%
)

echo.
echo ====================================================
echo  Result saved to: %~dp0p1_result.txt
echo  Opening it now (full report also shown above).
echo  Press any key to close this window.
echo ====================================================
start "" "%~dp0p1_result.txt"
pause
exit /b %RC%
