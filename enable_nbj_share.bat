@echo off
REM ============================================================
REM  enable_nbj_share.bat  -  one-shot setup of the NBJ SMB share.
REM  Double-click it. It self-elevates to admin if needed.
REM  What it does:
REM    1. Ensures LanmanServer / LanmanWorkstation are auto+running.
REM    2. Sets the active network profile to Private (so file sharing is allowed).
REM    3. Enables "File and Printer Sharing" + "Network Discovery" firewall rules.
REM    4. Creates the NBJ share with Everyone:FULL and correct NTFS ACL.
REM    5. Self-tests local access before finishing.
REM  Handheld source :  \\<this-pc>\NBJ\src
REM  Handheld logs   :  \\<this-pc>\NBJ\logs
REM ============================================================
setlocal EnableDelayedExpansion

REM --- self elevate to admin (never silently exit) -----------
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo.
    echo [!] This script needs Administrator rights.
    echo     A UAC prompt will appear. Click "Yes" to continue.
    echo     If no UAC prompt shows up, close this window and
    echo     right-click this .bat file -> "Run as administrator".
    echo.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    echo (Original window is now handing off to the elevated window...)
    pause
    exit /b
)

REM --- paths (edit SHARE_DIR if project moves) ----------------
set "SHARE_DIR=C:\Users\13567\WorkBuddy\2026-07-18-10-22-49\ningbingjue"
set "SHARE_NAME=NBJ"

if not exist "%SHARE_DIR%" (
    echo [X] Project root not found: %SHARE_DIR%
    pause
    exit /b 1
)

REM --- ensure SMB server / workstation services ---------------
echo [*] Ensuring SMB services are auto-started and running...
sc config LanmanServer start= auto >nul 2>&1
net start LanmanServer >nul 2>&1
sc config LanmanWorkstation start= auto >nul 2>&1
net start LanmanWorkstation >nul 2>&1
timeout /t 2 /nobreak >nul

REM --- switch active network profile to Private ---------------
echo [*] Setting active network profile to Private...
powershell -NoProfile -Command "Get-NetConnectionProfile | Where-Object { $_.IPv4Connectivity -ne 'NoTraffic' -and $_.Name -notlike '*Loopback*' } | Set-NetConnectionProfile -NetworkCategory Private" >nul 2>&1

REM --- enable firewall rules for file sharing -----------------
echo [*] Enabling firewall rules for file sharing...
REM Try English then Chinese display group names (OS language dependent)
netsh advfirewall firewall set rule group="File and Printer Sharing" new enable=yes profile=private >nul 2>&1
netsh advfirewall firewall set rule group="files and printer sharing" new enable=yes profile=private >nul 2>&1
netsh advfirewall firewall set rule group="Network Discovery" new enable=yes profile=private >nul 2>&1
netsh advfirewall firewall set rule group="network discovery" new enable=yes profile=private >nul 2>&1

REM --- remove any existing NBJ share (idempotent) -------------
net share %SHARE_NAME% >nul 2>&1
if %errorLevel% equ 0 (
    echo [*] Removing existing %SHARE_NAME% share...
    net share %SHARE_NAME% /delete /y >nul 2>&1
)

REM --- create SMB share, Everyone full (home LAN) -------------
echo [*] Creating share %SHARE_NAME% -^> %SHARE_DIR%
net share %SHARE_NAME%="%SHARE_DIR%" /GRANT:Everyone,FULL /REMARK:"NingBingJue dev sync" /CACHE:None
if %errorLevel% neq 0 (
    echo [X] Failed to create share. Make sure you are admin.
    pause
    exit /b 1
)

REM --- relax NTFS ACL so authenticated remote users can read+write
echo [*] Setting NTFS permissions (Everyone: Modify, inherited)...
icacls "%SHARE_DIR%" /grant:r "Everyone:(OI)(CI)(M)" /T >nul 2>&1

REM --- local self-test ----------------------------------------
echo [*] Self-testing share from localhost...
net use X: "\\%COMPUTERNAME%\%SHARE_NAME%" >nul 2>&1
if %errorLevel% equ 0 (
    echo [OK] Share reachable from this PC.
    net use X: /delete /y >nul 2>&1
) else (
    echo [X] Local share test failed. A reboot may be required.
    pause
    exit /b 1
)

echo.
echo [OK] %SHARE_NAME% share is live.
echo      Handheld source : \\%COMPUTERNAME%\%SHARE_NAME%\src
echo      Handheld logs   : \\%COMPUTERNAME%\%SHARE_NAME%\logs
echo      (Use \\%COMPUTERNAME%\%SHARE_NAME% instead of the IP; it survives DHCP changes)
echo.
pause
