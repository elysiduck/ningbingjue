"""Regression test for the "重启" (restart) button.

History
-------
The original ``LayoutEditPanel._restart`` tried ``os.execl`` *first*.  On the
shipped frozen build (windowed, ``uac_admin=True``) an in-place exec can fail
to actually relaunch, and even on the happy path the relaunched copy was not
guaranteed to carry ``--gui`` — so it could fall into the headless engine
self-test and exit immediately, presenting as "closed but never restarted".

The fix spawns a fresh, fully-detached GUI instance (forcing ``--gui``) and
*then* quits the current process, so the user always ends up with a running
HUD.  This test locks that contract in.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest

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
    return types.SimpleNamespace(
        _app=_FakeApp(),
        _overlay=_FakeOverlay(),
    )


class _PopenSpy:
    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple[list[str], int]] = []
        self.raise_exc = raise_exc

    def __call__(self, cmd: list[str], creationflags: int = 0) -> object:
        self.calls.append((list(cmd), creationflags))
        if self.raise_exc is not None:
            raise self.raise_exc
        return object()


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


# --- helpers -------------------------------------------------------------
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def _run(monkeypatch, frozen: bool, argv: list[str], popen: _PopenSpy,
         qapp: _MockQApp, execl: _ExeclSpy) -> None:
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


# --- tests ---------------------------------------------------------------
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


def test_restart_popen_failure_falls_back_to_execl(monkeypatch):
    popen = _PopenSpy(raise_exc=RuntimeError("CreateProcess failed"))
    qapp, execl = _MockQApp(), _ExeclSpy()
    _run(monkeypatch, True, ["NingBingJue.exe", "--gui"], popen, qapp, execl)
    assert popen.calls  # attempted spawn
    assert execl.calls  # fell back to os.execl
    assert qapp.quit_called  # still tears down the old instance


def test_restart_persists_and_hides_editor(monkeypatch):
    popen, qapp, execl = _PopenSpy(), _MockQApp(), _ExeclSpy()
    host = _make_host()
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "execl", execl)
    import PyQt6.QtWidgets as QW

    real_instance = QW.QApplication.instance
    monkeypatch.setattr(QW.QApplication, "instance", lambda: qapp)
    # Preserve sys state minimally for the single-instance call.
    saved_argv = sys.argv
    sys.argv = ["NingBingJue.exe", "--gui"]
    try:
        LayoutEditPanel._restart(host)
    finally:
        sys.argv = saved_argv
        QW.QApplication.instance = real_instance
    assert host._app.saved is True
    assert host._overlay.closed is True
