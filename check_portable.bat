@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title NingbingJue Portable Environment Check
set "LOG=%~dp0check_portable.log"
type nul > "%LOG%"
set "ROOT=%~dp0"
set "FAILS=0"

echo ============================================================& echo ============================================================>>"%LOG%"
echo  NingbingJue Portable Environment Check& echo  NingbingJue Portable Environment Check>>"%LOG%"
echo ============================================================& echo ============================================================>>"%LOG%"
echo.& echo.>>"%LOG%"

echo [1/3] Administrator privilege ...& echo [1/3] Administrator privilege ...>>"%LOG%"
fsutil dirty query %systemdrive% >nul 2>&1
if !errorlevel!==0 (
  echo       PASS: running as administrator& echo       PASS: running as administrator>>"%LOG%"
) else (
  echo       FAIL: NOT administrator. Right-click launch.bat and select Run as administrator.& echo       FAIL: NOT administrator.>>"%LOG%"
  set /a FAILS+=1
)

echo [2/3] Windows Memory Integrity - Core Isolation ...& echo [2/3] Windows Memory Integrity - Core Isolation ...>>"%LOG%"
set "HVCI=0"
for /f "tokens=3" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity" /v Enabled 2^>nul') do set "HVCI=%%a"
if "!HVCI!"=="0x1" (
  echo       FAIL: Memory Integrity is ON. Turn it OFF in Settings - Core Isolation, or ryzenadj driver is blocked.& echo       FAIL: Memory Integrity is ON.>>"%LOG%"
  set /a FAILS+=1
) else (
  echo       PASS: Memory Integrity is OFF or not configured& echo       PASS: Memory Integrity is OFF or not configured>>"%LOG%"
)

echo [3/3] Bundled resources ...& echo [3/3] Bundled resources ...>>"%LOG%"
set "FILES=runtime\python\python.exe blocks\ryzenadj-win64\ryzenadj.exe blocks\ryzenadj-win64\inpoutx64.dll RTSS\rtss-cli.exe layout.json"
for %%f in (%FILES%) do (
  if exist "%ROOT%%%f" (
    echo       PASS: %%f& echo       PASS: %%f>>"%LOG%"
  ) else (
    echo       FAIL: missing %%f& echo       FAIL: missing %%f>>"%LOG%"
    set /a FAILS+=1
  )
)

echo.& echo.>>"%LOG%"
echo ============================================================& echo ============================================================>>"%LOG%"
if !FAILS!==0 (
  echo  !FAILS! CHECK(S) FAILED. Fix the items above before running.& echo  !FAILS! CHECK(S) FAILED.>>"%LOG%"
) else (
  echo  ALL CHECKS PASSED. You can run launch.bat now.& echo  ALL CHECKS PASSED.>>"%LOG%"
)
echo ============================================================& echo ============================================================>>"%LOG%"
echo.& echo.>>"%LOG%"
echo Press any key to exit ...& echo Press any key to exit ...>>"%LOG%"
pause >nul
