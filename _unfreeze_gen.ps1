$ErrorActionPreference = 'Stop'
try { Import-Module VirtualDesktop -DisableNameChecking -ErrorAction Stop }
catch {
    try { Import-Module VirtualDesktop -Force -DisableNameChecking -ErrorAction Stop }
    catch { Write-Output 'VD_ERR:module-missing'; exit 3 }
}
if (-not (Get-Command Move-Window -ErrorAction SilentlyContinue)) {
    Write-Output 'VD_ERR:cmdlet-missing'; exit 3
}
$pid_ = __PID__
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Collections.Generic;
public class WinEnumPid {
    public delegate bool EnumDelegate(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumDelegate lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr hWnd, out int lpdwProcessId);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    public static List<IntPtr> FindByPid(int pid) {
        var result = new List<IntPtr>();
        EnumWindows((hWnd, _) => {
            int p = 0;
            GetWindowThreadProcessId(hWnd, out p);
            if (p == pid && IsWindowVisible(hWnd)) result.Add(hWnd);
            return true;
        }, IntPtr.Zero);
        return result;
    }
}
public class WinFg {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr hWnd, out int lpdwProcessId);
}

'@
# Recursive descendant tree (BFS over the parent->child map built from a single
# WMI query).  Games launched via Steam/Epic/Battle.net often nest several
# levels deep: launcher -> child -> render process, and the REAL window may hang
# off a grandchild.  A one-level scan misses it entirely, which is exactly why
# the window "didn't move".  We also re-scan on every retry because the render
# child may not have created its window yet at freeze time.
function Get-PidTree([int]$root) {
    $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    $byParent = @{}
    foreach ($pr in $all) {
        $pp = 0
        try { $pp = [int]$pr.ParentProcessId } catch {}
        if (-not $byParent.ContainsKey($pp)) {
            $byParent[$pp] = New-Object System.Collections.Generic.List[int]
        }
        $byParent[$pp].Add([int]$pr.ProcessId)
    }
    $seen = New-Object System.Collections.Generic.HashSet[int]
    $queue = New-Object System.Collections.Generic.Queue[int]
    $queue.Enqueue($root); [void]$seen.Add($root)
    while ($queue.Count -gt 0) {
        $cur = $queue.Dequeue()
        if ($byParent.ContainsKey($cur)) {
            foreach ($child in $byParent[$cur]) {
                if ($seen.Add($child)) { $queue.Enqueue($child) }
            }
        }
    }
    return $seen
}
function Resolve-Hwnd([System.Collections.Generic.HashSet[int]]$ids) {
    $candidates = New-Object System.Collections.Generic.List[IntPtr]
    # 1) own MainWindowHandle (covers the common single-window case)
    foreach ($id in $ids) {
        $p = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($p -and $p.MainWindowHandle -ne 0) { $candidates.Add($p.MainWindowHandle) }
    }
    # 2) EnumWindows by PID across the whole tree (UWP / borderless / games whose
    #    MainWindowHandle .NET tracking misses)
    if ($candidates.Count -eq 0) {
        foreach ($id in $ids) {
            $hs = [WinEnumPid]::FindByPid($id)
            foreach ($h in $hs) { $candidates.Add($h) }
        }
    }
    if ($candidates.Count -gt 0) { return $candidates[0] }
    return [IntPtr]::Zero
}
$ids = Get-PidTree $pid_
# Retry a few times AND re-scan the tree each round — the window may appear late.
$hwnd = [IntPtr]::Zero
for ($attempt = 0; $attempt -lt 8; $attempt++) {
    $ids = Get-PidTree $pid_
    $hwnd = Resolve-Hwnd $ids
    if ($hwnd -ne [IntPtr]::Zero) { break }
    Start-Sleep -Milliseconds 500
}
# Last-resort fallback: if the *foreground* window belongs to our process tree
# (i.e. the user is looking at the game they asked to freeze), move that — it is
# almost certainly the right window and far better than silently doing nothing.
if ($hwnd -eq [IntPtr]::Zero) {
    try {
        $fg = [WinFg]::GetForegroundWindow()
        $fgPid = 0
        [void][WinFg]::GetWindowThreadProcessId($fg, [ref]$fgPid)
        if ($ids.Contains($fgPid)) { $hwnd = $fg }
    } catch {}
}
if ($hwnd -eq [IntPtr]::Zero) { Write-Output 'VD_ERR:no-window'; exit 4 }
Write-Output ('VD_HWND=' + [int]$hwnd)
$current = Get-CurrentDesktop
Move-Window -Hwnd $hwnd -Desktop $current
Write-Output 'VD_OK:current'
