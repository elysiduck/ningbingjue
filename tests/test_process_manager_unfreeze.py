"""ProcessManager suspend/resume counter-semantics regression tests.

These tests pin the root cause of 「冻结后无法解冻恢复」:

    NtResumeProcess is *counter* semantics (each call only decrements the
    kernel suspend count by 1).  The old resume() called it exactly once, so
    a process whose counter had been pushed to 2 (e.g. suspend() stacking when
    is_suspended_native() returned None) could never be fully released — the
    process stayed frozen in kernel while the ledger was cleared, so the HUD
    reported 「已解冻」 but the freeze was permanent.

The mock below reuses the REAL ProcessManager.suspend / resume control flow
(guards + drain loop) but swaps the ctypes kernel calls for a pure-Python
suspend counter.  This is the only way to exercise the counter semantics
without a real Windows kernel handle.
"""

from __future__ import annotations

from ningbingjue.platform.process_manager import ProcessManager, Win32ProcessManager


class _FakeNt:
    """Drop-in for ``self._ntdll``: NtSuspendProcess/+1, NtResumeProcess/-1."""

    def __init__(self, pm: "CounterPM", fail_resume: bool = False) -> None:
        self.pm = pm
        self.fail_resume = fail_resume

    def NtSuspendProcess(self, h: int) -> int:
        self.pm._counter[h] = self.pm._counter.get(h, 0) + 1
        return 0

    def NtResumeProcess(self, h: int) -> int:
        if self.fail_resume:
            return 0xC0000001  # STATUS_UNSUCCESSFUL — simulate syscall failure
        c = self.pm._counter.get(h, 0)
        if c > 0:
            self.pm._counter[h] = c - 1
        return 0


class _FakeKernel:
    def CloseHandle(self, h: int) -> int:  # noqa: D401, N802 (ctypes name)
        return 0


class CounterPM(Win32ProcessManager):
    """Win32ProcessManager with the ctypes layer replaced by a suspend counter.

    Does NOT call super().__init__ (which would spin up real Windows ctypes
    state).  We only set the fields suspend()/resume() actually touch, and
    override the kernel primitives so the REAL guard/drain logic runs as-is.
    suspend()/resume()/is_suspended() delegate back to Win32ProcessManager so
    we exercise the genuine counter-semantics code, not MockProcessManager's
    counter-free shims.
    """

    def __init__(self, fail_resume: bool = False) -> None:
        self._ntdll = _FakeNt(self, fail_resume=fail_resume)
        self._kernel32 = _FakeKernel()  # must be non-None (suspend/resume gate)
        self._suspended: set[int] = set()
        self._counter: dict[int, int] = {}      # pid -> kernel suspend count
        self._native_fail: set[int] = set()      # pids whose query -> None

    # -- kernel primitives swapped for counter semantics -------------------
    def _handle(self, pid: int):  # noqa: A002 (matches ProcessManager signature)
        self._counter.setdefault(pid, 0)
        return pid  # handle == pid for the fake

    def _set_window_state(self, pid: int, minimized: bool) -> None:
        return None  # no-op in unit tests

    def _is_protected_system_process(self, pid: int) -> bool:
        return False  # never block in tests

    def is_suspended_native(self, pid: int):
        if pid in self._native_fail:
            return None
        return self._counter.get(pid, 0) > 0

    # -- abstract-method stubs; suspend/resume/is_suspended delegate to the
    #    REAL ProcessManager implementations so we exercise the genuine guard
    #    + drain logic (not MockProcessManager's counter-free shims). --------
    def suspend(self, pid: int) -> bool:
        return Win32ProcessManager.suspend(self, pid)

    def resume(self, pid: int, restore_window: bool = True) -> bool:
        return Win32ProcessManager.resume(self, pid, restore_window)

    def is_suspended(self, pid: int) -> bool:
        return Win32ProcessManager.is_suspended(self, pid)

    @property
    def available(self) -> bool:
        return True

    def enumerate(self):  # type: ignore[override]
        return []

    def is_running(self, name: str) -> bool:  # type: ignore[override]
        return False

    def start(self, path: str, **kw: object) -> bool:  # type: ignore[override]
        return False

    def stop(self, pid_or_name: int | str) -> bool:  # type: ignore[override]
        return False

    def stop_matching(self, cmd_substr: str) -> bool:  # type: ignore[override]
        return False


def test_resume_drains_double_suspend():
    """REGRESSION: a process whose kernel suspend counter is already 2 (e.g.
    inherited from a previous session, or suspended externally) must be fully
    resumed by ONE resume() call.  Before the fix resume() called
    NtResumeProcess exactly once, leaving the counter at 1 — the process
    stayed frozen in kernel while the ledger was cleared, i.e.
    「冻结后无法解冻恢复」.

    NBJ's OWN suspend is now idempotent (the guard fix) and must NOT push the
    counter from 2 to 3; resume() is what does the draining.
    """
    pm = CounterPM()
    pm._counter[100] = 2   # simulate a pre-existing double suspension
    assert pm.suspend(100) is True   # NBJ re-suspend is idempotent, no stacking
    assert pm._counter[100] == 2

    ok = pm.resume(100)
    assert ok is True
    assert pm._counter[100] == 0            # fully drained
    assert pm.is_suspended_native(100) is False
    assert 100 not in pm._suspended


def test_suspend_does_not_stack_when_native_query_fails():
    """REGRESSION: when is_suspended_native() returns None (query flaky) but the
    process is ALREADY suspended (kernel counter == 1), suspend() must NOT
    stack it to 2.  The pre-fix guard used `is True` which let None through and
    stacked — the root cause of the un-drainable double suspend."""
    pm = CounterPM()
    assert pm.suspend(200) is True          # counter -> 1, real suspend
    pm._native_fail.add(200)               # query now intermittently fails
    before = pm._counter[200]

    ok = pm.suspend(200)                    # re-suspend attempt while None
    assert ok is True
    assert pm._counter[200] == before      # NOT stacked to 2

    # and it can still be fully resumed afterwards
    pm._native_fail.discard(200)
    assert pm.resume(200) is True
    assert pm._counter[200] == 0


def test_resume_single_suspend_unchanged():
    """Sanity: the normal single freeze -> single unfreeze still works (no
    over-draining, no spurious failure)."""
    pm = CounterPM()
    assert pm.suspend(300) is True
    assert pm._counter[300] == 1
    assert pm.resume(300) is True
    assert pm._counter[300] == 0
    assert 300 not in pm._suspended


def test_resume_reports_failure_when_truly_stuck():
    """If NtResumeProcess keeps failing (returns non-zero), resume() must report
    False and keep the ledger entry — NOT silently claim success and leave the
    user thinking the freeze is gone."""
    pm = CounterPM(fail_resume=True)
    assert pm.suspend(400) is True
    assert pm.resume(400) is False
    assert 400 in pm._suspended          # ledger retained on failure
    assert pm._counter[400] == 1         # still suspended in kernel
