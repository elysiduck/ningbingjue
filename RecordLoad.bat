@echo off
SETLOCAL ENABLEDELAYEDEXPANSION
REM =====================================================================
REM  RecordLoad.bat - one-click launcher for record_load.py
REM  (this file lives inside the nginbjie\ folder, self-contained:
REM   copy the whole nginbjie\ folder anywhere and it still runs)
REM
REM  Pass arguments straight through to the python recorder:
REM    RecordLoad.bat
REM    RecordLoad.bat --duration 180
REM    RecordLoad.bat --exe eldenring.exe --no-gpu
REM    RecordLoad.bat --interval 1000
REM
REM  If you double-click with NO arguments, an interactive prompt
REM  lets you type the values instead.
REM =====================================================================

SET "ROOT=%~dp0"
SET "PY=%ROOT%runtime\python\python.exe"
SET "SCRIPT=%ROOT%record_load.py"

IF NOT EXIST "%PY%" (
  echo [ERROR] bundled python not found: %PY%
  pause
  EXIT /B 1
)
IF NOT EXIST "%SCRIPT%" (
  echo [ERROR] record script not found: %SCRIPT%
  pause
  EXIT /B 1
)

REM -- Command-line args given? Pass them through verbatim. --
IF NOT "%~1"=="" (
  "%PY%" "%SCRIPT%" %*
  GOTO :eof
)

REM -- No args: interactive prompt (double-click use case). --
echo.
echo === RecordLoad interactive ===
echo (just press Enter to accept the [default])
set /p DUR="Recording duration in seconds [300]: "
if "%DUR%"=="" set "DUR=300"
set /p EXE="Target exe name (empty = follow foreground window): "
set /p NOGPU="Skip GPU sampling? (y/N): "

SET "ARGS=--duration %DUR%"
IF NOT "%EXE%"=="" SET "ARGS=%ARGS% --exe %EXE%"
IF /I "%NOGPU%"=="y" SET "ARGS=%ARGS% --no-gpu"

echo.
echo Launching: %PY% %SCRIPT% %ARGS%
echo.
"%PY%" "%SCRIPT%" %ARGS%
echo.
echo Done. Press any key to close.
pause >nul

ENDLOCAL
