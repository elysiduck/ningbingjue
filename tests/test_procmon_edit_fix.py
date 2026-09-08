"""Regression tests for the procmon edit-panel fixes (no full-app boot).

These tests deliberately avoid ``Application.boot()`` (which currently hangs in
headless/CI sandboxes due to the heavy full-app boot). They exercise the exact
code paths that were changed for the two reported bugs plus the related
#P2-10 placeholder fix:

* Bug 1 — 新建监控进程时进程属性啥都不要有: ``ProcMonEditPanel._add`` must NOT
  inherit the selected item's name/path, and the new entry must be stored as a
  genuinely blank ``Name=""`` (data model stays honest; display layer shows the
  placeholder).
* Bug 2 — 保存新建的监控进程时程序闪退: ``ProcMonEditPanel._save`` must never
  abort the process; any exception in the save path is surfaced as a failure
  toast. ``ProcMonBlock.show_toast`` is guarded against deleted Qt widgets via
  ``sip.isdeleted``.
* #P2-10 — an empty-name button must not show a bare icon ``○  ``; both
  ``_button_text`` and the editor list show a placeholder instead.
"""
from __future__ import annotations

import types

import pytest

from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication, QLineEdit, QListWidget, QWidget
from PyQt6 import sip

from ningbingjue.blocks.procmon import ProcMonBlock, _widget_alive
from ningbingjue.platform.process_manager import ProcessManager
from ningbingjue.ui.edit_overlay import ProcMonEditPanel


# --------------------------------------------------------------------------- #
# fixtures / fakes
# --------------------------------------------------------------------------- #
@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


class _StubPM(ProcessManager):
    """Minimal ProcessManager that never touches the OS."""
    def __init__(self) -> None:
        # Skip the heavy base __init__; only what refresh_panel needs.
        pass
    @property
    def available(self) -> bool:
        return True
    def enumerate(self):  # noqa: D401 - stub
        return []
    def is_running(self, *a, **k):
        return False
    def is_suspended(self, *a, **k):
        return False
    def resume(self, *a, **k):
        return False
    def start(self, *a, **k):
        return False
    def stop(self, *a, **k):
        return False
    def stop_matching(self, *a, **k):
        return False
    def suspend(self, *a, **k):
        return False


class _Cfg:
    procmon_configs: list = []


class _FakeApp:
    """Stand-in for the real Application used by the edit panel."""
    def __init__(self) -> None:
        self.save_all_calls = 0
        self.save_error: Exception | None = None
    def save_all(self) -> None:
        self.save_all_calls += 1
        if self.save_error is not None:
            raise self.save_error


class _FakeBlock:
    """Edit-facing API of ProcMonBlock (no side effects, no Qt buttons)."""
    def __init__(self, configs=None) -> None:
        self.configs = [dict(c) for c in (configs or [])]
        self.save_config_calls = 0
        self.toast_text: str | None = None
        self.toast_ok: bool | None = None
        self.ui: dict = {}
    def add_config(self, name: str, path: str) -> None:
        self.configs.append({"Name": name, "Path": path, "Running": False})
    def save_config(self):
        self.save_config_calls += 1
        return [{"Name": c["Name"], "Path": c["Path"]} for c in self.configs]
    def show_toast(self, text: str, success: bool = True) -> None:
        self.toast_text = text
        self.toast_ok = success


def _bind_panel(host) -> None:
    """Bind the real ProcMonEditPanel edit methods onto a lightweight host.

    The four methods under test only touch ``block`` / ``lst`` / ``name`` /
    ``path`` / ``_app`` / ``row``, so they run unmodified against a host that
    provides exactly those attributes.
    """
    host._add = types.MethodType(ProcMonEditPanel._add, host)
    host._save = types.MethodType(ProcMonEditPanel._save, host)
    host._refresh = types.MethodType(ProcMonEditPanel._refresh, host)
    host._on_sel = types.MethodType(ProcMonEditPanel._on_sel, host)


def _make_host(block, app) -> object:
    host = type("Host", (), {})()
    host.block = block
    host._app = app
    host.lst = QListWidget()
    host.name = QLineEdit()
    host.path = QLineEdit()
    host.row = -1
    _bind_panel(host)
    return host


# --------------------------------------------------------------------------- #
# Bug 1 — new entry must be blank, and must not inherit the selected item
# --------------------------------------------------------------------------- #
def test_add_config_blank_stores_empty_name():
    block = ProcMonBlock(_Cfg(), _StubPM(), ui={})
    block.add_config("", "")
    assert block.configs[-1]["Name"] == ""
    assert block.configs[-1]["Path"] == ""


def test_edit_add_no_inherit_and_clears_fields(qapp):
    block = _FakeBlock([{"Name": "已有A", "Path": "C:/a.ahk"},
                        {"Name": "已有B", "Path": "C:/b.ahk"}])
    app = _FakeApp()
    host = _make_host(block, app)

    # Simulate the editor showing the currently selected (entry 0) item.
    host.lst.addItem("已有A")
    host.lst.addItem("已有B")
    host.lst.setCurrentRow(0)
    host.name.setText("已有A")
    host.path.setText("C:/a.ahk")

    before = [c["Name"] for c in block.configs]
    host._add()

    after = [c["Name"] for c in block.configs]
    # Bug 1a: the new entry must NOT be a copy of the selected item.
    assert after.count("已有A") == before.count("已有A"), f"inherit! {before} -> {after}"
    # Bug 1b: the new entry is genuinely blank (Name stored as "").
    assert block.configs[-1]["Name"] == ""
    assert block.configs[-1]["Path"] == ""
    # Bug 1c: after _add the input fields are cleared (进程属性啥都不要有).
    assert host.name.text() == "" and host.path.text() == ""


def test_refresh_list_shows_placeholder_for_blank(qapp):
    block = _FakeBlock([{"Name": "", "Path": ""}])
    app = _FakeApp()
    host = _make_host(block, app)
    host._refresh()
    # The blank entry must render as a placeholder, not a naked empty row.
    assert host.lst.item(0).text() == "(新监控项)"


# --------------------------------------------------------------------------- #
# Bug 2 — saving must never crash the process
# --------------------------------------------------------------------------- #
def test_edit_save_persists_blank_without_crash(qapp):
    block = _FakeBlock([{"Name": "已有A", "Path": "C:/a.ahk"}])
    app = _FakeApp()
    host = _make_host(block, app)

    host.lst.addItem("已有A")
    host._add()                       # appends a blank entry, selects it
    host.lst.setCurrentRow(len(block.configs) - 1)
    host._save()                      # save the blank new entry

    assert app.save_all_calls >= 1
    assert block.save_config_calls >= 1
    assert block.toast_text == "已保存监控项"
    assert block.toast_ok is True
    # The blank entry persisted as empty in the (fake) config.
    assert block.configs[-1]["Name"] == ""


def test_edit_save_exception_surfaced_as_toast(qapp):
    block = _FakeBlock([{"Name": "已有A", "Path": "C:/a.ahk"}])
    app = _FakeApp()
    app.save_error = RuntimeError("disk full")   # simulate a failing save_all
    host = _make_host(block, app)

    host.lst.addItem("已有A")
    host._add()
    host.lst.setCurrentRow(len(block.configs) - 1)

    # Must not raise / abort — the error is turned into a failure toast.
    host._save()
    assert block.toast_text is not None
    assert block.toast_text.startswith("保存失败")
    assert block.toast_ok is False


# --------------------------------------------------------------------------- #
# #P2-10 — empty-name button placeholder (no bare icon)
# --------------------------------------------------------------------------- #
def test_button_text_empty_name_placeholder():
    assert ProcMonBlock._button_text(None, {"Name": "", "Running": False}) == "○ 监控项"
    assert ProcMonBlock._button_text(None, {"Name": "微信", "Running": True}) == "● 微信"


# --------------------------------------------------------------------------- #
# Crash-proofing helper for show_toast (deleted-widget guard)
# --------------------------------------------------------------------------- #
def test_widget_alive_helper(qapp):
    assert _widget_alive(None) is False
    live = QWidget()
    assert _widget_alive(live) is True
    # Force the C++ side to be freed immediately (mirrors what rebuild()'s
    # deleteLater has already done by the time the user clicks save).
    sip.delete(live)
    # A destroyed QWidget reports as deleted — this is the guard that prevents
    # the save path from touching a freed C++ object and aborting the process.
    assert _widget_alive(live) is False


def test_show_toast_without_toast_widgets_does_not_crash():
    # A block whose ui has no toast_panel/toast_label must not abort.
    host = type("H", (), {"ui": {}})()
    # Should complete silently without touching any Qt widget.
    ProcMonBlock.show_toast(host, "hi", True)
    ProcMonBlock.show_toast(host, "bad", False)
