@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%" >nul 2>&1
if not errorlevel 1 (
    set "SCRIPT_DIR=!CD!"
)

REM --- auto request admin (ryzenadj needs ring0 driver) ---
net session >nul 2>&1
if errorlevel 1 (
    echo [info] requesting administrator privileges for GPU freq control...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    goto :eof
)

REM --- kill stale NingBingJue python processes (avoid launch.err lock) ---
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*run.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

set "BUNDLED_PY=!SCRIPT_DIR!\runtime\python\python.exe"
set "PY="
if exist "!BUNDLED_PY!" (
    set "PY=!BUNDLED_PY!"
) else (
    set "PY=python"
)

REM --- clear stale bytecode ONLY with "fresh" arg (launch.bat fresh) ---
REM Python auto-invalidates .pyc by source mtime, so wiping __pycache__ on
REM every launch forced a full cold recompile of the whole src tree and was
REM a major slow-startup factor. Keep the wipe available for the rare
REM "edited code but old logic still runs" case (e.g. sync tools that
REM preserve timestamps): just run  launch.bat fresh  once.
if /i "%~1"=="fresh" (
    echo [fresh] clearing __pycache__ under src ...
    for /d /r "%SCRIPT_DIR%\src" %%d in (__pycache__) do (
        if exist "%%d" rmdir /s /q "%%d" >nul 2>&1
    )
)
"%PY%" -c "import PyQt6" >nul 2>&1
if errorlevel 1 (
    echo [hint] PyQt6 not found, trying pip install...
    "%PY%" -m pip install PyQt6
)
echo Starting NingBingJue...
set "ERRFILE=%TEMP%\nbj_launch_%RANDOM%.err"
"%PY%" run.py --gui >"%ERRFILE%" 2>&1
set RC=!errorlevel!
if not !RC! == 0 goto showerr
goto cleanexit
:showerr
echo ==============================================
echo  STARTUP FAILED (exit code !RC!)
echo  Log file: !ERRFILE!
echo  ----------------------------------------------
type "!ERRFILE!"
echo  ----------------------------------------------
:cleanexit
echo Press any key to close this window...
pause
popd
exit /b !RC!
