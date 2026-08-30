"""Pure-Python (ctypes) window-handle resolver for virtual-desktop relocation.

Why this module exists
----------------------
The earlier PowerShell-based PID -> HWND resolution kept failing with
``VD_ERR:no-window`` for games.  Games launched via Steam / Epic / Battle.net
nest several process levels (launcher -> child -> render process) and the REAL
window often hangs off a process that is NOT a direct child of the PID the user
froze.  Doing the enumeration in Python via ctypes gives us full control AND
full logging, so failures are diagnosable instead of invisible.

What stays in PowerShell
------------------------
The actual *move* still uses the MScholtes ``VirtualDesktop`` module (via a thin
PowerShell call) because that module wraps the UNDOCUMENTED, build-specific
virtual-desktop COM interfaces.  Re-implementing ``IVirtualDesktopManagerInternal``
per Windows build in ctypes is exactly the rabbit hole the module exists to solve.
Everything window-FINDING related is now native Python; PowerShell is no longer
responsible for locating your window — which was the bug.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

# Process names whose subtree is too broad to treat as a single "launch group"
# (e.g. explorer.exe owns half the session).  When the top ancestor is one of
# these we restrict matching to the target's own descendants + ancestors only.
_BROAD_PROCESSES = {
    "system", "system idle process", "services.exe", "wininit.exe",
    "lsass.exe", "svchost.exe", "csrss.exe", "smss.exe", "explorer.exe",
    "runtimebroker.exe", "dllhost.exe", "taskhostw.exe", "fontdrvhost.exe",
    "conhost.exe", "powershell.exe", "python.exe", "pythonw.exe",
    "cmd.exe", "code.exe", "devenv.exe",
}

# Known multi-process "platform" apps whose process tree must NEVER be treated
# as a launch group by an app launched THROUGH them.  Steam is the canonical
# case: a game (or Mod Organizer 2) launched via Steam is a CHILD of steam.exe.
# If we climbed into steam.exe while resolving a game's window we would pull
# Steam's whole tree — including an unrelated app the user froze separately (e.g.
# MO2) — into the move, dragging that app to the wrong virtual desktop.  We stop
# the ancestor climb at these roots so a freeze/unfreeze of one app never
# sweeps in a sibling app that merely shares Steam as a common ancestor.
_FAMILY_ROOTS = {
    "steam.exe", "steamwebhelper.exe",
}


def _libs():
    """Lazily grab the Windows libs (only called on Windows)."""
    return (
        ctypes.windll.user32,
        ctypes.windll.kernel32,
        ctypes.windll.psapi,
    )


def is_window_responsive(hwnd: int, timeout_ms: int = 700) -> bool:
    """Whether ``hwnd`` pumps messages within ``timeout_ms``.

    THIS IS THE KEY GUARD against the freeze-time UI hang.  The virtual-desktop
    move (MScholtes ``Move-Window`` -> ``IApplicationViewCollection::GetViewForHwnd``)
    talks to the target window's UI thread.  If that thread is not pumping
    messages, the COM call blocks for the WHOLE PowerShell subprocess timeout
    (~30 s in the old code) — which is exactly what froze the app when the user
    tried to relocate a hung / non-cooperative window (e.g. WeChat's
    ``Qt51514QWindowIcon`` window).

    We probe with ``SendMessageTimeout(WM_NULL, SMTO_ABORTIFHUNG)``: a benign
    no-op message that a healthy window answers instantly, while a hung window
    makes the call return **0 fast** (thanks to ``SMTO_ABORTIFHUNG``) instead of
    waiting the full timeout.  Used to PREFER a responsive window when several
    candidates exist, so the move lands on the one that can actually be moved.

    Returns ``True`` when the window answers, ``False`` when it is hung.  On any
    probe error we return ``True`` — never wrongly reject a window we simply
    could not test (the caller's bounded timeout still protects us)."""
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
        ]
        user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        WM_NULL = 0x0000
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_size_t(0)
        ret = user32.SendMessageTimeoutW(
            wintypes.HWND(hwnd), WM_NULL, 0, 0,
            SMTO_ABORTIFHUNG, int(timeout_ms), ctypes.byref(result),
        )
        return bool(ret)
    except Exception:
        return True


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.CHAR * 260),
    ]


def _snapshot_parents() -> dict[int, int]:
    """Return ``{pid: ppid}`` for every process via CreateToolhelp32Snapshot."""
    user32, kernel32, psapi = _libs()
    TH32CS_SNAPPROCESS = 0x00000002
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value:
        return {}
    out: dict[int, int] = {}
    try:
        pe = _PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(pe)
        if kernel32.Process32First(snap, ctypes.byref(pe)):
            while True:
                out[pe.th32ProcessID] = pe.th32ParentProcessID
                if not kernel32.Process32Next(snap, ctypes.byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(snap)
    return out


def _name_of(pid: int) -> str:
    """Best-effort process image name via psapi."""
    try:
        _, kernel32, psapi = _libs()
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_INFORMATION
        if not h:
            h = kernel32.OpenProcess(0x0400, False, pid)  # QUERY_LIMITED
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            if psapi.GetProcessImageFileNameW(h, buf, 260):
                return buf.value.rsplit("\\", 1)[-1].lower()
            return ""
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return ""


def _children_map(parents: dict[int, int]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    return children


def _descendants(root: int, children: dict[int, list[int]]) -> set[int]:
    seen: set[int] = set()
    stack = [root]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for c in children.get(cur, ()):
            if c not in seen:
                stack.append(c)
    return seen


def _ancestors(target: int, parents: dict[int, int]) -> list[int]:
    chain: list[int] = []
    cur = target
    guard = 0
    while cur and guard < 64:
        ppid = parents.get(cur, 0)
        if not ppid or ppid == cur:
            break
        chain.append(ppid)
        cur = ppid
        guard += 1
    return chain


def related_pids(target: int) -> set[int]:
    """PIDs considered "the same app" as ``target`` (the launch group).

    Walk UP the parent chain until we hit pid 0 / a system PID / a BROAD process
    (``explorer.exe`` etc.).  The highest non-broad ancestor becomes the
    ``launch_root``; we then take its WHOLE subtree.  This catches
    ``launcher -> game -> child`` AND sibling windows (the render process and the
    actual window process are both children of the launcher).

    Crucially we stop AT a broad process but still include the launcher (its
    child) and that launcher's full subtree — so a sibling render process that
    owns the real window is NOT excluded.  We never climb INTO a broad process,
    which would otherwise match half the session.
    """
    parents = _snapshot_parents()
    children = _children_map(parents)
    cur = target
    launch_root = target
    guard = 0
    while guard < 64:
        ppid = parents.get(cur, 0)
        if not ppid or ppid == cur or ppid <= 4:
            break
        if _name_of(ppid) in _BROAD_PROCESSES:
            break  # don't climb into the broad process
        if _name_of(ppid) in _FAMILY_ROOTS:
            break  # don't climb into a platform app (Steam) — see _FAMILY_ROOTS
        launch_root = ppid
        cur = ppid
        guard += 1
    return _descendants(launch_root, children) | {launch_root}


def resolve_hwnd(target: int, log=None) -> int:
    """Return the HWND of a visible, titled top-level window belonging to the
    target's process, else ``0``.

    ``log`` is an optional ``callable(str)`` for diagnostics (e.g. the vdesktop
    log writer) so every candidate and the final pick are recorded.

    Selection is **tiered** so we always pick the RIGHT window:
      Tier 1  — a window owned by ``target`` itself (the common single-exe case)
      Tier 2  — a window owned by a descendant of ``target`` (game -> render child)
      Tier 3  — a window owned by a non-broad ancestor (rare: launcher owns UI)
      Tier 4  — last resort: the launch group, EXCLUDING broad system processes
                (explorer.exe etc. own half the session and are never the target)
    This guarantees we never grab an unrelated window (e.g. an explorer window
    that happens to be the target's parent) just because it shares a launcher.
    """
    if not target or target <= 0:
        return 0
    user32, _, _ = _libs()
    parents = _snapshot_parents()
    children = _children_map(parents)
    desc = _descendants(target, children)
    anc = _ancestors(target, parents)
    broad = related_pids(target)
    if log:
        log(f"hwnd: target={target} desc={sorted(desc)} anc={anc}")
        log(f"hwnd: target={target} broad={sorted(broad)}")

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: list[tuple[int, int, str]] = []

    def _cb(hwnd, lparam):  # noqa: ANN001
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in broad:
            length = user32.GetWindowTextLengthW(hwnd)
            title = ""
            if length and length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
            found.append((hwnd, pid.value, title))
            if log:
                log(f"hwnd: candidate hwnd={hwnd} pid={pid.value} title={title!r}")
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)

    if not found:
        if log:
            log(f"hwnd: NO visible window matched for target={target}")
        return 0

    # Broad system processes (explorer.exe ...) must never be the chosen window.
    broad_pids = {f[1] for f in found if _name_of(f[1]) in _BROAD_PROCESSES}
    anc_set = set(anc)

    def _pick(pool):
        if not pool:
            return None
        titled = [f for f in pool if f[2]]
        untitled = [f for f in pool if not f[2]]
        # Prefer a window that actually PUMPS MESSAGES: relocating an
        # unresponsive window makes the virtual-desktop COM move block for the
        # whole subprocess timeout (the freeze-time hang).  Probe titled windows
        # first (they are the real UI), then untitled ones.
        for f in titled + untitled:
            if is_window_responsive(f[0]):
                if log:
                    log(f"hwnd: responsive pick hwnd={f[0]} pid={f[1]} "
                        f"title={f[2]!r}")
                return f
        # None answered — still return the best titled window and let the
        # caller's bounded timeout handle a possibly-slow move (better to try
        # than to silently do nothing).
        if log:
            log("hwnd: no responsive window in pool; falling back to "
                "best-titled (a slow/hung move is bounded by timeout)")
        return titled[0] if titled else pool[0]

    exact = [f for f in found if f[1] == target]
    child = [f for f in found if f[1] in desc and f[1] != target]
    anc_nb = [f for f in found if f[1] in anc_set and f[1] not in broad_pids]
    rest = [f for f in found if f[1] not in broad_pids]

    for pool, tier in ((exact, "exact"), (child, "child"),
                       (anc_nb, "ancestor"), (rest, "rest")):
        b = _pick(pool)
        if b:
            if log:
                log(f"hwnd: SELECTED[{tier}] hwnd={b[0]} pid={b[1]} title={b[2]!r}")
            return int(b[0])
    if log:
        log(f"hwnd: only broad-system windows found for target={target}; refusing")
    return 0


def find_hwnds_for_pids(targets: set[int], log=None) -> list[int]:
    """Return the HWNDs of every visible top-level window owned by ANY PID in
    ``targets``.

    This is the PID-RESTRICTED counterpart of :func:`resolve_hwnd`.  Where
    ``resolve_hwnd`` expands to a "launch group" (which can accidentally include
    an unrelated sibling app sharing a process-tree ancestor — e.g. unfreezing
    Steam dragging a separately-frozen MO2 to the current desktop), this helper
    moves ONLY the windows whose owner PID is in the exact set the caller
    suspended.  The freeze block captures that set at freeze time and passes it
    here, so a move can never cross-contaminate another frozen app.
    """
    targets = {int(p) for p in targets if p and int(p) > 0}
    if not targets:
        return []
    user32, _, _ = _libs()
    found: list[int] = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, lparam):  # noqa: ANN001
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in targets:
            found.append(int(hwnd))
            if log:
                length = user32.GetWindowTextLengthW(hwnd)
                title = ""
                if length and length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value
                log(f"hwnd: (restricted) candidate hwnd={hwnd} pid={pid.value} "
                    f"title={title!r}")
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return found


def get_foreground_process_name() -> str:
    """Return the lower-cased exe name of the foreground window's process.

    Used by the RTSS *auto-lock* feature to distinguish "low GPU load because
    we're in a game's 2D menu" from "low GPU load because the desktop / 凝冰结魄
    itself is focused".  Returns ``""`` when there is no foreground window or the
    lookup fails (callers treat ``""`` as "don't trust this signal").
    """
    try:
        user32, kernel32, psapi = _libs()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        h = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_INFORMATION
        if not h:
            h = kernel32.OpenProcess(0x0400, False, pid.value)  # QUERY_LIMITED
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            if psapi.GetProcessImageFileNameW(h, buf, 260):
                return buf.value.rsplit("\\", 1)[-1].lower()
            return ""
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return ""


__all__ = [
    "resolve_hwnd", "related_pids", "is_window_responsive",
    "find_hwnds_for_pids", "get_foreground_process_name",
]
