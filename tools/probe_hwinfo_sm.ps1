# probe_hwinfo_sm.ps1 - HWiNFO shared-memory "cannot read" root-cause probe (ASCII / EN)
# Run on the handheld via probe_hwinfo_sm.bat (which uses -ExecutionPolicy Bypass).
# Run TWICE: double-click (.bat = Non-Admin) and right-click -> Run as Admin.
# Paste BOTH outputs back.

$ErrorActionPreference = "Continue"

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class HwiProbe {
    public const uint FILE_MAP_READ = 0x0004;
    [DllImport("kernel32", SetLastError = true, CharSet = CharSet.Ansi)]
    public static extern IntPtr OpenFileMappingA(uint dwDesiredAccess, bool bInheritHandle, string lpName);
    [DllImport("kernel32", SetLastError = true)]
    public static extern IntPtr MapViewOfFile(IntPtr hFileMappingObject, uint dwDesiredAccess,
        uint dwFileOffsetHigh, uint dwFileOffsetLow, UIntPtr dwNumberOfBytesToMap);
    [DllImport("kernel32", SetLastError = true)]
    public static extern bool UnmapViewOfFile(IntPtr lpBaseAddress);
    [DllImport("kernel32", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr hObject);
    [DllImport("kernel32")]
    public static extern uint GetLastError();
}
'@

function Decode-LastErr($code) {
    switch ($code) {
        2  { "ERROR_FILE_NOT_FOUND -- shared-memory object does not exist (HWiNFO not publishing SM / stopped / process exited)" }
        5  { "ERROR_ACCESS_DENIED -- insufficient permission (cross-session / cross-UAC access to Global\ namespace denied; most common when HWiNFO and NingBingJue run at different privilege levels)" }
        6  { "ERROR_INVALID_HANDLE" }
        8  { "ERROR_NOT_ENOUGH_MEMORY" }
        87 { "ERROR_INVALID_PARAMETER" }
        default { "Win32 error code $code" }
    }
}

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  HWiNFO Shared-Memory Root-Cause Probe" -ForegroundColor Cyan
Write-Host ("  Run-As: {0}" -f $(if(([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){'Administrator'}else{'Non-Admin User'})) -ForegroundColor Yellow
Write-Host "==================================================" -ForegroundColor Cyan

# [1] HWiNFO processes
Write-Host "`n[1] HWiNFO processes" -ForegroundColor Green
$hw = Get-Process -Name "HWiNFO*","HWiNFO64*","HWiNFO32*" -ErrorAction SilentlyContinue
if (-not $hw) {
    Write-Host "  [!] No HWiNFO process found! This contradicts 'HWiNFO is still running' -- confirm what you are actually seeing." -ForegroundColor Red
} else {
    foreach ($p in $hw) {
        $exePath = $null; $ver = $null
        try { if ($p.Path) { $exePath = $p.Path; $ver = $p.VersionInfo.FileVersion } } catch {}
        if (-not $exePath) {
            try {
                $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $($p.Id)" -ErrorAction SilentlyContinue
                if ($cim -and $cim.ExecutablePath) {
                    $exePath = $cim.ExecutablePath
                    $ver = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($exePath).FileVersion
                }
            } catch {}
        }
        if (-not $ver) { $ver = "(unavailable as Non-Admin; run the .bat as Admin to read it)" }
        if (-not $exePath) { $exePath = "(unavailable)" }
        Write-Host ("  PID={0} Name={1}`n      Path={2}`n      ExeVersion={3}  (this answers 'how to check version')`n      Started={4}" -f $p.Id, $p.ProcessName, $exePath, $ver, $p.StartTime)
    }
}

# [2] shared-memory SM2 / SM3 probe
Write-Host "`n[2] Shared-memory probe (OpenFileMappingA + MapViewOfFile)" -ForegroundColor Green
foreach ($name in @("Global\HWiNFO_SENS_SM2", "Global\HWiNFO_SENS_SM3")) {
    Write-Host ("  -> probing {0}" -f $name)
    $h = [HwiProbe]::OpenFileMappingA([HwiProbe]::FILE_MAP_READ, $false, $name)
    if ($h -eq [IntPtr]::Zero) {
        $err = [HwiProbe]::GetLastError()
        Write-Host ("     [x] open FAILED: {0}" -f (Decode-LastErr $err)) -ForegroundColor Red
        continue
    }
    Write-Host "     [OK] handle opened (h=$h)" -ForegroundColor Green
    $p = [HwiProbe]::MapViewOfFile($h, [HwiProbe]::FILE_MAP_READ, 0, 0, [UIntPtr]::Zero)
    if ($p -eq [IntPtr]::Zero) {
        $err = [HwiProbe]::GetLastError()
        Write-Host ("     [x] map view FAILED: {0}" -f (Decode-LastErr $err)) -ForegroundColor Red
    } else {
        try {
            $sig = [System.Runtime.InteropServices.Marshal]::ReadInt32($p, 0)
            $ver = [System.Runtime.InteropServices.Marshal]::ReadInt32($p, 4)
            $rev = [System.Runtime.InteropServices.Marshal]::ReadInt32($p, 8)
            $pollRaw = [System.Runtime.InteropServices.Marshal]::ReadInt64($p, 12)
            $sigOk = if ($sig -eq 0x53695748) { "OK HWiS(0x53695748)" } else { ("BAD 0x{0:x} (expected 0x53695748 = 'HWiS')" -f $sig) }
            $pollTime = if ($pollRaw -gt 0) { [DateTimeOffset]::FromUnixTimeSeconds($pollRaw).LocalDateTime.ToString("yyyy-MM-dd HH:mm:ss") } else { "unknown" }
            $age = if ($pollRaw -gt 0) { [math]::Round(([DateTimeOffset]::Now.ToUnixTimeSeconds() - $pollRaw) / 3600.0, 2) } else { $null }
            Write-Host ("     [OK] SMI header readable: signature={0} version=0x{1:x} revision={2}" -f $sigOk, $ver, $rev) -ForegroundColor Green
            Write-Host ("        last poll pollTime={0}  (~{1} hours ago)" -f $pollTime, $age)
            if ($age -ne $null -and $age -gt 0.2) {
                Write-Host ("       [!] data is {0} hours STALE -- this is exactly what NingBingJue's stale_after=30s guard flags as 'not fresh' (free-version 12h SM drop / HWiNFO hung)" -f $age) -ForegroundColor Yellow
            }
        } finally {
            [HwiProbe]::UnmapViewOfFile($p) | Out-Null
        }
    }
    [HwiProbe]::CloseHandle($h) | Out-Null
}

# [3] HWiNFO INI SensorsSM switch
Write-Host "`n[3] HWiNFO INI shared-memory switch (SensorsSM=1 publishes SM)" -ForegroundColor Green
$cands = @(
    "$env:ProgramFiles\HWiNFO64\HWiNFO64.INI",
    "${env:ProgramFiles(x86)}\HWiNFO\HWiNFO.INI",
    "$env:ProgramFiles\HWiNFO64\HWiNFO64.INI",
    "$env:LOCALAPPDATA\HWiNFO64\HWiNFO64.INI"
)
if ($hw -and $hw[0].Path) { $cands += (Split-Path $hw[0].Path) + "\HWiNFO64.INI" }
$foundIni = $false
foreach ($c in $cands) {
    if (Test-Path $c) {
        $foundIni = $true
        $sm = (Select-String -Path $c -Pattern "SensorsSM\s*=\s*(\d+)" | Select-Object -First 1)
        if ($sm) {
            $val = $sm.Matches.Groups[1].Value
            if ($val -eq '1') {
                Write-Host ("  INI: {0}`n      SensorsSM={1}  [OK shared-memory enabled]" -f $c, $val) -ForegroundColor Green
            } else {
                Write-Host ("  INI: {0}`n      SensorsSM={1}  [!! disabled -> ROOT CAUSE!]" -f $c, $val) -ForegroundColor Red
            }
        } else {
            Write-Host ("  INI: {0}`n      no SensorsSM line found (defaults to disabled -> ROOT CAUSE!)" -f $c) -ForegroundColor Red
        }
    }
}
if (-not $foundIni) { Write-Host "  No HWiNFO INI found (maybe installed in a non-standard path). Manually enable 'Shared Memory Support' in HWiNFO Settings -> General." -ForegroundColor Yellow }

Write-Host "`n==================================================" -ForegroundColor Cyan
Write-Host "  Paste the full output of BOTH runs back to me. No other logs needed." -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
