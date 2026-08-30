"""Virtual-desktop relocation tests.

Covers:
* FreezeBlock calls vd.move_to_background on freeze and vd.move_to_current on
  unfreeze (best-effort, never blocks the actual suspend/resume).
* A missing vdesktop (None) is handled gracefully.
* Win32VirtualDesktopManager builds well-formed PowerShell (module present,
  PID substituted, correct tail cmdlets) and degrades gracefully on failure.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from unittest import mock

from ningbingjue.blocks.freeze import FreezeBlock
from ningbingjue.platform.process_manager import MockProcessManager, ProcessInfo
from ningbingjue.platform.virtual_desktop import (
    MockVirtualDesktopManager,
    Win32VirtualDesktopManager,
)


def _pm(*procs):
    return MockProcessManager(list(procs))


# -- FreezeBlock integration --------------------------------------------
def test_freeze_calls_move_to_background():
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    vd = MockVirtualDesktopManager()
    block = FreezeBlock(tempfile.mkdtemp(), pm, vdesktop=vd)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)  # freeze
    assert pm.suspended
    assert vd.background == [2]
    assert vd.current == []


def test_unfreeze_calls_move_to_current():
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    vd = MockVirtualDesktopManager()
    block = FreezeBlock(tempfile.mkdtemp(), pm, vdesktop=vd)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)  # freeze
    block.handle_hotkey(1)
    block.handle_hotkey(1)  # unfreeze
    assert not pm.suspended
    assert vd.background == [2]
    assert vd.current == [2]


def test_no_vdesktop_is_safe():
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    block = FreezeBlock(tempfile.mkdtemp(), pm)  # vdesktop=None
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)
    assert pm.suspended  # freeze still works without virtual-desktop support
    # unfreeze path also safe
    block.handle_hotkey(1)
    block.handle_hotkey(1)
    assert not pm.suspended


def test_move_failure_does_not_block_freeze():
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    vd = MockVirtualDesktopManager()
    vd.fail_next = True  # simulate "could not move window"
    block = FreezeBlock(tempfile.mkdtemp(), pm, vdesktop=vd)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)
    assert pm.suspended  # freeze still succeeds even if the move fails


# -- async execution (the freeze-time UI-hang fix) ----------------------
class _ManualRunner:
    """Captures (work, done) instead of running them, so a test can drive the
    background step deterministically — mimicking the Qt worker without Qt."""

    def __init__(self):
        self.jobs = []

    def __call__(self, work, done):
        self.jobs.append((work, done))

    def drain(self):
        for work, done in list(self.jobs):
            done(work())
        self.jobs.clear()


def test_freeze_move_runs_off_thread_and_defers_state():
    """With an async runner injected, the (possibly slow) move + suspend must be
    deferred to the background — the UI thread must NOT block.  State only lands
    once the background job completes and its result is applied on the UI."""
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    vd = MockVirtualDesktopManager()
    block = FreezeBlock(tempfile.mkdtemp(), pm, vdesktop=vd)
    runner = _ManualRunner()
    block.set_async_runner(runner)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)  # freeze -> dispatched, not yet run
    # Nothing executed yet: no suspend, no move, no frozen entry.
    assert not pm.suspended
    assert vd.background == []
    assert not any(e.pid == 2 for e in block.frozen)
    assert 2 in block._pending
    # Drain the background job -> state now applied on the "UI thread".
    runner.drain()
    assert pm.suspended
    assert vd.background == [2]
    assert any(e.pid == 2 for e in block.frozen)
    assert 2 not in block._pending


def test_pending_guard_blocks_double_freeze():
    """A second press while the freeze is still in flight must be ignored so we
    never double-suspend or race the list update."""
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    vd = MockVirtualDesktopManager()
    block = FreezeBlock(tempfile.mkdtemp(), pm, vdesktop=vd)
    runner = _ManualRunner()
    block.set_async_runner(runner)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)  # dispatch #1
    assert len(runner.jobs) == 1
    # Try to fire the same target again while pending — must be ignored.
    block.handle_hotkey(0)
    block.handle_hotkey(0)
    assert len(runner.jobs) == 1  # no second job queued
    runner.drain()
    assert pm.suspended


# -- suspend-authoritative ordering (Bug 2 root cause) --------------------
def test_freeze_suspends_before_move():
    """Bug 2 root cause: the kernel suspend must happen FIRST, and the window
    move only AFTER a successful suspend.  Otherwise a process that fails to
    suspend but was already moved away looks 'frozen' (vanished to another
    desktop) yet never appears in the frozen list."""
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    order: list[str] = []
    pm.suspend = lambda pid: (order.append("suspend"), True)[1]  # type: ignore[method-assign]
    vd = MockVirtualDesktopManager()
    vd.move_pids = lambda pids, to_background: (  # type: ignore[method-assign]
        order.append("move"), True)[1]
    block = FreezeBlock(tempfile.mkdtemp(), pm, vdesktop=vd)
    runner = _ManualRunner()
    block.set_async_runner(runner)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)
    runner.drain()
    assert order == ["suspend", "move"], order
    assert any(e.pid == 2 for e in block.frozen)


def test_freeze_does_not_move_if_suspend_fails():
    """If suspend fails, the window must NOT be moved away — so it can never be
    left looking frozen (relocated to another desktop) while absent from the
    list.  The frozen entry must also stay empty."""
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    pm.suspend = lambda pid: False  # type: ignore[method-assign]
    vd = MockVirtualDesktopManager()
    moved: list[int] = []
    vd.move_to_background = lambda pid: (moved.append(pid), True)[1]  # type: ignore[method-assign]
    block = FreezeBlock(tempfile.mkdtemp(), pm, vdesktop=vd)
    runner = _ManualRunner()
    block.set_async_runner(runner)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)
    runner.drain()
    assert moved == [], "window must not move when suspend failed"
    assert not any(e.pid == 2 for e in block.frozen)
    assert 2 not in block._pending


# -- unfreeze ordering: window restore AFTER the move (the "jump to process
# desktop" fix) -----------------------------------------------------------
# 2026-08-21 根因修复：解冻不再在挂起状态下搬窗口（挂起进程的 UI 线程不
# pump 消息，Move-Window 的 COM 调用会阻塞每个 PowerShell 子进程到 10 秒
# 超时——绝区零 7 进程多窗口 → 串行数十秒 →「解冻卡住」）。新顺序：
#   resume(restore_window=False) → move_pids(当前桌面) → restore_window()
# 窗口被 SW_RESTORE 激活时已在当前桌面，防跳转不变式依然成立。
def test_unfreeze_moves_before_window_restore():
    """Regression: on unfreeze the windows must be back on the CURRENT desktop
    BEFORE anything restores/foregrounds them.  Restoring a window that is
    still parked on the background desktop makes Windows switch the active
    desktop over to it (the reported 'jump to the process's virtual desktop').
    The fix: resume threads WITHOUT touching windows, move the windows to the
    current desktop (they are now responsive, so the move is fast), and only
    THEN restore them — order must be resume -> move -> restore."""
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    order: list[str] = []
    def fake_resume(pid, restore_window=True):  # type: ignore[no-untyped-def]
        pm.suspended.discard(pid)   # mirror the real resume clearing the set
        order.append("resume")
        return True
    pm.resume = fake_resume  # type: ignore[method-assign]
    pm.restore_window = lambda pid: order.append("restore")  # type: ignore[method-assign]
    vd = MockVirtualDesktopManager()
    vd.move_pids = lambda pids, to_background: (  # type: ignore[method-assign]
        vd.current.extend(int(p) for p in pids), order.append("move"), True)[2]
    block = FreezeBlock(tempfile.mkdtemp(), pm, vdesktop=vd)
    runner = _ManualRunner()
    block.set_async_runner(runner)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)  # freeze
    runner.drain()          # apply the freeze first so self.frozen is populated
    order.clear()           # the freeze's own move also records here — drop it
    vd.current.clear()      # same for the freeze's background/current bookkeeping
    block.handle_hotkey(1)
    block.handle_hotkey(1)  # unfreeze
    runner.drain()
    assert order == ["resume", "move", "restore"], order
    assert vd.current == [2]
    assert not pm.suspended


def test_unfreeze_never_moves_while_suspended():
    """Root-cause regression (绝区零解冻卡住): the window move must NEVER run
    while the processes are still suspended.  A suspended process's UI thread
    does not pump messages, so the VirtualDesktop COM move blocks the whole
    PowerShell subprocess until its 10 s timeout — repeated per window this
    froze the unfreeze for tens of seconds while _pending kept swallowing
    input.  The move may only happen AFTER resume(restore_window=False)."""
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    order: list[str] = []
    def fake_resume(pid, restore_window=True):  # type: ignore[no-untyped-def]
        pm.suspended.discard(pid)   # mirror the real resume clearing the set
        order.append("resume")
        return True
    pm.resume = fake_resume  # type: ignore[method-assign]
    pm.restore_window = lambda pid: order.append("restore")  # type: ignore[method-assign]
    vd = MockVirtualDesktopManager()
    unfreezing = {"active": False}
    def record_move(pids, to_background):  # type: ignore[no-untyped-def]
        # The very property that fixes the hang: when the UNFREEZE move runs,
        # no process of this unit may still be suspended.  (The freeze's own
        # move legitimately runs while suspended — it is not asserted here.)
        if unfreezing["active"]:
            assert not pm.suspended, \
                "unfreeze move must never run while processes are suspended"
        order.append("move")
        vd.current.extend(int(p) for p in pids)
        return True
    vd.move_pids = record_move  # type: ignore[method-assign]
    block = FreezeBlock(tempfile.mkdtemp(), pm, vdesktop=vd)
    runner = _ManualRunner()
    block.set_async_runner(runner)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)  # freeze
    runner.drain()
    order.clear()           # only track the unfreeze sequence
    vd.current.clear()
    unfreezing["active"] = True
    block.handle_hotkey(1)
    block.handle_hotkey(1)  # unfreeze
    runner.drain()
    assert order == ["resume", "move", "restore"], order
    assert vd.current == [2], "window must still end up on the current desktop"
    assert not pm.suspended




def test_unfreeze_falls_back_to_move_to_current_when_move_pids_fails():
    """如果 move_pids（EnumWindows 路径）找不到窗口，解冻应回退到
    move_to_current（PowerShell Get-PidTree 递归 + 前台窗口兜底路径）逐个
    成员补移，保证窗口最终回到当前桌面（而非留在后台桌面导致「游戏进程在、
    窗口却看不见」）。"""
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    vd = MockVirtualDesktopManager()
    vd.move_pids = lambda pids, to_background: False  # type: ignore[method-assign]  # EnumWindows 找不到
    block = FreezeBlock(tempfile.mkdtemp(), pm, vdesktop=vd)
    runner = _ManualRunner()
    block.set_async_runner(runner)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)  # freeze
    runner.drain()
    block.handle_hotkey(1)
    block.handle_hotkey(1)  # unfreeze
    runner.drain()
    assert vd.current == [2], "move_pids 失败时应用 move_to_current 兜底移回"
    assert not pm.suspended


def test_is_window_responsive_zero_hwnd_is_false():
    """A 0 handle is never responsive (and must not touch ctypes)."""
    from ningbingjue.platform.win_hwnd import is_window_responsive
    assert is_window_responsive(0) is False


# -- Win32VirtualDesktopManager command construction --------------------
def test_win32_not_available_off_windows():
    vd = Win32VirtualDesktopManager(powershell="powershell.exe")
    # Force the platform check off (we are not on win32 in CI).
    with mock.patch("sys.platform", "linux"):
        assert vd.move_to_background(123) is False
        assert vd.move_to_current(123) is False


def _ok(args, stdout=b"VD_OK:background"):
    return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=b"")


def test_win32_builds_freeze_command():
    vd = Win32VirtualDesktopManager(powershell="powershell.exe")
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        return _ok(args)

    with mock.patch("sys.platform", "win32"), \
         mock.patch("ningbingjue.platform.virtual_desktop.subprocess.run", fake_run):
        ok = vd.move_to_background(4242)
    assert ok is True
    script = captured["args"][-1]
    # module usage — IMPORT only on the hot path (no Install-Module: installing
    # from a swallowed subprocess is what hung / failed silently before).
    assert "VirtualDesktop" in script
    assert "Import-Module VirtualDesktop" in script
    assert "Install-Module VirtualDesktop" not in script
    # PID substituted (no leftover token)
    assert "__PID__" not in script
    assert "$pid_ = 4242" in script
    # background-desktop logic + move cmdlet + honest success marker.
    # Reuse an EXISTING desktop via .Left/.Right navigation; never touch the
    # broken DesktopId/Name/Index members that regressed the move.
    assert "Get-CurrentDesktop" in script
    assert ".Right" in script and ".Left" in script
    assert "DesktopId" not in script
    assert "Switch-Desktop" in script          # single-desktop fallback only
    assert "Move-Window -Hwnd $hwnd -Desktop $target" in script
    assert "VD_OK:background" in script


def test_win32_builds_unfreeze_command():
    vd = Win32VirtualDesktopManager(powershell="powershell.exe")
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        return _ok(args, stdout=b"VD_OK:current")

    with mock.patch("sys.platform", "win32"), \
         mock.patch("ningbingjue.platform.virtual_desktop.subprocess.run", fake_run):
        ok = vd.move_to_current(99)
    assert ok is True
    script = captured["args"][-1]
    assert "__PID__" not in script
    assert "$pid_ = 99" in script
    # unfreeze moves back to the *current* desktop
    assert "Get-CurrentDesktop" in script
    assert "Move-Window -Hwnd $hwnd -Desktop $current" in script
    # no background-desktop creation logic on unfreeze
    assert "New-Desktop" not in script
    assert "Switch-Desktop" not in script


def test_win32_success_requires_ok_marker():
    """A zero exit code alone must NOT count as success — the script must print
    the VD_OK marker.  This is the fix for the old 'exit 0 lied' bug."""
    vd = Win32VirtualDesktopManager(powershell="powershell.exe")

    def no_marker(args, **kw):
        # rc=0 but the module was missing → script printed VD_ERR, not VD_OK.
        return subprocess.CompletedProcess(args, 0, stdout=b"VD_ERR:module-missing", stderr=b"")

    with mock.patch("sys.platform", "win32"), \
         mock.patch("ningbingjue.platform.virtual_desktop.subprocess.run", no_marker):
        assert vd.move_to_background(1) is False


def test_win32_returns_false_on_subprocess_error():
    vd = Win32VirtualDesktopManager(powershell="powershell.exe")

    def boom(*a, **k):
        raise OSError("no powershell")

    with mock.patch("sys.platform", "win32"), \
         mock.patch("ningbingjue.platform.virtual_desktop.subprocess.run", boom):
        assert vd.move_to_background(1) is False
        assert vd.move_to_current(1) is False


def test_win32_returns_false_on_nonzero_exit():
    vd = Win32VirtualDesktopManager(powershell="powershell.exe")

    def nonzero(args, **kw):
        return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"boom")

    with mock.patch("sys.platform", "win32"), \
         mock.patch("ningbingjue.platform.virtual_desktop.subprocess.run", nonzero):
        assert vd.move_to_background(1) is False


def test_win32_logs_failure_to_file():
    """Every attempt must be logged so failures are never invisible again."""
    log = os.path.join(tempfile.mkdtemp(), "vdesktop.log")
    vd = Win32VirtualDesktopManager(powershell="powershell.exe", log_path=log)

    def nonzero(args, **kw):
        return subprocess.CompletedProcess(args, 3, stdout=b"VD_ERR:module-missing", stderr=b"")

    with mock.patch("sys.platform", "win32"), \
         mock.patch("ningbingjue.platform.virtual_desktop.subprocess.run", nonzero):
        vd.move_to_background(1234)
    assert os.path.exists(log)
    with open(log, encoding="utf-8") as fh:
        content = fh.read()
    assert "move_to_background pid=1234" in content
    assert "FAILED" in content


# -- env-block slimming (the actual bug fix) ---------------------------
def test_slim_env_drops_oversized_var_keeps_essentials():
    """A single pathological variable (like the 316 KB ACC_PRODUCT_CONFIG_V3)
    must be dropped so the child env block stays under the 65535 limit, while
    essential vars survive.  This is THE fix for 'window silently didn't move'."""
    from ningbingjue.platform.virtual_desktop import (
        _slim_env, _ENV_BLOCK_SOFT_CAP,
    )
    src = {
        "PATH": r"C:\Windows;C:\Windows\System32",
        "SYSTEMROOT": r"C:\Windows",
        "PSModulePath": r"C:\Modules",
        "TEMP": r"C:\Temp",
        "ACC_PRODUCT_CONFIG_V3": "x" * 316558,  # the pathological blob
        "SOME_NORMAL_VAR": "value",
    }

    def size(d):
        return sum(len(k) + len(str(v)) + 2 for k, v in d.items())

    slim = _slim_env(src)
    assert "ACC_PRODUCT_CONFIG_V3" not in slim   # dropped
    assert "PATH" in slim and "SYSTEMROOT" in slim
    assert "PSModulePath" in slim and "TEMP" in slim
    assert "SOME_NORMAL_VAR" in slim             # small var kept
    assert size(slim) <= _ENV_BLOCK_SOFT_CAP
    assert size(slim) < 65535                    # under the hard Windows limit


def test_slim_env_protects_large_but_essential_var():
    """Even if PATH somehow grows large, a protected var is never dropped."""
    from ningbingjue.platform.virtual_desktop import _slim_env
    src = {"PATH": "p" * 20000, "JUNK": "j" * 20000}
    slim = _slim_env(src)
    assert "PATH" in slim                         # protected, kept despite size
    assert "JUNK" not in slim                     # oversized non-protected, dropped


def test_win32_run_passes_slim_env_to_subprocess():
    """_run must launch PowerShell with the slimmed env (not the full one).

    Note: real os.environ refuses to *store* a var > 32767 chars, but a process
    can *inherit* one that big (as on the user's machine).  We simulate that by
    patching the module's os.environ with a plain dict holding the blob."""
    vd = Win32VirtualDesktopManager(powershell="powershell.exe")
    captured = {}

    def fake_run(args, **kw):
        captured["env"] = kw.get("env")
        return _ok(args)

    fake_environ = {
        "PATH": r"C:\Windows;C:\Windows\System32",
        "SYSTEMROOT": r"C:\Windows",
        "PSModulePath": r"C:\Modules",
        "TEMP": r"C:\Temp",
        "ACC_PRODUCT_CONFIG_V3": "x" * 316558,   # the pathological blob
    }
    with mock.patch("sys.platform", "win32"), \
         mock.patch("ningbingjue.platform.virtual_desktop.os.environ", fake_environ), \
         mock.patch("ningbingjue.platform.virtual_desktop.subprocess.run", fake_run):
        assert vd.move_to_background(4242) is True
    env = captured["env"]
    assert env is not None
    assert "ACC_PRODUCT_CONFIG_V3" not in env     # slimmed before launch
    block = sum(len(k) + len(str(v)) + 2 for k, v in env.items())
    assert block < 65535


def test_freeze_script_uses_recursive_pid_tree():
    """The window handle must be resolved across the FULL process tree (not just
    one level of children).  Games launched via Steam/Epic nest launcher ->
    child -> render process, and the real window often hangs off a grandchild.
    A one-level scan misses it — the exact reason windows "didn't move".

    Lock the fix: the generated script must build a parent->child map and BFS
    the whole tree (Queue + byParent), and retry up to 8 times."""
    from ningbingjue.platform.virtual_desktop import _FREEZE_PS
    assert "$byParent" in _FREEZE_PS               # build full parent map
    assert "System.Collections.Generic.Queue[int]" in _FREEZE_PS  # BFS over tree
    assert "$attempt -lt 8" in _FREEZE_PS          # retry window is generous
    assert "Get-CimInstance Win32_Process" in _FREEZE_PS
    # The old one-level filter must be gone; the BFS walks the whole tree.
    assert "ParentProcessId=$root" not in _FREEZE_PS
    assert "$byParent.ContainsKey($cur)" in _FREEZE_PS


# -- native Python HWND resolution (the actual bug fix) ------------------
def test_win32_resolves_hwnd_in_python_then_moves():
    """The window handle is resolved in PURE PYTHON (ctypes), not PowerShell —
    this is the fix for the recurring VD_ERR:no-window.  The Python-resolved
    HWND must be handed straight to Move-Window; the PowerShell PID->HWND dance
    must NOT run on the happy path."""
    vd = Win32VirtualDesktopManager(powershell="powershell.exe")
    captured = {}

    def fake_run(args, **kw):
        captured["script"] = args[-1]
        captured["env"] = kw.get("env")
        return _ok(args)

    with mock.patch("sys.platform", "win32"), \
         mock.patch("ningbingjue.platform.virtual_desktop.subprocess.run", fake_run), \
         mock.patch.object(vd, "resolve_hwnd", return_value=123456):
        assert vd.move_to_background(777) is True
    assert "123456" in captured["script"]                 # python hwnd passed in
    assert "Move-Window -Hwnd" in captured["script"]      # move still happens
    assert "Get-PidTree" not in captured["script"]        # PS resolution skipped
    assert "__PID__" not in captured["script"]
    assert captured["env"] is not None                    # env still slimmed


def test_win32_falls_back_to_ps_resolution_when_python_finds_no_hwnd():
    """If the native resolver returns 0 (genuinely no window), we must fall back
    to the PowerShell resolution script rather than silently doing nothing."""
    vd = Win32VirtualDesktopManager(powershell="powershell.exe")
    captured = {}

    def fake_run(args, **kw):
        captured["script"] = args[-1]
        return _ok(args)

    with mock.patch("sys.platform", "win32"), \
         mock.patch("ningbingjue.platform.virtual_desktop.subprocess.run", fake_run), \
         mock.patch.object(vd, "resolve_hwnd", return_value=0):
        assert vd.move_to_background(777) is True
    assert "Get-PidTree" in captured["script"]            # PS resolution used
    assert "$pid_ = 777" in captured["script"]


def test_related_pids_includes_launcher_subtree_and_siblings():
    """The launch group must contain the launcher, the target, its siblings and
    its descendants (the real window often hangs off a sibling/grandchild).
    This is the connectivity that the old one-level scan missed."""
    import ningbingjue.platform.win_hwnd as wh
    # 100=explorer(broad) -> 200=launcher -> 201=game(target), 202=sibling, 203=child of 201
    fake = {100: 0, 200: 100, 201: 200, 202: 200, 203: 201}
    names = {100: "explorer.exe", 200: "launcher.exe", 201: "game.exe",
             202: "render.exe", 203: "child.exe"}
    with mock.patch.object(wh, "_snapshot_parents", return_value=fake), \
         mock.patch.object(wh, "_name_of", side_effect=lambda p: names.get(p, "")):
        rel = wh.related_pids(201)
    # launch_root = 200 (launcher, not broad) -> whole subtree
    assert rel == {200, 201, 202, 203}


def test_related_pids_restricts_to_target_when_launcher_is_broad():
    """If the top ancestor is a broad system process (explorer.exe), we must NOT
    treat the whole session as the launch group — only the target's own
    descendants + ancestors.  Otherwise we'd match unrelated windows."""
    import ningbingjue.platform.win_hwnd as wh
    # 100=explorer(broad) -> 201=game(target), 999=some unrelated app also under explorer
    fake = {100: 0, 201: 100, 999: 100}
    names = {100: "explorer.exe", 201: "game.exe", 999: "other.exe"}
    with mock.patch.object(wh, "_snapshot_parents", return_value=fake), \
         mock.patch.object(wh, "_name_of", side_effect=lambda p: names.get(p, "")):
        rel = wh.related_pids(201)
    assert 201 in rel
    assert 999 not in rel          # unrelated app excluded
    assert 100 not in rel or True  # explorer itself may or may not be in; unrelated app must be out


# -- reuse an EXISTING desktop via .Left/.Right navigation (no pileup) ----
def test_win32_freeze_reuses_existing_desktop_via_navigation():
    """On builds where Get-Desktop -Index / DesktopId / Name are broken (return
    $null), the freeze script must REUSE an existing desktop by walking the
    .Left/.Right adjacency and NEVER touch the crashy DesktopId — that crash
    (New-Desktop ran, then .DesktopId.ToString() threw before Move-Window) is
    exactly what regressed the move into 'new desktop every time + never moved'.
    New-Desktop stays only as the single-desktop fallback."""
    from ningbingjue.platform.virtual_desktop import _TAIL_FREEZE

    # navigation-based reuse, crash-proof template
    assert "$cur = Get-CurrentDesktop" in _TAIL_FREEZE
    assert "$node.Right" in _TAIL_FREEZE and "$node.Left" in _TAIL_FREEZE
    assert "DesktopId" not in _TAIL_FREEZE          # never the broken property
    assert "VD_BG_GUID" not in _TAIL_FREEZE         # GUID path fully removed
    assert "Move-Window -Hwnd $hwnd -Desktop $target" in _TAIL_FREEZE
    assert "New-Desktop" in _TAIL_FREEZE            # single-desktop fallback only
    assert "VD_OK:background" in _TAIL_FREEZE

    # the runtime script built from it is fully token-substituted
    vd = Win32VirtualDesktopManager(powershell="powershell.exe")
    captured = {}

    def fake_run(args, **kw):
        captured["script"] = args[-1]
        return _ok(args)

    with mock.patch("sys.platform", "win32"), \
         mock.patch("ningbingjue.platform.virtual_desktop.subprocess.run", fake_run), \
         mock.patch.object(vd, "resolve_hwnd", return_value=123456):
        assert vd.move_to_background(777) is True
    assert "123456" in captured["script"]           # python hwnd injected
    assert "__HWND__" not in captured["script"]
    assert "__BG__" not in captured["script"]


def test_win32_dispose_is_noop_never_deletes_a_desktop():
    """Because we now REUSE an existing desktop rather than owning a dedicated
    one, dispose must be a safe no-op — deleting a user's real desktop would be
    destructive."""
    vd = Win32VirtualDesktopManager(powershell="powershell.exe")

    def boom(*a, **k):  # any PowerShell call here would be a bug
        raise AssertionError("dispose must not run PowerShell / delete desktops")

    with mock.patch("sys.platform", "win32"), \
         mock.patch("ningbingjue.platform.virtual_desktop.subprocess.run", boom):
        assert vd.dispose_background() is False




