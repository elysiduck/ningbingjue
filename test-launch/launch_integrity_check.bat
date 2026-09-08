@echo off
REM =====================================================================
REM  test-launch\launch_integrity_check.bat
REM  One-click launcher for the Python corruption scanner
REM  (check_scripts_integrity.py).
REM
REM  Convention (same as sibling launch_perf.bat): project root is
REM  resolved from this .bat's own location (no hard-coded absolute
REM  paths), so copying the whole "ningbingjue" folder elsewhere keeps
REM  this working.
REM
REM  Arguments are forwarded to the scanner, e.g.
REM    launch_integrity_check.bat                  -> scan project recursively
REM    launch_integrity_check.bat C:\some\dir      -> scan a specific dir
REM    launch_integrity_check.bat --all            -> also scan bundled libs
REM
REM  The window NEVER auto-closes. After a scan we drop into a wait
REM  loop in the SAME cmd process (no child shell): press ENTER to
REM  scan again, or type "exit" + ENTER to close the window.
REM
REM  Pure ASCII / English only: CMD parses .bat as GBK on zh-CN, so any
REM  non-ASCII here would be garbled and could break the script. The
REM  scanner (.py, UTF-8) outputs Chinese labels; we switch the console
REM  to UTF-8 (chcp 65001) and force Python UTF-8 (PYTHONUTF8=1) so
REM  those labels render correctly instead of as mojibake.
REM =====================================================================

REM Switch console to UTF-8 so the scanner's Chinese output is readable.
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"

set "RC=0"

REM Project root = parent of this .bat's folder (absolute, no trailing slash)
pushd "%~dp0.." >nul 2>&1
set "ROOT=%CD%"
popd

set "PY=%ROOT%\runtime\python\python.exe"
if not exist "%PY%" set "PY=python"

REM Locate the scanner. Prefer it INSIDE the project (ROOT), which matches
REM the portable "ningbingjue" folder design; fall back to one level above
REM (workspace root) so it also works while the scanner still sits at the
REM workspace root. This makes the launcher tolerant of either layout.
set "SCANNER="
if exist "%ROOT%\check_scripts_integrity.py" (
    set "SCANNER=%ROOT%\check_scripts_integrity.py"
) else if exist "%ROOT%\..\check_scripts_integrity.py" (
    set "SCANNER=%ROOT%\..\check_scripts_integrity.py"
)

REM Where to save a copy of the scan output (so results survive even if the
REM console is closed / behaves oddly). Lives next to this .bat.
set "LOG=%~dp0last_scan_report.txt"

if not defined SCANNER (
    echo [ERROR] check_scripts_integrity.py not found at:
    echo         %ROOT%\check_scripts_integrity.py
    echo         nor at %ROOT%\..\check_scripts_integrity.py
    echo         Place the scanner next to this .bat or at the workspace root.
    echo.
    echo (Window stays open. Close it manually, or type "exit" + ENTER.)
    set /p "x=>> "
    if /i "%x%"=="exit" goto :eof
    goto :eof
)

"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python interpreter not found or not runnable: %PY%
    echo         (The bundled runtime may be missing; install Python or copy the runtime folder.)
    echo.
    echo (Window stays open. Close it manually, or type "exit" + ENTER.)
    set /p "x=>> "
    if /i "%x%"=="exit" goto :eof
    goto :eof
)

echo Using python : %PY%
echo Scanner      : %SCANNER%
echo Log file     : %LOG%
echo.
echo ----------------------------------------------------------
echo  Tip: pass a directory to scan a specific location.
echo       (no argument = scan the project source recursively)
echo       use --all to also scan bundled runtime/.venv
echo       --list is always added so every file is listed
echo ----------------------------------------------------------
echo.

REM ---- scan loop (same cmd process, never auto-closes) ----
:rescan
"%PY%" "%SCANNER%" %* --list > "%LOG%" 2>&1
type "%LOG%"
set RC=%errorlevel%

echo.
echo ==========================================================
if "%RC%"=="0" (
    echo  Scan finished CLEAN - no hard corruption found (exit code 0).
) else if "%RC%"=="2" (
    echo  Scan could NOT run - see errors above (exit code 2).
) else (
    echo  Scan finished WITH PROBLEMS (exit code %RC%).
)
echo.
echo  Detection list is shown above. For each file:
echo    [OK]     = clean (compiles, valid encoding, no binary junk)
echo    [BAD]    = HARD corruption, MUST fix
echo    [REVIEW] = empty/small file, usually normal
echo.
echo  A copy of this output was saved to:
echo    %LOG%
echo.
echo  Window stays OPEN. Press ENTER to scan again,
echo  or type "exit" + ENTER to close this window.
echo ==========================================================
set /p "x=>> "
if /i "%x%"=="exit" goto :eof
goto :rescan
