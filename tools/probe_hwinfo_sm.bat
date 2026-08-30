@echo off
chcp 65001 >nul 2>&1
setlocal
set "PS1=%~dp0probe_hwinfo_sm.ps1"
cd /d "%~dp0"

echo ============================================
echo  HWiNFO Shared-Memory Root-Cause Probe  -  BAT Launcher
echo  Current Run-As:
net session >nul 2>&1 && echo    [Administrator]  || echo    [Non-Admin User]
echo ============================================
echo.
echo  This launcher runs the probe via:
echo    powershell -ExecutionPolicy Bypass -File probe_hwinfo_sm.ps1
echo  which bypasses the system "scripts are disabled" restriction.
echo.
if not exist "%PS1%" (
  echo  [!] probe_hwinfo_sm.ps1 not found in the same folder as this .bat.
  echo      Keep both files together, then run again.
  pause
  exit /b 1
)
echo  IMPORTANT: run this .bat TWICE to detect a permission mismatch:
echo    1. Double-click this .bat          -^> Non-Admin
echo    2. Right-click -^> Run as Admin     -^> Administrator
echo  Paste BOTH outputs back to me.
echo.
echo  Running probe ...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
set RC=%errorlevel%
echo.
if not %RC%==0 (
  echo  [!] Probe exited with code %RC%. If it says "cannot be loaded / scripts disabled",
  echo      the handheld uses a GROUP-POLICY block; ask me to build an EncodedCommand variant.
)
echo ============================================
echo  To save output to a file: in CMD at this folder run:
echo    probe_hwinfo_sm.bat ^> probe_result.txt
echo ============================================
pause
