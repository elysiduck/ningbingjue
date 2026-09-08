"""Regression tests for Win32ProcessManager enumeration.

Guards the real bug fixed on 2026-07-18: ``wmic`` reorders the requested
columns, so the CSV header comes back as
``Node,CommandLine,Name,ProcessId,SessionId,WorkingSetSize`` — NOT the
positional order the old code assumed.  Parsing by fixed index made
``int(parts[1])`` choke on a CommandLine string and dropped every row, which
left the freeze block permanently empty.

We feed a mocked wmic CSV (deliberately reordered) and assert ProcessId is
resolved by *header name*, the session/self filters apply, and the CIM
fallback is exercised when wmic yields nothing.
"""

from __future__ import annotations

import subprocess
import types
from unittest import mock

from ningbingjue.platform.process_manager import Win32ProcessManager


def _patched_manager() -> Win32ProcessManager:
    """Build a Win32ProcessManager whose ctypes handles are stubbed so the real
    ``suspend`` control flow runs without touching the OS."""
    pm = Win32ProcessManager()
    fake_nt = types.SimpleNamespace()
    fake_nt.NtSuspendProcess = lambda h: 0  # success
    fake_k = types.SimpleNamespace()
    fake_k.OpenProcess = lambda *a, **k: 1
    fake_k.CloseHandle = lambda *a, **k: 1
    pm._ntdll = fake_nt
    pm._kernel32 = fake_k
    pm._user32 = types.SimpleNamespace()
    pm._is_protected_system_process = lambda pid: False
    return pm


def test_suspend_minimizes_before_ntsuspend():
    """Bug 1 root cause: a suspended window's thread cannot process the
    SC_MINIMIZE message, so minimizing AFTER NtSuspendProcess is a no-op and the
    window stays restored on the (background) virtual desktop.  Minimize must
    happen FIRST, while the process is still responsive."""
    pm = _patched_manager()
    calls: list[str] = []

    def traced_set_window_state(pid, minimized):
        calls.append("minimize")

    pm._set_window_state = traced_set_window_state

    assert pm.suspend(4242) is True
    assert calls == ["minimize"], (
        f"minimize must run before NtSuspendProcess, got call order: {calls}"
    )
    assert 4242 in pm._suspended


def test_suspend_records_suspended_only_on_success():
    """If NtSuspendProcess fails, the process must not be tracked as suspended
    (and minimize must still have run first — state stays consistent)."""
    pm = _patched_manager()
    pm._ntdll.NtSuspendProcess = lambda h: 0xC0000001  # STATUS_UNSUCCESSFUL
    assert pm.suspend(99) is False
    assert 99 not in pm._suspended



def _wmic_csv_reordered() -> str:
    # Note the real-world reordering: CommandLine sits right after Node.
    return (
        "Node,CommandLine,Name,ProcessId,SessionId,WorkingSetSize\r\n"
        "PC,,explorer.exe,1234,1,524288000\r\n"
        'PC,"C:\\games\\game.exe -fullscreen",game.exe,5678,1,2097152000\r\n'
        "PC,,System,4,0,7995392\r\n"  # session 0 -> must be filtered out
        "PC,,self.exe,9999,1,123456\r\n"  # self pid -> filtered out
    )


def test_wmic_reordered_columns_parsed_by_name():
    mgr = Win32ProcessManager()
    with mock.patch.object(mgr, "_current_session_id", return_value=1), \
         mock.patch("os.getpid", return_value=9999), \
         mock.patch("subprocess.run", return_value=mock.Mock(
             stdout=_wmic_csv_reordered().encode("utf-16"), stderr=b"")):
        procs = mgr.enumerate()

    names = {p.name: p.pid for p in procs}
    # explorer + game survive session/self filters; System(session 0) + self dropped.
    assert names.get("explorer.exe") == 1234
    assert names.get("game.exe") == 5678
    assert "System" not in names
    assert 9999 not in {p.pid for p in procs}
    # CommandLine survived the parse (game.exe had a quoted, comma-bearing cmd).
    game = next(p for p in procs if p.name == "game.exe")
    assert "game.exe -fullscreen" in game.cmdline
    # WorkingSetSize -> MB
    assert abs(game.memory_mb - 2000.0) < 1.0


def test_cim_fallback_when_wmic_empty():
    mgr = Win32ProcessManager()
    cim_csv = (
        "ProcessId,Name,CommandLine,WorkingSetSize,SessionId\r\n"
        "2222,notepad.exe,,262144000,1\r\n"
    )
    with mock.patch.object(mgr, "_current_session_id", return_value=1), \
         mock.patch("os.getpid", return_value=1), \
         mock.patch("subprocess.run", side_effect=[
             mock.Mock(stdout=b"", stderr=b""),            # wmic -> empty
             mock.Mock(stdout=cim_csv.encode("utf-8"), stderr=b""),  # CIM -> rows
         ]):
        procs = mgr.enumerate()
    assert any(p.name == "notepad.exe" and p.pid == 2222 for p in procs)
