"""Regression tests for the "重启" (restart) button and the dev-sync relaunch.

History
-------
* The original ``LayoutEditPanel._restart`` tried ``os.execl`` *first*.  On the
  shipped frozen build (windowed, ``uac_admin=True``) an in-place exec can fail
  to actually relaunch, and even on the happy path the relaunched copy was not
  guaranteed to carry ``--gui`` — so it could fall into the headless engine
  self-test and exit immediately, presenting as "closed but never restarted".
  The fix spawns a fresh, fully-detached GUI instance (forcing ``--gui``) and
  *then* quits the current process, so the user always ends up with a running
  HUD.
* 2026-08-30: switched to ``subprocess.Popen`` + sync wait.  That rewrite
  silently dropped three contracts this file used to lock: forcing ``--gui``,
  ``CREATE_NO_WINDOW``, and the ``os.execl`` fallback — the tests went red and
  stayed red (they were still red right before the 2026-09-01 fix).
* 2026-09-01 (this update): restart verification is now **asynchronous**
  (``_RestartWatcher`` polls every 250 ms for up to 8 s) so the Qt main thread
  is never slept on.  The tests therefore stub ``_start_async_restart`` with a
  synchronous trigger: the behaviour contracts above are still asserted, and
  the new watcher gets its own dedicated tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import types

import pytest

from ningbingjue.ui import edit_overlay as eo
from ningbingjue.ui.edit_overlay import LayoutEditPanel


# --- fakes ---------------------------------------------------------------
class _FakeApp:
    def __init__(self) -> None:
        self.saved = False

    def save_all(self) -> None:
        self.saved = True


class _Sig:
    def __init__(self, owner: "_FakeOverlay") -> None:
        self._owner = owner

    def emit(self) -> None:
        self._owner.closed = True


class _FakeOverlay:
    def __init__(self) -> None:
        self.closed = False
        self.close_requested = _Sig(self)


def _make_host() -> types.SimpleNamespace:
    """A stand-in ``self`` for ``LayoutEditPanel._restart``.

    ``_restart`` hands ``self._on_restart_ok`` / ``self._on_restart_failed`` to
    ``_start_async_restart`` as callbacks — on a real instance those are bound
    methods, so a SimpleNamespace has to carry them explicitly.  ``_restart_btn``
    is set to ``None`` on purpose: ``_restart`` wraps the button update in
    try/except so the editor keeps working without a real button, and the tests
    assert that path too.
    """
    host = types.SimpleNamespace(
        _app=_FakeApp(),
        _overlay=_FakeOverlay(),
        _restart_btn=None,
    )
    host._on_restart_ok = lambda: eo._quit_after_restart(host._app)
    host._on_restart_failed = lambda msg: None
    return host


class _PopenSpy:
    """Stand-in for ``subprocess.Popen`` — records cmd + creationflags.

    Accepts ``**kwargs``: the real call passes env/cwd/stdin/stdout/stderr too,
    and an over-narrow signature makes every test fail with a TypeError instead
    of exercising the behaviour under test (that is exactly how this file went
    stale in 2026-08-30).
    """

    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple[list[str], int]] = []
        self.kwargs: list[dict] = []
        self.raise_exc = raise_exc

    def __call__(self, cmd: list[str], **kwargs) -> object:
        self.calls.append((list(cmd), int(kwargs.get("creationflags", 0))))
        self.kwargs.append(dict(kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeProc()  # a child that stays alive


class _ExeclSpy:
    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.raise_exc = raise_exc

    def __call__(self, *args: str, **kwargs: object) -> object:
        self.calls.append(args)
        if self.raise_exc is not None:
            raise self.raise_exc
        return object()


class _MockQApp:
    def __init__(self) -> None:
        self.quit_called = False

    def quit(self) -> None:
        self.quit_called = True


def _sync_watcher_start(self) -> None:
    """Collapse ``_RestartWatcher``'s QTimer polling into one immediate verdict.

    Only the *waiting* is stubbed — ``_spawn_restart_child_proc`` still runs for
    real, so the assertions below cover the actual command line, creation flags
    and the ``os.execl`` fallback.  Stubbing ``_start_async_restart`` instead
    would short-circuit everything the tests are supposed to check.
    """
    rc = self._proc.poll()
    if rc is not None:
        self._finish(False, self._explain(rc))
    else:
        self._finish(True, "")


# --- helpers -------------------------------------------------------------
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def _run(monkeypatch, frozen: bool, argv: list[str], popen: _PopenSpy,
         qapp: _MockQApp, execl: _ExeclSpy) -> None:
    _ensure_qapp()  # QTimer/QObject need a live QApplication
    saved_frozen = getattr(sys, "frozen", None)
    saved_exe = sys.executable
    saved_argv = sys.argv
    sf = sys.executable if not frozen else "C:/fake/NingBingJue.exe"
    try:
        if frozen:
            sys.frozen = True
        elif "frozen" in sys.__dict__:
            del sys.frozen
        sys.executable = sf
        sys.argv = argv
        monkeypatch.setattr(subprocess, "Popen", popen)
        monkeypatch.setattr(os, "execl", execl)
        monkeypatch.setattr(eo._RestartWatcher, "start", _sync_watcher_start)
        import PyQt6.QtWidgets as QW

        real_instance = QW.QApplication.instance
        monkeypatch.setattr(QW.QApplication, "instance", lambda: qapp)
        try:
            LayoutEditPanel._restart(_make_host())
        finally:
            QW.QApplication.instance = real_instance
    finally:
        if saved_frozen is None and "frozen" in sys.__dict__:
            del sys.frozen
        else:
            sys.frozen = saved_frozen
        sys.executable = saved_exe
        sys.argv = saved_argv


# --- behaviour contracts -------------------------------------------------
def test_restart_frozen_gui_present(monkeypatch):
    popen, qapp, execl = _PopenSpy(), _MockQApp(), _ExeclSpy()
    _run(monkeypatch, True, ["NingBingJue.exe", "--gui"], popen, qapp, execl)
    assert popen.calls, "must spawn a new instance"
    cmd, flags = popen.calls[0]
    assert cmd[0] == "C:/fake/NingBingJue.exe"
    assert "--gui" in cmd
    assert flags & CREATE_NEW_PROCESS_GROUP
    assert flags & CREATE_NO_WINDOW
    assert qapp.quit_called
    assert not execl.calls  # happy path must NOT use os.execl


def test_restart_frozen_gui_inserted_when_missing(monkeypatch):
    popen, qapp, execl = _PopenSpy(), _MockQApp(), _ExeclSpy()
    _run(monkeypatch, True, ["NingBingJue.exe"], popen, qapp, execl)
    cmd, _ = popen.calls[0]
    assert cmd == ["C:/fake/NingBingJue.exe", "--gui"], cmd
    assert qapp.quit_called


def test_restart_nonfrozen_forces_gui(monkeypatch):
    popen, qapp, execl = _PopenSpy(), _MockQApp(), _ExeclSpy()
    _run(monkeypatch, False, ["run.py", "--root", "D:/x"], popen, qapp, execl)
    cmd, _ = popen.calls[0]
    assert cmd[0].endswith("python.exe")
    assert cmd[1].endswith("run.py")
    assert "--root" in cmd and "D:/x" in cmd
    assert "--gui" in cmd
    assert qapp.quit_called


def test_restart_headless_argv_is_not_forwarded(monkeypatch):
    """``--headless`` must never survive into the restart command line.

    Without this, a HUD started with ``--headless`` would relaunch into the
    headless engine self-test, exit cleanly a few seconds later, and the user
    would see "restarted, then the program vanished".
    """
    popen, qapp, execl = _PopenSpy(), _MockQApp(), _ExeclSpy()
    _run(monkeypatch, True, ["NingBingJue.exe", "--headless"],
         popen, qapp, execl)
    cmd, _ = popen.calls[0]
    assert "--headless" not in cmd, cmd
    assert cmd == ["C:/fake/NingBingJue.exe", "--gui"], cmd


def test_restart_popen_failure_falls_back_to_execl(monkeypatch):
    popen = _PopenSpy(raise_exc=RuntimeError("CreateProcess failed"))
    qapp, execl = _MockQApp(), _ExeclSpy()
    _run(monkeypatch, True, ["NingBingJue.exe", "--gui"], popen, qapp, execl)
    assert popen.calls  # attempted spawn
    assert execl.calls  # fell back to os.execl
    assert qapp.quit_called  # still tears down the old instance


def test_restart_persists_and_hides_editor(monkeypatch):
    _ensure_qapp()
    popen, qapp, execl = _PopenSpy(), _MockQApp(), _ExeclSpy()
    host = _make_host()
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "execl", execl)
    monkeypatch.setattr(eo._RestartWatcher, "start", _sync_watcher_start)
    import PyQt6.QtWidgets as QW

    real_instance = QW.QApplication.instance
    monkeypatch.setattr(QW.QApplication, "instance", lambda: qapp)
    saved_argv = sys.argv
    sys.argv = ["NingBingJue.exe", "--gui"]
    try:
        LayoutEditPanel._restart(host)
    finally:
        sys.argv = saved_argv
        QW.QApplication.instance = real_instance
    assert host._app.saved is True
    assert host._overlay.closed is True


def test_restart_is_idempotent_while_pending(monkeypatch):
    """A second click while a restart is in flight must be a no-op."""
    _ensure_qapp()
    popen = _PopenSpy()
    host = _make_host()
    host._restart_watcher = object()  # pretend a restart is already pending
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(eo._RestartWatcher, "start", _sync_watcher_start)
    LayoutEditPanel._restart(host)
    assert not popen.calls, "must not spawn a second instance"


# --- _RestartWatcher -----------------------------------------------------
_QAPP = None


def _ensure_qapp():
    """A real QApplication is required for QTimer; keep it alive module-wide."""
    global _QAPP
    from PyQt6.QtWidgets import QApplication

    if _QAPP is None:
        _QAPP = QApplication.instance() or QApplication([])
    return _QAPP


class _FakeProc:
    """Minimal stand-in for ``subprocess.Popen`` — ``poll()`` only."""

    def __init__(self, die_after: float | None = None) -> None:
        self._t0 = time.monotonic()
        self._die_after = die_after
        self.returncode = 0

    def poll(self):
        if self._die_after is not None and \
                time.monotonic() - self._t0 >= self._die_after:
            return 0
        return None


def _drive(watcher, timeout: float = 6.0) -> None:
    """Pump the event loop until the watcher emits (no QEventLoop needed)."""
    app = _ensure_qapp()
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        app.processEvents()
        time.sleep(0.02)


def test_watcher_succeeds_when_child_survives(monkeypatch):
    _ensure_qapp()
    monkeypatch.setattr(eo._RestartWatcher, "GRACE_S", 0.6)
    box: dict = {}
    w = eo._RestartWatcher(_FakeProc())
    w.succeeded.connect(lambda: box.setdefault("r", "ok"))
    w.failed.connect(lambda m: box.setdefault("r", "fail"))
    w.start()
    _drive(w)
    assert box.get("r") == "ok", "a surviving child must be accepted"


def test_watcher_fails_when_child_dies(monkeypatch):
    _ensure_qapp()
    monkeypatch.setattr(eo._RestartWatcher, "GRACE_S", 5.0)
    box: dict = {}
    w = eo._RestartWatcher(_FakeProc(die_after=0.2))
    w.succeeded.connect(lambda: box.setdefault("r", "ok"))
    w.failed.connect(lambda m: box.setdefault("r", "fail:" + str(m)[:40]))
    w.start()
    _drive(w)
    assert str(box.get("r", "")).startswith("fail"), \
        "a child that exits early must NOT trigger the old process to quit"
    # rc=0 gets the UAC-elevation hint — that is the most common false exit.
    assert "rc=0" in str(box.get("r", "")) or "rc=" in str(box.get("r", ""))


def test_watcher_does_not_block_the_main_thread(monkeypatch):
    """The whole point of the async rewrite: the loop keeps spinning."""
    app = _ensure_qapp()
    monkeypatch.setattr(eo._RestartWatcher, "GRACE_S", 0.6)
    beats = {"n": 0}

    def _beat() -> None:
        beats["n"] += 1

    from PyQt6.QtCore import QTimer

    hb = QTimer()
    hb.setInterval(30)
    hb.timeout.connect(_beat)
    hb.start()
    box: dict = {}
    w = eo._RestartWatcher(_FakeProc())
    w.succeeded.connect(lambda: box.setdefault("r", "ok"))
    w.start()
    _drive(w)
    hb.stop()
    assert box.get("r") == "ok"
    assert beats["n"] > 5, \
        "the main thread must keep processing events while waiting"
