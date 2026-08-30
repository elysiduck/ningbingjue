"""In-window edit overlay + gamepad control regression tests.

These lock the three requests in the latest round:

* Editing UI lives **inside the main window** (a child overlay, never a dialog).
* The tile editor's 2-D grid uses the original's AABB overlap-swap on arrange.
* A gamepad (MockBackend) drives the same navigation + A/B/X/Y activation as
  the keyboard via ``InteractionController``.
"""

from __future__ import annotations

import json

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication, QDialog, QWidget

from ningbingjue.app import Application
from ningbingjue.core.config import Tile
from ningbingjue.ui.gamepad import GamepadState, MockBackend
from ningbingjue.ui.gamepad_hint import HintButton

ROOT = (
    r"D:\下载\localsend\凝冰结魄-覆盖任务栏-修复-完全解耦-备份-20260717172824"
    r"\凝冰结魄-覆盖任务栏-修复-完全解耦-备份"
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _seed(tmp_path, tiles):
    cfg = tmp_path / "layout.json"
    from ningbingjue.core.config import AppConfig
    ac = AppConfig()
    ac.tiles = tiles
    cfg.write_text(json.dumps(ac.to_dict(), ensure_ascii=False), encoding="utf-8")
    return cfg


# --------------------------------------------------------------------------
# In-window overlay (no separate dialog)
# --------------------------------------------------------------------------
def _is_inside(widget, ancestor):
    """True if ``ancestor`` is anywhere in ``widget``'s parent chain."""
    p = widget.parentWidget()
    while p is not None:
        if p is ancestor:
            return True
        p = p.parentWidget()
    return False


def test_edit_overlay_is_in_window(tmp_path, qapp):
    cfg = _seed(tmp_path, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()

    overlay = app._window._edit_overlay
    # It is a plain widget — NOT a dialog — and lives *inside* the main window
    # (it is a child of the body QStackedWidget, which is a child of the window).
    assert isinstance(overlay, QWidget)
    assert not isinstance(overlay, QDialog)
    assert _is_inside(overlay, app._window)

    app._window._open_editor()
    assert app._window._body.currentWidget() is overlay
    assert app._window._controller._edit_mode is True
    # nav_items now come from the overlay, not the hidden HUD panels.
    assert overlay.nav_items()
    app._window.close()
    qapp.processEvents()


def test_edit_overlay_toggle_closes(tmp_path, qapp):
    cfg = _seed(tmp_path, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    app._window._open_editor()
    assert app._window._controller._edit_mode is True
    app._window._open_editor()  # second press closes
    assert app._window._controller._edit_mode is False
    assert app._window._body.currentWidget() is app._window._scroll
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Tile grid AABB overlap swap (faithful to original MouseUp handler)
# --------------------------------------------------------------------------
def test_tile_grid_overlap_swap(tmp_path, qapp):
    cfg = _seed(tmp_path, [
        Tile(name="A", launch_path="C:/a", launch_type="FILE",
             col=0, row=0, width=1, height=1),
        Tile(name="B", launch_path="C:/b", launch_type="FILE",
             col=1, row=0, width=1, height=1),
    ])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    app._window._open_editor()
    app._window._edit_overlay.open_tile()
    grid = app._window._edit_overlay._tile_panel._grid

    # Move A (idx 0) onto B's cell (1,0) -> they should swap positions.
    grid._move_selected_to(0, 0, 1)
    assert grid.work[0].col == 1 and grid.work[0].row == 0
    assert grid.work[1].col == 0 and grid.work[1].row == 0
    app._window.close()
    qapp.processEvents()


def test_tile_grid_clamps_to_bounds(tmp_path, qapp):
    cfg = _seed(tmp_path, [
        Tile(name="A", launch_path="C:/a", launch_type="FILE",
             col=3, row=0, width=2, height=1),
    ])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    app._window._open_editor()
    app._window._edit_overlay.open_tile()
    grid = app._window._edit_overlay._tile_panel._grid
    # A 2-wide tile at col=5 (overflow) → clamp to 5 - 2 = 3.
    grid._move_selected_to(0, 0, 5)
    assert grid.work[0].col == 3  # COLS=5, 5-2=3
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Gamepad control (MockBackend -> InteractionController)
# --------------------------------------------------------------------------
def test_gamepad_stick_navigates(tmp_path, qapp):
    cfg = _seed(tmp_path, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    ctrl = app._window._controller
    ctrl._gamepad = MockBackend()

    app._window._open_editor()
    app._window._edit_overlay.open_tile()
    panel = app._window._edit_overlay._tile_panel
    grid = panel._grid
    panel.sel = 0
    grid.sel = 0
    ctrl._focus = -1
    ctrl.refresh()
    for i, it in enumerate(ctrl._items):
        if it.widget is grid:
            ctrl.set_focus(i)
            break

    # Push the stick right -> the selected tile nudges one cell to the right.
    ctrl._gamepad.state = GamepadState(connected=True, lx=30000, ly=0)
    ctrl._poll_gamepad()
    assert grid.work[0].col == 1
    app._window.close()
    qapp.processEvents()


def test_gamepad_face_buttons_emit(tmp_path, qapp):
    cfg = _seed(tmp_path, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    ctrl = app._window._controller
    ctrl._gamepad = MockBackend()

    saves = []
    cancels = []
    deletes = []
    ctrl.request_save.connect(lambda: saves.append(1))
    ctrl.request_cancel.connect(lambda: cancels.append(1))
    ctrl.request_delete.connect(lambda: deletes.append(1))

    # Rising edge of Y / B / X should each fire exactly once.
    ctrl._gamepad.state = GamepadState(connected=True, y=True, b=True, x=True)
    ctrl._poll_gamepad()
    # Holding (no state change) must NOT re-fire.
    ctrl._poll_gamepad()
    assert len(saves) == 1 and len(cancels) == 1 and len(deletes) == 1
    app._window.close()
    qapp.processEvents()


def test_gamepad_moves_selected_tile(tmp_path, qapp):
    cfg = _seed(tmp_path, [
        Tile(name="A", launch_path="C:/a", launch_type="FILE",
             col=0, row=0, width=1, height=1),
    ])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    app._window._open_editor()
    app._window._edit_overlay.open_tile()
    panel = app._window._edit_overlay._tile_panel
    grid = panel._grid
    panel.sel = 0
    grid.sel = 0

    ctrl = app._window._controller
    ctrl._gamepad = MockBackend()
    # Focus the grid then push the stick down -> the selected tile nudges down.
    ctrl._focus = -1
    ctrl.refresh()
    # find the grid NavItem and focus it
    for i, it in enumerate(ctrl._items):
        if it.widget is grid:
            ctrl.set_focus(i)
            break
    ctrl._gamepad.state = GamepadState(connected=True, lx=0, ly=30000)
    ctrl._poll_gamepad()
    assert grid.work[0].row == 1
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Tile overlap resolution (no tiles should render on top of each other)
# --------------------------------------------------------------------------
def test_tile_grid_resolve_all_removes_overlap(tmp_path, qapp):
    """Two tiles sharing the same cell are auto-resolved on load."""
    cfg = _seed(tmp_path, [
        Tile(name="A", launch_path="C:/a", launch_type="FILE",
             col=0, row=0, width=1, height=1),
        Tile(name="B", launch_path="C:/b", launch_type="FILE",
             col=0, row=0, width=1, height=1),
    ])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    app._window._open_editor()
    app._window._edit_overlay.open_tile()
    grid = app._window._edit_overlay._tile_panel._grid
    # After _resolve_all, the two tiles occupy different cells.
    for i in range(len(grid.work)):
        assert grid._first_overlap(i) == -1
    app._window.close()
    qapp.processEvents()


def test_tile_grid_free_slot_respects_footprint(tmp_path, qapp):
    """A 2-wide tile cannot be placed where it would overlap a smaller tile."""
    cfg = _seed(tmp_path, [
        Tile(name="A", launch_path="C:/a", launch_type="FILE",
             col=0, row=0, width=2, height=1),
        Tile(name="B", launch_path="C:/b", launch_type="FILE",
             col=2, row=0, width=1, height=1),
    ])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    app._window._open_editor()
    app._window._edit_overlay.open_tile()
    grid = app._window._edit_overlay._tile_panel._grid
    # A 2-wide tile at (0,0): a 1-wide tile can still go to (2,0) or (3,0) etc.
    # But cannot go to (1,0) because A occupies (0,0) and (1,0).
    slot = grid._free_slot(1, 1)
    assert slot is not None
    sc, sr = slot
    # Must not overlap the 2-wide A at (0,0)→(1,0).
    assert not grid._aabb_overlap(sc, sr, 1, 1, 0, 0, 2, 1)
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Drag-crash fix (float coords no longer raise TypeError)
# --------------------------------------------------------------------------
def test_tile_grid_mouse_move_float_coords(tmp_path, qapp):
    """Simulate a float mouse-drag — must not crash (was TypeError before fix)."""
    cfg = _seed(tmp_path, [
        Tile(name="A", launch_path="C:/a", launch_type="FILE",
             col=0, row=0, width=1, height=1),
    ])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    app._window._open_editor()
    app._window._edit_overlay.open_tile()
    grid = app._window._edit_overlay._tile_panel._grid
    # Simulate a mouse press at pixel coords inside the first tile to start drag.
    pt = QPointF(30.0, 30.0)
    grid.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, pt, pt,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    # Now move: floating-point coordinates should NOT raise TypeError.
    for x in (30.0, 100.7, 200.3):
        mv = QPointF(x, 50.0)
        grid.mouseMoveEvent(QMouseEvent(
            QEvent.Type.MouseMove, mv, mv,
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier))
    rp = QPointF(200.3, 50.0)
    grid.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, rp, rp,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Gamepad ABXY badge is shown persistently on HintButton controls
# --------------------------------------------------------------------------
def test_gamepad_hint_badge_on_buttons(tmp_path, qapp):
    """HintButton controls show badges only after gamepad has been used."""
    cfg = _seed(tmp_path, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    app._window._open_editor()
    app._window._edit_overlay.open_tile()
    ctrl = app._window._controller

    # Before gamepad use: no hints shown.
    ctrl.refresh()
    for it in ctrl._items:
        w = it.widget
        if isinstance(w, HintButton) and it.hint:
            assert w.show_gamepad_hint is False, f"{w.text()} hint shown before gp active"

    # After gamepad activation: hints appear.
    ctrl._gp_active = True
    ctrl.refresh()
    for it in ctrl._items:
        w = it.widget
        if isinstance(w, HintButton) and it.hint:
            assert w.show_gamepad_hint is True, f"{w.text()} hint missing"
            assert w.gamepad_hint == it.hint, f"{w.text()} wrong hint {w.gamepad_hint}"
    app._window.close()
    qapp.processEvents()
