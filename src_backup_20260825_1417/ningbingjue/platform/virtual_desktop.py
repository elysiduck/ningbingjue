"""Virtual-desktop window relocation — moves a process's main window between
Windows virtual desktops.

Faithful port of the mechanism in ``Move-WindowToDesktop.ps1``: that script
relies on MScholtes' ``VirtualDesktop`` PowerShell module, which wraps the
undocumented Windows virtual-desktop COM interfaces and auto-selects the right
one per OS build.  We reuse the exact same module and drive it from Python via
``powershell.exe`` / ``pwsh.exe``.

Why the earlier version "silently didn't work"  (ROOT CAUSE — confirmed)
------------------------------------------------------------------------
The move is done by PowerShell in BOTH the reference .ps1 and here — Python only
launches PowerShell as a subprocess.  The reference script "worked" because it
ran **interactively** from a clean shell.  The App's subprocess did not, and the
real, machine-verified reason is the **65535-byte child-environment-block limit**:

    Importing ``VirtualDesktop`` runs ``Add-Type`` -> spawns ``csc.exe`` to
    compile its C#.  ``csc.exe`` is a child process, so Windows must hand it an
    environment block, which is capped at 65535 bytes.  On this machine the
    inherited env block is **325146 bytes** — one single variable,
    ``ACC_PRODUCT_CONFIG_V3``, is **316558 bytes** all by itself.  csc.exe
    therefore cannot start, ``Add-Type`` throws, ``Import-Module`` fails, and
    ``Move-Window`` never exists — so the window never moves.  Exactly the same
    class of failure the user hit before with csc's 65535 limit.

    Fix: when Python launches ``powershell.exe`` we pass a **slimmed env**
    (``_slim_env``) that drops abnormally large variables, keeping the block far
    under 65535 so the downstream csc.exe can start.  Small, essential vars
    (PATH / SYSTEMROOT / PSModulePath / TEMP ...) are always preserved.

    PID-based, as the user asked: freeze/unfreeze are PID-anchored, so the runtime
    script resolves PID -> HWND (own window -> child windows -> EnumWindows by
    PID) before calling ``Move-Window`` (which needs an HWND).

Additional hardening:

1. **No install on the hot path.**  The module is *vendored* inside the
   distribution (``src/ningbingjue/vendor/VirtualDesktop``) and imported by
   absolute path at runtime via ``-FullyQualifiedName`` (used because some
   PowerShell builds, e.g. 5.1.26100, lack ``-Path``); a ``-Name`` attempt with
   the bundle dir prepended to ``$env:PSModulePath`` is the fallback, then the
   system-installed module.  So no per-machine ``Install-Module`` and no
   admin/non-admin scope dance is needed (the exe is always elevated).  If the
   bundle is somehow absent, it falls back to a system-installed module; the
   standalone doctor script (``write_diagnostic``) remains the repair path.
2. **Honest success detection.**  A move only counts as success when the script
   prints the ``VD_OK`` marker on stdout (the old code trusted ``exit 0``, so a
   missing module reported success while nothing moved).
3. **Always logged.**  Every attempt appends the exe, pid and full stdout/stderr
   to ``vdesktop.log`` so failures are never invisible again.

Design notes (mirrors the project's "接口抽象 + Mock" seam):
* ``VirtualDesktopManager`` is the abstract contract.
* ``Win32VirtualDesktopManager`` is the real implementation (Windows only).
* ``MockVirtualDesktopManager`` records calls so the freeze logic stays testable
  headless / on non-Windows hosts.

Behavior wired into the freeze block:
* **freeze**  -> ``move_to_background(pid)``: send the window to a background
  virtual desktop (the last desktop that is NOT the current one).  If only one
  desktop exists we create a new one (and immediately switch the user back).
* **unfreeze** -> ``move_to_current(pid)``: bring the window back to the desktop
  the user is currently on.

Both operations are *best-effort*: any failure returns ``False`` and MUST NOT
block the actual suspend/resume — the window simply stays where it is.
"""

from __future__ import annotations

import abc
import os
import shutil
import subprocess
import sys
import time

from .base import ExternalAdapter

_PID_TOKEN = "__PID__"
# Bounded backstop.  The move runs OFF the UI thread (see FreezeBlock's async
# runner), so a slow move never freezes the app; this cap simply guarantees a
# hung ``Move-Window`` (e.g. on a non-cooperative window whose UI thread does
# not pump messages) is abandoned in seconds instead of the old 30 s.  A warm
# ``Import-Module`` + move completes in well under a second; a cold csc.exe
# compile on first run is a few seconds, so 10 s leaves comfortable headroom.
_PS_TIMEOUT_S = 10
# Overall budget for a multi-window ``move_pids`` call.  Each window spawns its
# own PowerShell subprocess (Import-Module + Move-Window ≈ 0.7 s warm); a hung /
# non-cooperative window can still burn the full 10 s subprocess timeout.  With
# several visible windows (a 7-process game unit can have 3+), serial execution
# without a cap could stall the unfreeze for tens of seconds.  Once the budget
# is exhausted we stop trying further windows and report what already moved.
# (2026-08-21: measured Move-Window at ~680 ms per window even on a *suspended*
# process — the old "suspend ⇒ COM blocks 10 s" assumption was wrong; the real
# risk is N-window serialisation, so we cap the TOTAL, not per-window.)
_MOVE_BUDGET_S = 15.0
# Success marker the PowerShell scripts print on stdout.  We require this to be
# present (not merely exit-code 0) before reporting a successful move.
_OK_MARKER = "VD_OK"

# --- child-env slimming (the actual bug fix) ------------------------------
# Windows caps a child process's environment block at 65535 bytes.  Import of
# the VirtualDesktop module spawns csc.exe (via Add-Type); if the inherited env
# block exceeds that cap, csc.exe cannot start and the import fails.  We build a
# slimmed env that drops abnormally large variables while ALWAYS keeping the
# essentials, then pass it to the PowerShell subprocess.
#
# Strategy is size-based (not a strict whitelist) so we keep maximum
# compatibility: normal variables are tiny and survive untouched; only the rare
# pathological blob (e.g. ACC_PRODUCT_CONFIG_V3 ~ 316 KB) gets dropped.
_ENV_BLOCK_SOFT_CAP = 60000          # keep well under the 65535 hard limit
_ENV_VAR_MAX_BYTES = 16384           # any single var bigger than this is suspect
# Never drop these no matter what — the module / csc.exe / powershell need them.
_ENV_PROTECTED = frozenset(k.upper() for k in (
    "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PSMODULEPATH", "USERNAME", "USERDOMAIN", "COMPUTERNAME",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "NUMBER_OF_PROCESSORS",
    "SYSTEMDRIVE", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "PROGRAMDATA", "PUBLIC", "ALLUSERSPROFILE", "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)", "DRIVERDATA", "OS",
))


def _slim_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of the environment small enough for a child env block.

    Drops (a) any single variable larger than ``_ENV_VAR_MAX_BYTES`` and then,
    if still over ``_ENV_BLOCK_SOFT_CAP``, the largest remaining non-protected
    variables until the total fits.  Protected keys are never removed.
    """
    src = dict(os.environ if source is None else source)

    def _size(k: str, v: str) -> int:
        return len(k) + len(str(v)) + 2  # "K=V\0"

    kept: dict[str, str] = {}
    for k, v in src.items():
        v = "" if v is None else str(v)
        if k.upper() in _ENV_PROTECTED:
            kept[k] = v
            continue
        if _size(k, v) > _ENV_VAR_MAX_BYTES:
            continue  # drop the oversized blob
        kept[k] = v

    total = sum(_size(k, v) for k, v in kept.items())
    if total > _ENV_BLOCK_SOFT_CAP:
        # Drop largest non-protected vars until we fit.
        droppable = sorted(
            ((k, v) for k, v in kept.items() if k.upper() not in _ENV_PROTECTED),
            key=lambda kv: _size(kv[0], kv[1]), reverse=True,
        )
        for k, v in droppable:
            if total <= _ENV_BLOCK_SOFT_CAP:
                break
            total -= _size(k, v)
            kept.pop(k, None)
    return kept

# --- PowerShell building blocks -------------------------------------------
# Runtime header: IMPORT ONLY.  We never Install-Module on the hot path (that is
# what hung / failed silently in a swallowed subprocess).  If the module is not
# importable we print a machine-readable reason and exit non-zero.
_PROBE_HEADER = """$ErrorActionPreference = 'Stop'
try { Import-Module VirtualDesktop -DisableNameChecking -ErrorAction Stop }
catch {
    try { Import-Module VirtualDesktop -Force -DisableNameChecking -ErrorAction Stop }
    catch { Write-Output 'VD_ERR:module-missing'; exit 3 }
}
if (-not (Get-Command Move-Window -ErrorAction SilentlyContinue)) {
    Write-Output 'VD_ERR:cmdlet-missing'; exit 3
}
"""

# --- bundled-module resolution (vendored VirtualDesktop) ------------------
# The module is shipped inside the distribution (see build_exe.py / spec
# ``--add-data ... ningbingjue/vendor/VirtualDesktop``) so we can import it by
# ABSOLUTE PATH and avoid BOTH the per-machine Install-Module step AND the
# admin/non-admin PSModulePath scope mismatch that broke the move on the
# handheld (the exe is always elevated via uac-admin).  Falls back to the
# system-installed module (doctor flow) when no bundle is present.
def _bundled_module_dir() -> str | None:
    """Return the absolute path to the vendored VirtualDesktop module, or None.

    Resolves correctly for the frozen onedir layout AND running from source.
    """
    candidates: list[str] = []
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        # onedir COLLECT layout: <exe>/_internal/ningbingjue/vendor/VirtualDesktop
        candidates.append(os.path.join(
            exe_dir, "_internal", "ningbingjue", "vendor", "VirtualDesktop"))
        # onefile / _MEIPASS extraction fallback
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(
                meipass, "ningbingjue", "vendor", "VirtualDesktop"))
    # Source layout: platform/ -> ningbingjue/ -> vendor/VirtualDesktop
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "..", "vendor", "VirtualDesktop"))
    for c in candidates:
        c = os.path.abspath(c)
        if (os.path.isfile(os.path.join(c, "VirtualDesktop.psd1"))
                or os.path.isfile(os.path.join(c, "VirtualDesktop.psm1"))):
            return c
    return None


def _import_header() -> str:
    """PowerShell header that imports the VirtualDesktop module.

    Prefers the vendored copy (loaded by absolute path via -FullyQualifiedName,
    which exists on PowerShell builds where -Path is unavailable, e.g.
    5.1.26100); falls back to prepending the bundle dir to $env:PSModulePath
    + -Name (still uses our shipped copy, elevation/scope independent), then to
    the system-installed module (doctor flow).  The vendored copy is always
    preferred over any system install.
    """
    mdir = _bundled_module_dir()
    if mdir:
        safe_dir = mdir.replace("'", "''")
        parent = os.path.dirname(mdir).replace("'", "''")
        return (
            "$ErrorActionPreference = 'Stop'\n"
            "$__nbjMod = Join-Path '%s' 'VirtualDesktop.psd1'\n"
            "$__ok = $false\n"
            "if (Test-Path $__nbjMod) {\n"
            "  try { Import-Module -FullyQualifiedName $__nbjMod -Force "
            "-DisableNameChecking -ErrorAction Stop; $__ok = $true }\n"
            "  catch { $__ok = $false }\n"
            "}\n"
            "if (-not $__ok) {\n"
            "  $env:PSModulePath = '%s' + [System.IO.Path]::PathSeparator "
            "+ $env:PSModulePath\n"
            "  try { Import-Module VirtualDesktop -Force -DisableNameChecking "
            "-ErrorAction Stop }\n"
            "  catch { Write-Output 'VD_ERR:module-missing'; exit 3 }\n"
            "}\n"
            "if (-not (Get-Command Move-Window -ErrorAction SilentlyContinue)) { "
            "Write-Output 'VD_ERR:cmdlet-missing'; exit 3 }\n"
        ) % (safe_dir, parent)
    # No bundle present: rely on the system-installed module (doctor flow).
    return _PROBE_HEADER


# Resolve the target process's main window handle FROM ITS PID.  PID is the
# anchor (freeze/unfreeze are PID-based); the VirtualDesktop module's
# ``Move-Window`` needs an HWND, so we translate here.  Order:
#   1. the process's own MainWindowHandle (covers most apps / games);
#   2. any child process's MainWindowHandle (launchers that spawn the real game);
#   3. EnumWindows over the PID and its children (UWP / borderless edge cases).
_WINENUM_CS = """using System;
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
"""

_RESOLVE_HWND = """$pid_ = __PID__
Add-Type -TypeDefinition @'
<CS>
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
"""

# Freeze: move the window to an EXISTING background desktop, reusing it every
# time so we never pile up new desktops.
#
# Why navigation instead of index/GUID/name (machine-verified ROOT CAUSE):
#   On this machine the MScholtes wrapper's ``Get-Desktop -Index``,
#   ``$d.DesktopId`` and ``$d.Name`` are ALL broken — they return $null.  The
#   previous GUID approach called ``$target.DesktopId.ToString()`` which throws
#   on the $null value BEFORE ``Move-Window`` ever runs.  That is exactly the
#   regression the user hit: ``New-Desktop`` had already executed (so a new
#   desktop appeared) but the script crashed before moving the window (so the
#   window "didn't move").
#   The Desktop object's ``.Left`` / ``.Right`` adjacency links come straight
#   from the COM layer and DO work here, so we walk them to reach an existing
#   background desktop and reuse it.  We only ``New-Desktop`` when the machine
#   truly has a single desktop and there is nothing to reuse.  This template
#   NEVER touches the broken DesktopId / Name / Index members, so it cannot
#   crash the way the GUID version did.
_TAIL_FREEZE = """$cur = Get-CurrentDesktop
$target = $null
try {
    # Reuse an EXISTING desktop: walk to the far edge in one direction so every
    # frozen window consistently lands on the same background desktop.
    $node = $cur; $steps = 0
    while ($node.Right -and $steps -lt 64) { $node = $node.Right; $steps++; $target = $node }
    if ($null -eq $target) {
        $node = $cur; $steps = 0
        while ($node.Left -and $steps -lt 64) { $node = $node.Left; $steps++; $target = $node }
    }
} catch { $target = $null }
if ($null -eq $target) {
    # Only one desktop exists -> create exactly one background desktop and jump
    # the user straight back so the freeze never relocates them to it.
    $target = New-Desktop
    try { Switch-Desktop -Desktop $cur } catch {}
}
Move-Window -Hwnd $hwnd -Desktop $target
Write-Output 'VD_OK:background'
"""

# Unfreeze: move the window back to whatever desktop the user is currently on.
_TAIL_UNFREEZE = """$current = Get-CurrentDesktop
Move-Window -Hwnd $hwnd -Desktop $current
Write-Output 'VD_OK:current'
"""

_FREEZE_PS = _import_header() + _RESOLVE_HWND.replace("<CS>", _WINENUM_CS) + _TAIL_FREEZE
_UNFREEZE_PS = _import_header() + _RESOLVE_HWND.replace("<CS>", _WINENUM_CS) + _TAIL_UNFREEZE

# When Python (ctypes) has already resolved the window handle, we skip the
# PowerShell PID->HWND dance entirely and hand the HWND straight to Move-Window.
# This is the robust path: window-finding is done natively, PowerShell only does
# the (undocumented-COM) move.
_HWND_TOKEN = "__HWND__"
_MOVE_HWND = "$hwnd = [IntPtr]__HWND__\n"
_MOVE_BACKGROUND_PS = _import_header() + _MOVE_HWND + _TAIL_FREEZE
_MOVE_CURRENT_PS = _import_header() + _MOVE_HWND + _TAIL_UNFREEZE

# One-off interactive installer (used by the doctor script, NOT the hot path).
_INSTALL_PS = """$ErrorActionPreference = 'Stop'
if (-not (Get-Module -ListAvailable VirtualDesktop)) {
    Write-Host '[*] 正在安装 VirtualDesktop 模块...' -ForegroundColor Yellow
    Install-Module VirtualDesktop -Scope CurrentUser -Force -SkipPublisherCheck
}
Import-Module VirtualDesktop -Force -DisableNameChecking
"""


class VirtualDesktopManager(ExternalAdapter, abc.ABC):
    """Contract for relocating a process window across virtual desktops."""

    @abc.abstractmethod
    def move_to_background(self, pid: int) -> bool:
        """Move ``pid``'s main window to a background virtual desktop.

        Returns ``True`` only when the move actually succeeded (module present,
        window found, ``Move-Window`` reported ``VD_OK``).  Never raises.
        """
        ...

    @abc.abstractmethod
    def move_to_current(self, pid: int) -> bool:
        """Move ``pid``'s main window back to the current virtual desktop.

        Returns ``True`` only on real success.  Never raises.
        """
        ...

    @abc.abstractmethod
    def move_pids(self, pids: Any, to_background: bool) -> bool:
        """Move EVERY visible window owned by ANY PID in ``pids`` to the
        background (``to_background=True``) or the current (``False``) virtual
        desktop.

        Unlike ``move_to_background``/``move_to_current`` (which resolve a single
        target's "launch group" via the process tree), this moves ONLY the exact
        PIDs handed in — so a freeze/unfreeze action relocates precisely the
        processes it suspended and can never drag in an unrelated app that merely
        shares a process-tree ancestor (e.g. unfreezing Steam must not also yank
        a game the user froze separately).  Returns ``True`` if at least one
        window was moved, ``False`` if no visible window matched any PID (best-
        effort; never blocks suspend/resume).  Never raises.
        """
        ...


class Win32VirtualDesktopManager(VirtualDesktopManager):
    """Real implementation backed by the PowerShell ``VirtualDesktop`` module."""

    def __init__(self, powershell: str | None = None,
                 log_path: str | None = None) -> None:
        self.log_path = log_path
        # Reuse an EXISTING background desktop via .Left/.Right navigation (see
        # _TAIL_FREEZE) — no per-App state to persist, no dedicated desktop to
        # own.  This is deliberate: the GUID-based approach was broken on this
        # machine (DesktopId returns $null) and is what regressed the move.
        if powershell:
            self._exes = [powershell]
        else:
            # Windows PowerShell (powershell.exe) and PowerShell 7 (pwsh.exe)
            # have SEPARATE module directories — the module may only be present
            # in one.  Try both and use whichever can actually load it.
            self._exes = [p for p in (
                shutil.which("powershell.exe"),
                shutil.which("pwsh.exe"),
            ) if p]

    @property
    def available(self) -> bool:
        return sys.platform == "win32" and bool(self._exes)

    # -- logging ---------------------------------------------------------
    def _log(self, msg: str) -> None:
        if not self.log_path:
            return
        try:
            d = os.path.dirname(self.log_path)
            if d:
                os.makedirs(d, exist_ok=True)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(f"[{ts}] {msg}\n")
        except Exception:
            pass

    def log_boot(self) -> None:
        """Write a one-line marker at wiring time so the log ALWAYS exists.

        If the user launches the app and finds no ``vdesktop.log`` at all, it
        proves stale code / wrong working dir — not a PowerShell failure."""
        self._log(f"boot: available={self.available} exes={self._exes}")

    def tail(self, n: int = 25) -> str:
        """Return the last ``n`` lines of the log (for in-UI display)."""
        if not self.log_path or not os.path.exists(self.log_path):
            return ""
        try:
            with open(self.log_path, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            return "\n".join(lines[-n:])
        except Exception:
            return ""

    # -- core runner -----------------------------------------------------
    def _run_capture(self, script: str, log_label: str) -> tuple[bool, str]:
        """Run the PowerShell script and return (succeeded, stdout).  stdout is
        surfaced so callers can read markers like VD_BG_GUID out of it."""
        if not self.available:
            self._log(f"{log_label}: skipped (not on Windows / no PowerShell)")
            return False, ""
        slim = _slim_env()
        full_bytes = sum(len(k) + len(str(v)) + 2 for k, v in os.environ.items())
        slim_bytes = sum(len(k) + len(str(v)) + 2 for k, v in slim.items())
        if slim_bytes < full_bytes:
            self._log(
                f"{log_label}: env slimmed {full_bytes}->{slim_bytes} bytes "
                f"(cap {_ENV_BLOCK_SOFT_CAP})"
            )
        last_out = ""
        for exe in self._exes:
            try:
                proc = subprocess.run(
                    [exe, "-NoProfile", "-NonInteractive",
                     "-ExecutionPolicy", "Bypass", "-Command", script],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    check=False, timeout=_PS_TIMEOUT_S, env=slim,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                out = (proc.stdout or b"").decode("utf-8", "replace").strip()
                err = (proc.stderr or b"").decode("utf-8", "replace").strip()
                last_out = out
                if proc.returncode == 0 and _OK_MARKER in out:
                    self._log(f"{log_label}: OK via {os.path.basename(exe)} :: {out}")
                    return True, out
                last = f"rc={proc.returncode} out={out!r} err={err[:400]!r}"
                self._log(f"{log_label}: FAILED via {os.path.basename(exe)} :: {last}")
            except Exception as e:  # noqa: BLE001
                last = repr(e)
                self._log(f"{log_label}: EXCEPTION via {os.path.basename(exe)} :: {last}")
        self._log(f"{log_label}: all PowerShell hosts failed; last -> {last}")
        return False, last_out

    def _run(self, script: str, log_label: str) -> bool:
        ok, _ = self._run_capture(script, log_label)
        return ok

    @staticmethod
    def _norm_pid(pid: int) -> int | None:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None
        return pid if pid > 0 else None

    def resolve_hwnd(self, pid: int) -> int:
        """Resolve ``pid``'s window handle in PURE PYTHON (ctypes).

        No PowerShell involved — this is far more robust for games whose window
        hangs off a deep / sibling child process, and every candidate is logged
        so failures are diagnosable.  Returns the HWND or 0."""
        from .win_hwnd import resolve_hwnd as _resolve
        try:
            return int(_resolve(pid, log=self._log))
        except Exception as e:  # noqa: BLE001
            self._log(f"resolve_hwnd pid={pid}: EXCEPTION :: {e!r}")
            return 0

    def move_to_background(self, pid: int) -> bool:
        p = self._norm_pid(pid)
        if p is None:
            return False
        # Primary: resolve the HWND natively in Python, then hand it to the move.
        try:
            hwnd = self.resolve_hwnd(p)
        except Exception as e:  # noqa: BLE001
            self._log(f"move_to_background pid={p}: python resolve EXCEPTION :: {e!r}")
            hwnd = 0
        if hwnd:
            return self._run(
                _MOVE_BACKGROUND_PS.replace(_HWND_TOKEN, str(hwnd)),
                log_label=f"move_to_background pid={p} hwnd={hwnd}",
            )
        # Last resort: let PowerShell try to resolve (its own retry loop).
        self._log(f"move_to_background pid={p}: python found no hwnd; "
                  f"falling back to PowerShell resolution")
        return self._run(
            _FREEZE_PS.replace(_PID_TOKEN, str(p)),
            log_label=f"move_to_background pid={p}",
        )

    def move_to_current(self, pid: int) -> bool:
        p = self._norm_pid(pid)
        if p is None:
            return False
        try:
            hwnd = self.resolve_hwnd(p)
        except Exception as e:  # noqa: BLE001
            self._log(f"move_to_current pid={p}: python resolve EXCEPTION :: {e!r}")
            hwnd = 0
        if hwnd:
            return self._run(
                _MOVE_CURRENT_PS.replace(_HWND_TOKEN, str(hwnd)),
                log_label=f"move_to_current pid={p} hwnd={hwnd}",
            )
        self._log(f"move_to_current pid={p}: python found no hwnd; "
                  f"falling back to PowerShell resolution")
        return self._run(_UNFREEZE_PS.replace(_PID_TOKEN, str(p)),
                         log_label=f"move_to_current pid={p}")

    def move_pids(self, pids: Any, to_background: bool) -> bool:
        """PID-RESTRICTED bulk move (see the abstract contract).  Resolves every
        visible window owned by any PID in ``pids`` natively (ctypes) and moves
        each to the target desktop.  Never expands to a process-tree launch group,
        so it can't cross-contaminate a sibling app."""
        pset = {int(p) for p in pids if p and int(p) > 0}
        if not pset:
            return False
        try:
            from .win_hwnd import find_hwnds_for_pids
            hwnds = find_hwnds_for_pids(pset, log=self._log)
        except Exception as e:  # noqa: BLE001
            self._log(f"move_pids pids={sorted(pset)}: resolve EXCEPTION :: {e!r}")
            hwnds = []
        if not hwnds:
            self._log(f"move_pids pids={sorted(pset)}: no visible window matched")
            return False
        script = _MOVE_BACKGROUND_PS if to_background else _MOVE_CURRENT_PS
        ok = True
        deadline = time.monotonic() + _MOVE_BUDGET_S
        done = 0
        for hwnd in hwnds:
            if time.monotonic() > deadline:
                self._log(f"move_pids pids={sorted(pset)}: total budget "
                          f"{_MOVE_BUDGET_S:.0f}s exhausted; skipping remaining "
                          f"windows (moved {done}/{len(hwnds)})")
                break
            r = self._run(
                script.replace(_HWND_TOKEN, str(hwnd)),
                log_label=f"move_pids pids={sorted(pset)} hwnd={hwnd}",
            )
            ok = r and ok
            done += 1
        return ok

    def dispose_background(self) -> bool:
        """No-op.  The robust freeze path REUSES an existing desktop (walking the
        .Left/.Right adjacency) rather than owning a dedicated one, so there is
        nothing to tear down — and deleting a desktop the user already had would
        be destructive.  Kept for API compatibility with the freeze block."""
        return False

    # -- one-time setup / diagnostics -----------------------------------
    def module_ready(self) -> bool:
        """Quick probe: is the VirtualDesktop module importable right now?"""
        return self._run(_import_header() + "Write-Output 'VD_OK:probe'",
                         log_label="module_ready probe")

    def write_diagnostic(self, target_dir: str) -> str | None:
        """Drop a double-clickable doctor script next to ``vdesktop.log``.

        Returns the .bat path (or None on failure).  ASCII filenames only, to
        avoid the Windows non-ASCII path encoding trap.  The .ps1 is written
        UTF-8-BOM so PowerShell 5.1 reads its Chinese output correctly."""
        try:
            os.makedirs(target_dir, exist_ok=True)
            ps_path = os.path.join(target_dir, "vdesktop-doctor.ps1")
            bat_path = os.path.join(target_dir, "vdesktop-doctor.bat")
            with open(ps_path, "w", encoding="utf-8-sig") as fh:
                fh.write(_DOCTOR_PS)
            with open(bat_path, "w", encoding="ascii") as fh:
                fh.write(
                    "@echo off\r\n"
                    "powershell.exe -NoProfile -ExecutionPolicy Bypass "
                    "-File \"%~dp0vdesktop-doctor.ps1\"\r\n"
                    "pause\r\n"
                )
            return bat_path
        except Exception:
            return None


class MockVirtualDesktopManager(VirtualDesktopManager):
    """Records calls; used in headless tests and on non-Windows hosts."""

    def __init__(self) -> None:
        self.background: list[int] = []
        self.current: list[int] = []
        self.fail_next = False
        self.bg_guid: str | None = None

    def dispose_background(self) -> bool:
        # No real desktop to tear down under test; just record the call.
        self.bg_guid = None
        return True

    @property
    def available(self) -> bool:
        return True

    def move_to_background(self, pid: int) -> bool:
        if self.fail_next:
            return False
        self.background.append(int(pid))
        return True

    def move_to_current(self, pid: int) -> bool:
        if self.fail_next:
            return False
        self.current.append(int(pid))
        return True

    def move_pids(self, pids: Any, to_background: bool) -> bool:
        if self.fail_next:
            return False
        target = self.background if to_background else self.current
        for p in pids:
            target.append(int(p))
        return True


# --- Standalone interactive doctor (double-click to diagnose/fix) ----------
# Run by the user in a real interactive session: installs the module (answering
# the NuGet prompt), lists desktops, and moves a chosen PID's window to a
# background desktop — proving the mechanism works on their machine.
_DOCTOR_PS = r"""# 凝冰结魄 - 虚拟桌面移动诊断 / 一键修复
# 用途：确认「按 PID 把窗口移到后台虚拟桌面」在你机器上能否工作。
# 这是交互式脚本，可安装模块、回答 NuGet 提示——正是 App 子进程里做不到的。

$ErrorActionPreference = 'Stop'
function Line($t,$c='Gray'){ Write-Host $t -ForegroundColor $c }

Line "==== 凝冰结魄 虚拟桌面诊断 ====" 'Cyan'
Line ("PowerShell 版本: " + $PSVersionTable.PSVersion) 'DarkGray'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Line ("是否管理员运行: " + $admin) 'DarkGray'
Line ""

# 0) 环境块瘦身（关键）：导入模块会用 Add-Type 启动 csc.exe，
#    子进程环境块上限 65535 字节。若本机有超大环境变量（如 ACC_PRODUCT_CONFIG_V3），
#    会导致 csc.exe 无法启动、Import-Module 失败。这里先移除超大变量。
Line "[0] 检查环境块大小..." 'Yellow'
$envBytes = 0
foreach ($e in [Environment]::GetEnvironmentVariables().GetEnumerator()) {
    $envBytes += ($e.Key.Length + $e.Value.ToString().Length + 2)
}
Line ("    当前环境块: $envBytes 字节 (上限 65535)" ) $(if ($envBytes -gt 65535) { 'Red' } else { 'DarkGray' })
if ($envBytes -gt 60000) {
    Get-ChildItem Env: | Where-Object { ($_.Name.Length + $_.Value.Length + 2) -gt 16384 } | ForEach-Object {
        Line ("    移除超大环境变量: " + $_.Name + " (" + $_.Value.Length + " 字节)") 'Yellow'
        Remove-Item ("Env:\" + $_.Name) -ErrorAction SilentlyContinue
    }
    $envBytes = 0
    foreach ($e in [Environment]::GetEnvironmentVariables().GetEnumerator()) {
        $envBytes += ($e.Key.Length + $e.Value.ToString().Length + 2)
    }
    Line ("    瘦身后环境块: $envBytes 字节" ) 'Green'
}
Line ""

# 1) 模块
Line "[1] 检查 VirtualDesktop 模块..." 'Yellow'
$mod = Get-Module -ListAvailable VirtualDesktop
# 以管理员运行时装到 AllUsers，这样无论 App 以普通用户还是管理员启动都能找到模块。
# 这正是「App 里窗口不动、但手动能移」的头号原因：模块只装在普通用户 scope。
$scope = if ($admin) { 'AllUsers' } else { 'CurrentUser' }
if (-not $mod) {
    Line "    未安装，正在从 PowerShell Gallery 安装（首次可能提示安装 NuGet，输入 Y 回车）..." 'Yellow'
    Line ("    以 $scope 权限安装（与 App 运行权限一致）。") 'DarkGray'
    try {
        Install-Module VirtualDesktop -Scope $scope -Force -SkipPublisherCheck
        Line "    安装完成。" 'Green'
    } catch {
        Line "    安装失败: $($_.Exception.Message)" 'Red'
        Line "    可手动运行: Install-Module VirtualDesktop -Scope $scope -Force -SkipPublisherCheck" 'Red'
        return
    }
} else {
    Line ("    已安装，版本: " + ($mod.Version -join ',')) 'Green'
    if ($admin) {
        # 已装但当前是管理员：确认 AllUsers 也有，避免 App 以管理员运行时找不到。
        $allUsers = $mod | Where-Object { $_.ModuleBase -like '*\Program Files\*' -or $_.ModuleBase -like '*\WindowsPowerShell\Modules' -and $false }
        if (-not ($mod.Path -like '*\Program Files\*')) {
            Line "    检测到以管理员运行，补装一份 AllUsers 副本以免 App(管理员) 找不到模块..." 'Yellow'
            try { Install-Module VirtualDesktop -Scope AllUsers -Force -SkipPublisherCheck } catch {}
        }
    }
}
Import-Module VirtualDesktop -Force -DisableNameChecking
if (-not (Get-Command Move-Window -ErrorAction SilentlyContinue)) {
    Line "    模块导入后仍找不到 Move-Window，模块可能损坏。" 'Red'; return
}
Line "    模块可用。" 'Green'
Line ""

# 2) 桌面数量
$count = Get-DesktopCount
$current = Get-CurrentDesktop
Line "[2] 当前虚拟桌面数量: $count" 'Yellow'
if ($count -lt 2) { Line "    只有 1 个桌面，诊断时会自动新建一个用于测试。" 'DarkGray' }
Line ""

# 3) 选目标 PID
Line "[3] 选择要移动的进程" 'Yellow'
$inPid = Read-Host "    输入进程 PID（直接回车则测试记事本 notepad）"
if ([string]::IsNullOrWhiteSpace($inPid)) {
    $np = Get-Process notepad -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $np) {
        Line "    没有记事本在运行，正在打开一个用于测试..." 'DarkGray'
        Start-Process notepad; Start-Sleep -Milliseconds 800
        $np = Get-Process notepad -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    $thePid = $np.Id
} else { $thePid = [int]$inPid }
Line "    目标 PID = $thePid" 'Green'

# 4) PID -> HWND
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
            int p = 0; GetWindowThreadProcessId(hWnd, out p);
            if (p == pid && IsWindowVisible(hWnd)) result.Add(hWnd);
            return true;
        }, IntPtr.Zero);
        return result;
    }
}
'@
function Get-PidTree([int]$root){
    $all=Get-CimInstance Win32_Process -EA SilentlyContinue
    $byParent=@{}
    foreach($pr in $all){ $pp=0; try{$pp=[int]$pr.ParentProcessId}catch{}; if(-not $byParent.ContainsKey($pp)){$byParent[$pp]=New-Object System.Collections.Generic.List[int]}; $byParent[$pp].Add([int]$pr.ProcessId) }
    $seen=New-Object System.Collections.Generic.HashSet[int]; $q=New-Object System.Collections.Generic.Queue[int]; $q.Enqueue($root); [void]$seen.Add($root)
    while($q.Count -gt 0){ $cur=$q.Dequeue(); if($byParent.ContainsKey($cur)){ foreach($c in $byParent[$cur]){ if($seen.Add($c)){ $q.Enqueue($c) } } } }
    return $seen
}
$hwnd=[IntPtr]::Zero
foreach($id in (Get-PidTree $thePid)){ $p=Get-Process -Id $id -EA SilentlyContinue; if($p -and $p.MainWindowHandle -ne 0){$hwnd=$p.MainWindowHandle; break} }
if($hwnd -eq [IntPtr]::Zero){ foreach($id in (Get-PidTree $thePid)){ $hs=[WinEnumPid]::FindByPid($id); if($hs.Count -gt 0){$hwnd=$hs[0]; break} } }
Line "[4] 解析窗口句柄: $hwnd" 'Yellow'
if($hwnd -eq [IntPtr]::Zero){ Line "    找不到该 PID 的可见窗口（可能是无界面进程）。" 'Red'; return }

# 5) 移到后台桌面 —— 用 .Left/.Right 导航复用已有桌面（Index/DesktopId 在部分
#    Win10/11 版本会返回 $null，故不可依赖；导航链来自 COM 层，实测可用）。
Line "[5] 移动到后台虚拟桌面..." 'Yellow'
$target=$null
try {
    $node=$current; $steps=0
    while($node.Right -and $steps -lt 64){ $node=$node.Right; $steps++; $target=$node }
    if($null -eq $target){ $node=$current; $steps=0; while($node.Left -and $steps -lt 64){ $node=$node.Left; $steps++; $target=$node } }
} catch { $target=$null }
if($null -eq $target){ $target=New-Desktop; try{ Switch-Desktop -Desktop $current }catch{} }
try {
    Move-Window -Hwnd $hwnd -Desktop $target
    Line "    成功！窗口已移到后台桌面。用 Win+Tab 应能看到它在另一个桌面。" 'Green'
    Line "    3 秒后自动移回当前桌面..." 'DarkGray'
    Start-Sleep -Seconds 3
    Move-Window -Hwnd $hwnd -Desktop (Get-CurrentDesktop)
    Line "    已移回。诊断通过 —— 你的机器支持该功能。" 'Green'
    Line ""
    Line ">> 结论：机制可用。如果 App 里仍不生效，多半是 App 以管理员运行、" 'Cyan'
    Line "   而模块只装在了普通用户 scope。请在【与 App 相同的权限】下再跑一次本脚本安装模块。" 'Cyan'
} catch {
    Line "    Move-Window 失败: $($_.Exception.Message)" 'Red'
    Line "    多为模块版本与当前 Windows 版本不匹配，尝试: Update-Module VirtualDesktop" 'Red'
}
"""

__all__ = [
    "VirtualDesktopManager",
    "Win32VirtualDesktopManager",
    "MockVirtualDesktopManager",
]
