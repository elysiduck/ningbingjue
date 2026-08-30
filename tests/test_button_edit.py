"""Button style editor (ButtonEditPanel) regression tests.

Locks the latest round's requests:

* The button editor is an **inline sub-page** of the edit overlay (a child
  widget, never a dialog) — opened via ``EditOverlay.open_button``.
* ``collect_button_groups`` walks the live HUD only, excluding the edit overlay
  subtree, and groups buttons by ``block_type`` with stable keys.
* The two-layer preset system works end-to-end: L2 commit + apply restyles
  every HUD button; L1 preset bind overlays a single button.
* Persistence round-trips through ``save_all_button_data`` / ``load_all_button_data``.
"""

from __future__ import annotations

import json
import re

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ningbingjue.app import Application
from ningbingjue.core.color import styles as _BS
from ningbingjue.core.color.styles import (
    BUTTON_PRESET_MAP,
    BUTTON_STYLES,
    PER_BUTTON_PRESETS,
    ButtonStyle,
)
from ningbingjue.core.config import Tile
from ningbingjue.ui.button_edit import (
    ButtonEditPanel,
    _PreviewItem,
    collect_button_groups,
)
from ningbingjue.ui.rounded_button import RoundedButton
from ningbingjue.ui.tile_button import TileButton

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


def _reset_button_styles():
    """Reset the styles module to defaults (mutable contents, not reassignment,
    so imported dict bindings in button_edit stay valid)."""
    defaults = _BS._build_default_styles()
    BUTTON_STYLES.clear()
    BUTTON_STYLES.update(defaults)
    _BS.ACTIVE_STYLE_NAME = "暗黑科技"
    PER_BUTTON_PRESETS.clear()
    BUTTON_PRESET_MAP.clear()


@pytest.fixture(autouse=True)
def _isolate_button_styles():
    _reset_button_styles()
    yield
    _reset_button_styles()


def _make_app(tmp_path, qapp, tiles=None):
    cfg = _seed(tmp_path, tiles or [])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    return app


# --------------------------------------------------------------------------
# Inline sub-page (not a dialog)
# --------------------------------------------------------------------------
def test_button_editor_is_inline_subpage(tmp_path, qapp):
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    overlay = app._window._edit_overlay
    panel = overlay._button_panel

    assert isinstance(panel, ButtonEditPanel)
    assert isinstance(panel, QWidget)
    assert not isinstance(panel, QDialog)
    # It lives inside the overlay stack (a child of the main window).
    assert panel.parent() is overlay._stack or overlay._stack.indexOf(panel) >= 0

    overlay.open_button()
    assert overlay.mode() == "button"
    assert overlay._stack.currentWidget() is panel
    assert overlay.nav_items(), "button mode must expose nav items"
    app._window.close()
    qapp.processEvents()


def test_open_button_refresh_does_not_crash(tmp_path, qapp):
    """refresh() builds the canvas preview list from live HUD buttons."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()  # must not raise
    # The canvas should have at least one preview item (HUD has buttons).
    assert panel._canvas_lay.count() > 0
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# collect_button_groups — live HUD only, grouped by block_type
# --------------------------------------------------------------------------
def test_collect_groups_excludes_edit_overlay(tmp_path, qapp):
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    win = app._window
    overlay = win._edit_overlay
    groups = collect_button_groups(win, exclude=overlay)

    # Every collected button must NOT be inside the edit overlay.
    for _bt, items in groups.items():
        for _key, btn in items:
            p = btn.parentWidget()
            while p is not None:
                assert p is not overlay, "editor control leaked into HUD group"
                p = p.parentWidget()

    # The editor's own action buttons must not appear anywhere in the groups.
    all_btns = [b for items in groups.values() for _k, b in items]
    for editor_btn in overlay._button_panel.findChildren(QPushButton):
        assert editor_btn not in all_btns
    app._window.close()
    qapp.processEvents()


def test_collect_groups_keys_are_stable_across_rebuild(tmp_path, qapp):
    """Keys are ``block_type#local_index`` — deterministic across rebuilds."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    win = app._window
    overlay = win._edit_overlay
    before = {k for items in collect_button_groups(win, exclude=overlay).values()
              for k, _ in items}
    win.rebuild()
    after = {k for items in collect_button_groups(win, exclude=overlay).values()
             for k, _ in items}
    assert before == after, "button binding keys must survive a layout rebuild"
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# L2 theme: commit + apply restyles HUD buttons via preset assignments
# --------------------------------------------------------------------------
def test_l2_commit_and_apply_restyles_buttons(tmp_path, qapp):
    """The new L2 model: create a L1 preset, bind it to a button, then
    ``_apply_and_refresh`` saves the assignment into the selected theme and
    pushes the preset to the live HUD button."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel

    # Create a L1 preset with a distinctive back colour.
    PER_BUTTON_PRESETS["测试红"] = ButtonStyle(
        cut_size=8, corner_radius=0, border_thickness=3,
        hover_only_border=False, hover_gradient=False, link_theme=False,
        colors={"BackColor": "ff0000", "BorderColor": "5fafeb", "ForeColor": "ff0000"})
    panel.refresh()
    qapp.processEvents()

    # Find a QPushButton in the preview and get its key.
    groups = collect_button_groups(app._window, exclude=app._window._edit_overlay)
    target = None
    target_key = None
    for _bt, items in groups.items():
        if _bt.startswith("tile") or _bt == "freeze":
            continue
        for k, b in items:
            if isinstance(b, QPushButton) and not isinstance(b, RoundedButton):
                target = b
                target_key = k
                break
        if target is not None:
            break
    assert target is not None, "HUD must expose at least one plain QPushButton"
    before_ss = target.styleSheet()

    # Select the L1 preset and bind it to the button.
    panel._sel_preset = "测试红"
    panel._sel_keys = {target_key}
    panel._bind()
    # Apply: save assignment to theme and push to live.
    panel._apply_and_refresh()
    qapp.processEvents()

    # The theme must now contain the assignment.
    assert _BS.ACTIVE_STYLE_NAME in BUTTON_STYLES
    assert BUTTON_STYLES[_BS.ACTIVE_STYLE_NAME].get(target_key) == "测试红"
    # The live button must have been restyled.  Native QPushButton reads its
    # preset through a stylesheet, so we assert on styleSheet() (the color:
    # rule does NOT propagate into palette()).
    after_ss = target.styleSheet()
    assert after_ss != before_ss, "live button stylesheet must change after apply"
    # "测试红" ForeColor is ff0000 -> rgb(255,0,0) in the generated stylesheet.
    # (A live HintButton renders only ForeColor via its stylesheet; BackColor is
    # drawn by RoundedButton/TileButton painters, not a stylesheet background.)
    assert "rgb(255,0,0)" in after_ss, "live button must pick up the bound preset's ForeColor"
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# L1 preset: create + bind + apply overlays a single button
# --------------------------------------------------------------------------
def test_l1_preset_bind_applies_to_one_button(tmp_path, qapp):
    app = _make_app(tmp_path, qapp, [
        Tile(name="A", launch_path="C:/a", launch_type="FILE"),
        Tile(name="B", launch_path="C:/b", launch_type="FILE"),
    ])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()

    groups = collect_button_groups(app._window, exclude=app._window._edit_overlay)
    # Two tiles → two TileButtons (RoundedButton subclass) in the tile block.
    rbs = []
    for _bt, items in groups.items():
        for k, b in items:
            if isinstance(b, RoundedButton):
                rbs.append((k, b))
    assert len(rbs) >= 2, "need >=2 RoundedButtons (seed 2 tiles)"
    (k0, b0), (_k1, b1) = rbs[0], rbs[1]

    # Create a preset with a unique back colour and bind it to b0.
    PER_BUTTON_PRESETS["t-red"] = ButtonStyle(
        cut_size=8, corner_radius=0, border_thickness=3,
        hover_only_border=False, hover_gradient=False, link_theme=False,
        colors={"BackColor": "00ff00", "BorderColor": "5fafeb", "ForeColor": "ffffff"})
    _BS.bind_button_preset(k0, "t-red")
    _BS.apply_per_button_preset(b0, "t-red")

    assert (b0._back.red(), b0._back.green(), b0._back.blue()) == (0, 255, 0)
    # The unbound sibling keeps the active style colour (not the preset).
    assert b1._back != b0._back
    # Binding map recorded.
    assert BUTTON_PRESET_MAP.get(k0) == "t-red"
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Persistence round-trip
# --------------------------------------------------------------------------
def test_persistence_round_trip(tmp_path, qapp):
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])

    # New model: a theme stores {button_key: preset_name} assignments.
    BUTTON_STYLES["暗黑科技"]["ls#0"] = "p1"
    PER_BUTTON_PRESETS["p1"] = ButtonStyle(
        cut_size=4, corner_radius=2, border_thickness=1,
        hover_only_border=True, hover_gradient=False, link_theme=False,
        colors={"BackColor": "aabbcc"})
    _BS.bind_button_preset("rtss#0", "p1")
    _BS.ACTIVE_STYLE_NAME = "暗黑科技"

    _BS.save_all_button_data(str(tmp_path))

    # Wipe in-memory state, then reload from disk.
    BUTTON_STYLES.clear()
    PER_BUTTON_PRESETS.clear()
    BUTTON_PRESET_MAP.clear()
    _BS.ACTIVE_STYLE_NAME = ""

    _BS.load_all_button_data(str(tmp_path))

    assert "暗黑科技" in BUTTON_STYLES
    assert BUTTON_STYLES["暗黑科技"].get("ls#0") == "p1"
    assert "p1" in PER_BUTTON_PRESETS
    assert PER_BUTTON_PRESETS["p1"].cut_size == 4
    assert BUTTON_PRESET_MAP.get("rtss#0") == "p1"
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Back button returns to the layout editor
# --------------------------------------------------------------------------
def test_back_button_returns_to_layout(tmp_path, qapp):
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    overlay = app._window._edit_overlay
    overlay.open_button()
    assert overlay.mode() == "button"
    overlay._button_panel._back_btn.click()
    assert overlay.mode() == "layout"
    assert overlay._stack.currentWidget() is overlay._layout_panel
    app._window.close()
    qapp.processEvents()


def test_gp_cancel_from_button_returns_to_layout(tmp_path, qapp):
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    app._window._open_editor()
    overlay = app._window._edit_overlay
    overlay.open_button()
    assert overlay.mode() == "button"
    app._window._gp_cancel()
    assert overlay.mode() == "layout"
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Top-to-bottom layout (the panel is narrow — no left/right split)
# --------------------------------------------------------------------------
def test_root_layout_is_top_to_bottom(tmp_path, qapp):
    """Root is a QVBoxLayout stacking the header *above* the controls.

    The live-preview button list was moved out of the panel root into a
    dedicated ``ButtonListPopup`` (docked beside the window), so the panel
    root only contains the header and the controls scroll area.
    """
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel

    lay = panel.layout()
    assert isinstance(lay, QVBoxLayout), "root layout must be vertical (top-to-bottom)"
    idx_ctrl = lay.indexOf(panel._controls_scroll)
    assert idx_ctrl >= 0, "controls must be a direct child of the panel root layout"
    # The preview canvas now lives inside the ButtonListPopup, not the panel
    # root, so it must NOT be a direct child here.
    idx_canvas = lay.indexOf(panel._canvas_scroll)
    assert idx_canvas == -1, \
        "preview canvas must live in ButtonListPopup, not the panel root"
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Type-faithful preview clones (text rendered inside the button)
# --------------------------------------------------------------------------
def _live_groups(app):
    panel = app._window._edit_overlay._button_panel
    # Exclude the editor overlay AND the floating button-list popup: the popup
    # hosts the preview clones plus its own 全选/清空 editor chrome (plain
    # QPushButtons) that must never be treated as live HUD sources.
    return collect_button_groups(
        app._window,
        exclude=app._window._edit_overlay,
        popup=panel._list_popup,
    )


def test_tile_block_buttons_excluded_from_preview(tmp_path, qapp):
    """After filtering, zero TileButton clones appear and no tile-block
    group header is rendered (tile buttons are hidden from preview)."""
    app = _make_app(tmp_path, qapp, [
        Tile(name="我的游戏", launch_path="C:/a", launch_type="FILE"),
        Tile(name="工具箱", launch_path="C:/b", launch_type="FILE"),
    ])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()
    qapp.processEvents()

    clones = panel._canvas.findChildren(TileButton)
    assert len(clones) == 0, "tile-block clones must be filtered out"
    headers = [c.text() for c in panel._canvas.findChildren(QLabel)
               if ">>" in c.text()]
    assert not any("页面布局编辑" in h for h in headers), \
        "tile block header must not appear in preview list"
    app._window.close()
    qapp.processEvents()


def test_pushbutton_preview_clone_matches_text_and_stylesheet(tmp_path, qapp):
    """QPushButton sources clone into QPushButtons with the exact same text
    (inside the button) and stylesheet — i.e. pixel-identical to the HUD."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()
    qapp.processEvents()

    live_pushes = []
    for _bt, items in _live_groups(app).items():
        for _k, b in items:
            if isinstance(b, QPushButton) and b.text():
                live_pushes.append(b)
    assert live_pushes, "HUD must expose native QPushButtons with text"

    clone_pairs = {(c.text(), c.styleSheet())
                   for c in panel._canvas.findChildren(QPushButton)}
    for b in live_pushes:
        assert (b.text(), b.styleSheet()) in clone_pairs, \
            f"preview must clone QPushButton {b.text()!r} with identical text+stylesheet"
    app._window.close()
    qapp.processEvents()


def test_preview_clones_are_interactive(tmp_path, qapp):
    """Clones must be fully interactive (hover + click), NOT mouse-
    transparent — so they render hover/pressed states like the HUD."""
    app = _make_app(tmp_path, qapp, [
        Tile(name="A", launch_path="C:/a", launch_type="FILE"),
        Tile(name="B", launch_path="C:/b", launch_type="FILE"),
    ])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()
    qapp.processEvents()

    clones = panel._canvas.findChildren(TileButton) + \
        panel._canvas.findChildren(QPushButton)
    assert clones, "canvas must contain preview clones"
    for c in clones:
        assert not c.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents), \
            "clone must receive mouse events for hover/pressed states"
    app._window.close()
    qapp.processEvents()


def test_clone_click_selects_key(tmp_path, qapp):
    """Clicking the clone (no outer wrapper frame) selects the binding key —
    selection is driven by the clone's ``clicked`` signal."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()
    qapp.processEvents()

    tiles = panel._canvas.findChildren(TileButton)
    pushes = panel._canvas.findChildren(QPushButton)
    assert tiles or pushes, "canvas must contain clickable clone buttons"

    # Use the first clone to exercise selection.
    clone = (tiles[0] if tiles else pushes[0])
    # Start from a clean selection (guard against cross-test global pollution
    # of BUTTON_SELECTION -> self._sel_keys).
    panel._sel_keys.clear()
    assert not panel._sel_keys, "no selection before click"
    if isinstance(clone, QPushButton):
        clone.clicked.emit(False)
    else:
        clone.clicked.emit()
    qapp.processEvents()
    # The clone's own key (set on the inner clone at _make_preview_item) must
    # now be in the multi-selection set (toggle via the clone's clicked signal).
    assert clone._src_key in panel._sel_keys, "click must select the clone's key"
    assert "#" in clone._src_key, "selected key uses the tile/block#name form"
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Preview-list filtering rules
# --------------------------------------------------------------------------
def test_freeze_block_shows_at_most_two_representatives(tmp_path, qapp):
    """Freeze block preview keeps at most one ❄ candidate and one 🔥
    frozen button."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()
    qapp.processEvents()

    freeze_clones = [c for c in panel._canvas.findChildren(QPushButton)
                     if c.text().startswith("❄") or c.text().startswith("🔥")]
    cand = sum(1 for c in freeze_clones if c.text().startswith("❄"))
    froz = sum(1 for c in freeze_clones if c.text().startswith("🔥"))
    assert cand <= 1 and froz <= 1, \
        f"freeze preview must show ≤1 candidate + ≤1 frozen, got {cand}+{froz}"
    assert cand + froz <= 2
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Requirement 1: preview clones render exactly like the live HUD buttons
# --------------------------------------------------------------------------
def test_clone_colors_and_geometry_match_source(tmp_path, qapp):
    """A RoundedButton clone (the custom-drawn button type) must carry the
    *exact* back / border colours and geometry (cut / corner / border) of its
    live HUD source — i.e. the preview button looks identical to the one on the
    main interface.  (Tiles are filtered out of the preview, so we exercise the
    clone *mechanism* directly on a live TileButton source, which is the same
    path ``render_canvas`` would use for any RoundedButton block.)"""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel

    target = None
    for _items in _live_groups(app).values():
        for _k, b in _items:
            if isinstance(b, RoundedButton):
                target = b
                break
        if target is not None:
            break
    assert target is not None, "need a live RoundedButton (TileButton) source"

    clone = panel._clone_button(target)
    assert isinstance(clone, RoundedButton)
    assert (clone._back.red(), clone._back.green(), clone._back.blue()) == \
           (target._back.red(), target._back.green(), target._back.blue()), \
        "clone back colour must match the live HUD button"
    assert (clone._border.red(), clone._border.green(), clone._border.blue()) == \
           (target._border.red(), target._border.green(), target._border.blue()), \
        "clone border colour must match the live HUD button"
    assert clone.cut_size == target.cut_size
    assert clone.corner_radius == target.corner_radius
    assert clone.border_thickness == target.border_thickness
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Requirement 2: no outer wrapper / container box around preview buttons
# --------------------------------------------------------------------------
def test_canvas_scroll_has_no_container_border(tmp_path, qapp):
    """The preview scroll area must carry NO container border (the previous
    ``2px solid rgba(95,175,235,0.25)`` box is removed)."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    ss = panel._canvas_scroll.styleSheet().lower()
    assert "rgba(95,175,235,0.25)" not in ss, "outer canvas container border must be gone"
    assert "border: none" in ss, "canvas scroll area must have no border"
    app._window.close()
    qapp.processEvents()


def test_preview_items_wrap_clone_with_focus_frame(tmp_path, qapp):
    """Each preview row is a :class:`_PreviewItem` that hosts a faithful clone
    plus a mouse-transparent focus-frame overlay.  When NOT selected the frame
    is hidden (no visible wrapper box), and the clone keeps its native HUD
    shape — selection never forces it into a rectangle."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()
    qapp.processEvents()

    # No leftover old container box (rgba(20,24,32,0.9)) anywhere under canvas.
    for f in panel._canvas.findChildren(QFrame):
        assert "rgba(20,24,32,0.9)" not in f.styleSheet().lower(), \
            "old wrapper container box must be gone"

    rows = [w for i in range(panel._canvas_lay.count())
            if isinstance((w := panel._canvas_lay.itemAt(i).widget()), _PreviewItem)]
    assert rows, "preview must contain _PreviewItem rows"
    for row in rows:
        # Unselected by default → focus frame hidden (no visible box).
        # isVisibleTo(row) checks the frame's own visibility flag regardless of
        # whether the (unshown) canvas/popup ancestors are visible yet.
        assert not row._frame.isVisibleTo(row), "unselected item must hide its focus frame"
        # The clone underneath keeps its native shape (never reshaped).
        assert getattr(row._clone, "corner_radius", 0) >= 0
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Requirement 3: interactivity (hover + click selection, no wrapper)
# --------------------------------------------------------------------------
def test_clone_hover_responds_like_hud(tmp_path, qapp):
    """Clones must keep full hover state exactly like the main interface.

    * RoundedButton clones draw their own hover colour/gradient — exercise via
      the clone mechanism on a live (TileButton) source.
    * QPushButton clones carry the source's exact styleSheet, so any ``:hover``
      rule the HUD button has is rendered identically.
    """
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()
    qapp.processEvents()

    # RoundedButton hover mechanism.
    live_rb = None
    for _items in _live_groups(app).values():
        for _k, b in _items:
            if isinstance(b, RoundedButton):
                live_rb = b
                break
        if live_rb is not None:
            break
    assert live_rb is not None
    rb_clone = panel._clone_button(live_rb)
    assert isinstance(rb_clone, RoundedButton)
    assert rb_clone._hovered is False
    rb_clone.enterEvent(None)  # type: ignore[arg-type]
    assert rb_clone._hovered is True, "RoundedButton clone must respond to hover like the HUD"
    rb_clone.leaveEvent(None)  # type: ignore[arg-type]
    assert rb_clone._hovered is False

    # QPushButton clones carry the source styleSheet (hover behaviour included).
    live_pb = None
    for _items in _live_groups(app).values():
        for _k, b in _items:
            if isinstance(b, QPushButton) and b.text():
                live_pb = b
                break
        if live_pb is not None:
            break
    assert live_pb is not None
    pb_clone = panel._clone_button(live_pb)
    assert isinstance(pb_clone, QPushButton)
    assert pb_clone.styleSheet() == live_pb.styleSheet(), \
        "QPushButton clone must copy the source styleSheet (incl. :hover)"
    app._window.close()
    qapp.processEvents()


def test_selection_shows_focus_frame_not_rectangle(tmp_path, qapp):
    """Selecting a key must frame the item with the HUD focus outer-frame
    (mouse-transparent overlay) and must NOT reshape / recolour the clone — a
    rounded button stays rounded (never forced into a rectangle)."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()
    qapp.processEvents()

    groups = _live_groups(app)
    sel_key = None
    for _bt, items in groups.items():
        if _bt == "tile":
            continue  # tile clones are filtered out of the preview
        for k, _b in items:
            sel_key = k
            break
        if sel_key:
            break
    assert sel_key is not None

    # Locate the live source button for that key (to compare shape later).
    src_btn = None
    for _bt, items in groups.items():
        for k, b in items:
            if k == sel_key:
                src_btn = b
        if src_btn is not None:
            break
    assert src_btn is not None

    panel._toggle_button(sel_key)
    qapp.processEvents()

    # The selected row is a _PreviewItem whose focus frame is now visible.
    rows = [w for i in range(panel._canvas_lay.count())
            if isinstance((w := panel._canvas_lay.itemAt(i).widget()), _PreviewItem)]
    row = next((r for r in rows if r._src_key == sel_key), None)
    assert row is not None, "selected key must have a _PreviewItem row"
    assert row._frame.isVisibleTo(row), "selected row must show the focus frame"
    assert row._frame.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents), \
        "focus frame must be mouse-transparent"
    assert "e6ff1e" in row._frame.styleSheet().lower(), \
        "focus frame must use the HUD focus colour"

    # The clone underneath keeps its native HUD shape — NOT reshaped.
    clone = row._clone
    if isinstance(clone, (RoundedButton, TileButton)):
        assert clone.corner_radius == src_btn.corner_radius, \
            "clone must keep the source corner radius (not forced rectangular)"
        assert clone._border.getRgb()[:3] != (245, 215, 75), \
            "selection must NOT recolour the clone's own border"
    else:
        assert "f5d74b" not in clone.styleSheet().lower(), \
            "selection must NOT recolour the clone's own border"
    app._window.close()
    qapp.processEvents()


def test_clone_button_is_faithful_no_selection_side_effects(tmp_path, qapp):
    """`_clone_button` must clone a TileButton faithfully and must NOT apply any
    selection styling (no ``set_selected``, no border recolour) — selection lives
    on the wrapping preview item's focus frame, never on the clone itself, so a
    clone never becomes a rectangle when its item is selected."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    live = [b for _items in _live_groups(app).values() for _k, b in _items
            if isinstance(b, TileButton)]
    assert live, "need a live TileButton source"
    clone = panel._clone_button(live[0])
    assert isinstance(clone, TileButton)
    # No selection side-effects on the clone itself.
    assert not getattr(clone, "_selected", False), "clone must not be marked selected"
    assert clone.corner_radius == live[0].corner_radius, \
        "clone must keep the source corner radius"
    app._window.close()
    qapp.processEvents()


def test_make_preview_item_focus_frame_on_selection(tmp_path, qapp):
    """``_make_preview_item(is_sel=True)`` must wrap the faithful clone in a
    ``_PreviewItem`` whose focus frame is visible; ``is_sel=False`` keeps it
    hidden.  The clone itself is never recoloured for selection."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    # Exercise the plain RoundedButton clone branch directly (the HUD only
    # creates TileButton + QPushButton).
    src = RoundedButton()
    src._border = __import__("PyQt6.QtGui", fromlist=["QColor"]).QColor(95, 175, 235)

    sel_item = panel._make_preview_item("k1", src, is_sel=True)
    assert isinstance(sel_item, _PreviewItem)
    assert sel_item._frame.isVisibleTo(sel_item)
    assert sel_item._clone is not None
    # The clone must NOT carry the selection accent on its own border.
    assert sel_item._clone._border.getRgb()[:3] != (245, 215, 75)

    unsel_item = panel._make_preview_item("k2", src, is_sel=False)
    assert isinstance(unsel_item, _PreviewItem)
    assert not unsel_item._frame.isVisibleTo(unsel_item)
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Requirement 4a: the preview list must NOT show binding-key labels (ls#0 …)
# --------------------------------------------------------------------------
def test_preview_shows_no_key_labels(tmp_path, qapp):
    """The preview list must not print the internal binding keys (``ls#0``,
    ``rtss#0`` …) or a ``[preset]`` badge next to the buttons — only the
    faithful button clones appear."""
    app = _make_app(tmp_path, qapp, [
        Tile(name="我的游戏", launch_path="C:/a", launch_type="FILE"),
    ])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()
    qapp.processEvents()

    # No label anywhere in the canvas may carry a ``block#index`` key.
    for lab in panel._canvas.findChildren(QLabel):
        assert not re.search(r"#\d", lab.text()), \
            f"preview must not show key labels, found {lab.text()!r}"
    # No clone's text is a raw key either (keys stay internal for binding).
    for c in panel._canvas.findChildren(QPushButton):
        assert not re.fullmatch(r"[a-z]+#\d+", c.text().strip()), \
            f"clone text must not be a binding key, found {c.text()!r}"
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Requirement 4b: preview clones render at the EXACT width/height of the live
#                HUD buttons (not natural sizeHint, not a fixed 24px, …)
# --------------------------------------------------------------------------
def test_clone_size_matches_source_mechanism(tmp_path, qapp):
    """``_clone_button`` must give the clone the source button's **size
    intent**, so the preview matches what the button actually looks like in
    the HUD:

    * sources with an explicit width constraint (fixedWidth / minimumWidth /
      maximumWidth) → the clone copies that constraint (pixel-faithful);
    * sources with NO explicit width constraint (their width comes purely
      from the card layout's stretch / equal split, e.g. the fan "应用" /
      undervolt "自动降压" / SMT "热核视图" full-width buttons) → the clone
      must NOT copy the stretched layout geometry (it would render as a
      giant full-row button in the editor list, wider than it should be);
      instead it keeps the content natural width (sizeHint).
    * Height is ALWAYS the source's natural content height (sizeHint), never
      the layout-stretched geometry.  This is the fix for the "button list
      shows abnormally tall buttons" regression: the old code did
      ``setFixedHeight(src.height())`` which copied a vertically-stretched
      HUD height onto the preview clone.  We assert on ``sizeHint`` (not
      ``height()``) because a freshly-cloned widget's geometry is still the
      unlaid-out 640x480 default until it enters a layout.
    """
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel

    live = [b for _items in _live_groups(app).values() for _k, b in _items
            if isinstance(b, QPushButton) and b.width() > 0 and b.height() > 0]
    assert live, "need live QPushButton sources with a realized size"
    for src in live:
        clone = panel._clone_button(src)
        assert isinstance(clone, QPushButton)
        # 高度：克隆的「自然尺寸意图」(sizeHint) 必须与源一致——始终取内容自然
        # 高，绝不复制布局拉伸后的几何高（修复：按钮列表异常高按钮）。
        # 用 sizeHint 而非 height()：克隆在加入布局前几何仍是 640x480 未布局
        # 默认值，sizeHint 才反映真实意图。
        assert clone.sizeHint().height() == src.sizeHint().height(), \
            f"clone natural height must match source: got {clone.sizeHint().height()}, " \
            f"expected {src.sizeHint().height()}"
        exp_w = src.minimumWidth() > 0 or src.maximumWidth() < 16777215
        if exp_w:
            # 有显式宽度约束 → 克隆忠实复制约束。
            assert clone.minimumWidth() == src.minimumWidth(), \
                f"clone minWidth {clone.minimumWidth()} != src {src.minimumWidth()}"
            assert clone.maximumWidth() == src.maximumWidth(), \
                f"clone maxWidth {clone.maximumWidth()} != src {src.maximumWidth()}"
        else:
            # 无显式宽度约束 → 克隆不得把布局拉伸的几何复制成固定宽度
            # （否则按钮列表里出现占满整行的巨宽按钮）。宽度交给布局按
            # 内容自然宽度(sizeHint)分配。
            assert clone.maximumWidth() >= 16777215, \
                f"clone must not fix width for stretch-laid-out source {src.text()!r}"
            assert clone.minimumWidth() <= src.sizeHint().width(), \
                f"clone must not copy stretched geometry for {src.text()!r}"
    app._window.close()
    qapp.processEvents()


def test_canvas_clone_sizes_match_live_buttons(tmp_path, qapp):
    """End-to-end: every clone rendered in the canvas must share its source
    button's *intended* (constraint-aware) size — NOT the layout-stretched
    HUD geometry.  Text buttons with no explicit width constraint render at
    their content sizeHint (so a full-row 312px button collapses to its ~56px
    content width in the list); buttons with a fixed width keep it.  Height is
    always the natural height.  This locks in the fix for the "button list
    shows abnormally tall / oversized buttons" regression, where the old code
    copied the stretched layout height/width onto the preview clone.

    The expected set is built from ``panel._collect_groups()`` — the exact
    same source ``render_canvas`` walks — so editor-chrome / popup / shortcut
    buttons (e.g. the list-popup "全选"/"清空" controls) are excluded on both
    sides and cannot cause false mismatches.
    """
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()
    qapp.processEvents()

    # Build the *intended preview size* for each rendered source button.
    expected: dict[str, tuple[str, int, int]] = {}
    for bt, items in panel._collect_groups().items():
        if bt.startswith("tile") or bt == "freeze":
            continue
        for k, b in items:
            if b.width() > 0 and b.height() > 0:
                if b.minimumWidth() > 0 or b.maximumWidth() < 16777215:
                    ew = b.minimumWidth() if b.minimumWidth() > 0 else b.maximumWidth()
                else:
                    ew = b.sizeHint().width()
                # Height is constraint-aware too: an explicit fixed height
                # (e.g. the 26x26 / 22x22 icon buttons) is preserved; otherwise
                # the clone renders at its natural content height.
                if b.minimumHeight() > 0 and b.minimumHeight() == b.maximumHeight():
                    eh = b.minimumHeight()
                elif b.maximumHeight() < 16777215:
                    eh = b.maximumHeight()
                else:
                    eh = b.sizeHint().height()
                expected[k] = (type(b).__name__, ew, eh)

    clones: dict[str, tuple[str, int, int]] = {}
    for c in panel._canvas.findChildren(QPushButton):
        key = getattr(c, "_src_key", None)
        if key is None:
            continue
        if c.text().startswith("❄") or c.text().startswith("🔥"):
            continue
        clones[key] = (type(c).__name__, c.width(), c.height())

    assert clones == expected, \
        "every preview clone must match its source button's intended width/height"
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Requirement 5: preview buttons wrap onto shared rows (FlowLayout)
# --------------------------------------------------------------------------
def test_flow_layout_wraps_and_respects_break(tmp_path, qapp):
    """Standalone check of the wrap engine: in a narrow container several
    buttons share a row (wrap, not one-per-row), and a widget carrying the
    ``flowBreak`` property occupies its own line (group headers)."""
    from ningbingjue.ui.button_edit import FlowLayout

    cont = QWidget()
    cont.setFixedWidth(200)
    flay = FlowLayout(cont, hspacing=6, vspacing=6)
    btns = []
    for i in range(4):
        b = QPushButton(f"B{i}")
        b.setFixedSize(60, 20)
        flay.addWidget(b)
        btns.append(b)
    # Header must sit on its own line and push the following button below it.
    hdr = QLabel("HEADER")
    hdr.setFixedSize(80, 16)
    hdr.setProperty("flowBreak", True)
    flay.addWidget(hdr)
    b5 = QPushButton("B5")
    b5.setFixedSize(60, 20)
    flay.addWidget(b5)

    cont.show()
    qapp.processEvents()
    qapp.processEvents()

    body_ys = [b.y() for b in btns]
    # Narrow container → the 4 buttons must wrap (fewer rows than buttons, and
    # at least one row holds more than one button).
    assert len(set(body_ys)) < len(btns), "narrow container must wrap buttons"
    assert any(body_ys.count(y) > 1 for y in set(body_ys)), \
        "multiple buttons must share a row"
    # Header is on its own line: no body button shares the header's y, and B5
    # (placed after the break) is below the header.
    assert hdr.y() not in body_ys, "flowBreak header must be on its own line"
    assert b5.y() > hdr.y()
    cont.close()
    qapp.processEvents()


def test_canvas_preview_buttons_wrap_onto_shared_rows(tmp_path, qapp):
    """End-to-end: inside the button editor the preview clones flow left→right
    and wrap onto shared rows instead of one button per line."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    overlay = app._window._edit_overlay
    overlay.open_button()
    panel = overlay._button_panel
    # Shrink the preview column so multiple buttons are forced to wrap.
    panel._canvas_scroll.setFixedWidth(240)
    panel.refresh()
    qapp.processEvents()

    clones = [c for c in panel._canvas.findChildren(QPushButton)
              if not (c.text().startswith("❄") or c.text().startswith("🔥"))]
    assert len(clones) >= 3, "need several preview clones to prove wrapping"
    ys = [c.y() for c in clones]
    assert len(set(ys)) < len(clones), \
        "preview buttons must wrap onto fewer rows than there are buttons"
    assert any(ys.count(y) > 1 for y in set(ys)), \
        "at least one row must hold multiple preview buttons"
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# refresh() must prefer L1 preset preview over L2 style
# --------------------------------------------------------------------------
def test_refresh_preserves_l1_preview(tmp_path, qapp):
    """After refresh(), if a L1 preset was selected, the custom preview
    (``_preview`` RoundedButton) must show that preset's colors — NOT the
    L2 active style."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel

    # 1. Create a preset with a *distinctive* back colour so we can tell it
    #    apart from any L2 style default.
    distinctive_back = "ff8800"  # vivid orange
    preset_name = "测试独特色"
    from ningbingjue.core.color.styles import ButtonStyle, PER_BUTTON_PRESETS
    PER_BUTTON_PRESETS[preset_name] = ButtonStyle(
        cut_size=8, corner_radius=0, border_thickness=3,
        hover_only_border=False, hover_gradient=False, link_theme=False,
        colors={"BackColor": distinctive_back, "BorderColor": "ff0000",
                "ForeColor": "ffffff", "HoverForeColor": "ffffff"})

    # 2. Refresh so the list widget picks up the newly added preset.
    panel.refresh()
    qapp.processEvents()

    # 3. Select the preset in the list — this calls _on_preset_selected →
    #    _load_preset which sets _editor + _apply_to_preview.
    items = panel._preset_list.findItems(preset_name, Qt.MatchFlag.MatchExactly)
    assert items, "test preset must appear in the QListWidget"
    panel._preset_list.setCurrentItem(items[0])
    qapp.processEvents()

    # 4. Now call refresh() again — this runs _load_style first (L2) then
    #    should restore the L1 preset because _sel_preset is still set.
    panel.refresh()
    qapp.processEvents()

    # 5. The preview button must carry the L1 preset's distinctive back colour,
    #    NOT the L2 active style's default (which is a muted mauve derivative).
    preview_back = panel._preview._back
    assert preview_back is not None
    # Verify it's the orange we set, not the default mauve-ish color.
    assert preview_back.red() == 255
    assert preview_back.green() == 136
    assert preview_back.blue() == 0

    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# 圆角生效 / 文字渲染 / 预览状态演示（本次修复）
# --------------------------------------------------------------------------
def test_corner_radius_honored_by_rounded_button(qapp):
    """圆角 > 0 时按钮轮廓应为圆角矩形（而不再只画切角六边形）。"""
    rb = RoundedButton()
    rb.corner_radius = 0
    rb.cut_size = 8
    path_cut = rb._path(100, 40)
    rb.corner_radius = 15
    path_round = rb._path(100, 40)
    # 圆角矩形元素远多于切角六边形
    assert path_round.elementCount() > path_cut.elementCount()
    # 轮廓必须覆盖整颗按钮区域
    assert path_round.boundingRect().width() == 100
    assert path_round.boundingRect().height() == 40


def test_rounded_button_renders_text_and_fore(qapp):
    rb = RoundedButton()
    rb.setText("预览按钮")
    assert rb.text() == "预览按钮"
    # 默认前景/悬停前景已就绪
    assert rb._fore.alpha() > 0
    assert rb._hover_fore.alpha() > 0
    rb.set_hover_state(True)
    assert rb._hovered is True
    rb.set_hover_state(False)
    assert rb._hovered is False


def test_styles_qpushbutton_uses_corner_radius(qapp):
    """原生 QPushButton 的 border-radius 应跟随 corner_radius（不再写死 6px）。"""
    from ningbingjue.core.color.styles import _apply_style_to_ctrl
    btn = QPushButton("x")
    st = ButtonStyle(
        cut_size=8, corner_radius=12, border_thickness=3,
        hover_only_border=False, hover_gradient=False, link_theme=False,
        colors={"BackColor": "1c1e26", "BorderColor": "5fafeb", "ForeColor": "ffffff"},
    )
    _apply_style_to_ctrl(btn, st)
    assert "border-radius: 12px" in btn.styleSheet()

    st0 = ButtonStyle(corner_radius=0,
                      colors={"BackColor": "1c1e26", "BorderColor": "5fafeb",
                              "ForeColor": "ffffff"})
    btn0 = QPushButton("y")
    _apply_style_to_ctrl(btn0, st0)
    assert "border-radius: 0px" in btn0.styleSheet()


def test_preview_state_drives_visibility(qapp, tmp_path):
    """预览状态切换应让 hover / toggle 专属颜色在预览里可见。"""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    prev = panel._preview

    panel._preview_state = "normal"
    panel._apply_preview_state()
    assert prev._hovered is False
    assert prev._is_toggle is False

    panel._preview_state = "hover"
    panel._apply_preview_state()
    assert prev._hovered is True

    panel._preview_state = "active"
    panel._apply_preview_state()
    assert prev._is_toggle is True
    assert prev._active is True

    panel._preview_state = "inactive"
    panel._apply_preview_state()
    assert prev._is_toggle is True
    assert prev._active is False

    # 文字由按钮内部绘制（不再是写死颜色的覆盖层）
    assert prev.text() == "预览按钮"
    app._window.close()
    qapp.processEvents()


def test_color_and_slider_tooltips_explained(qapp, tmp_path):
    """11 个颜色色块与 3 个几何滑块都应带中文说明（解决『看不懂』）。"""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel

    for key, sw in panel._swatches.items():
        assert sw.toolTip(), f"色块 {key} 缺少中文说明"

    all_tips = {l.toolTip() for l in panel.findChildren(QLabel)}
    assert any("圆角滑块即时生效" in t for t in all_tips), "圆角滑块缺少说明"
    assert any("切掉按钮四角" in t for t in all_tips), "切角滑块缺少说明"
    app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# Regression: the four bottom action buttons must actually DO something
# (the original "没有任何作用" bug).
# --------------------------------------------------------------------------
def test_save_as_preset_works_without_selecting_a_button(tmp_path, qapp):
    """『保存为定制预设』 must create a new L1 preset from the current editor
    even when no canvas button is selected — it must not silently bail."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()

    panel._sel_key = None
    panel._sel_preset = None
    panel._editor["colors"]["BackColor"] = "ff0000"
    before = len(PER_BUTTON_PRESETS)
    panel._save_as_preset()
    qapp.processEvents()

    assert len(PER_BUTTON_PRESETS) == before + 1, "保存为定制预设 must create a preset"
    assert "已存为预设" in panel._status.text()
    # The new preset captured the edited colour.
    new = list(PER_BUTTON_PRESETS.values())[-1]
    assert new.colors.get("BackColor") == "ff0000"
    app._window.close()
    qapp.processEvents()


def test_save_as_preset_uses_name_field(tmp_path, qapp):
    """『保存为定制预设』 names the new preset from the 名称 field when present."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()

    panel._name.setText("我的青色按钮")
    panel._preset_name_draft = "我的青色按钮"
    panel._editor["colors"]["BackColor"] = "00ffff"
    panel._save_as_preset()
    qapp.processEvents()

    assert "我的青色按钮" in PER_BUTTON_PRESETS, "name field must name the new preset"
    app._window.close()
    qapp.processEvents()


def test_overwrite_preset_gives_feedback_when_none_selected(tmp_path, qapp):
    """『覆盖当前预设』 must explain itself instead of silently doing nothing
    when no preset is selected in the list."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()

    panel._sel_preset = None
    panel._overwrite_preset()
    qapp.processEvents()

    assert "请先" in panel._status.text(), "must explain why nothing happened"
    app._window.close()
    qapp.processEvents()


def test_apply_and_refresh_restyles_unbound_selected_button(tmp_path, qapp):
    """『应用并刷新画布』 must push the current editor style onto the selected
    (still unbound) live HUD button so the user sees an immediate effect — this
    is the core of the 'no effect' fix."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()

    groups = collect_button_groups(app._window, exclude=app._window._edit_overlay)
    target = None
    target_key = None
    for _bt, items in groups.items():
        if _bt.startswith("tile") or _bt == "freeze":
            continue
        for k, b in items:
            if isinstance(b, QPushButton) and not isinstance(b, RoundedButton):
                target = b
                target_key = k
                break
        if target is not None:
            break
    assert target is not None

    panel._sel_keys = {target_key}
    # Drive the *ForeColor* (text colour) — a live HintButton (the plain
    # QPushButton this test selects) renders only ForeColor via its stylesheet;
    # BackColor is drawn by RoundedButton/TileButton painters, not a stylesheet
    # `background:`, so it can never appear in target.styleSheet().
    panel._editor["colors"]["ForeColor"] = "abcdef"
    panel._apply_and_refresh()
    qapp.processEvents()

    # abcdef -> rgb(171,205,239) must now be on the live button.
    assert "171,205,239" in target.styleSheet(), \
        "应用并刷新画布 must restyle the selected unbound live button"
    app._window.close()
    qapp.processEvents()


def test_commit_style_explains_when_no_binding(tmp_path, qapp):
    """『保存当前分配』 must tell the user how to bind instead of silently
    writing an empty map (the 'no effect' symptom)."""
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    panel.refresh()

    BUTTON_PRESET_MAP.clear()
    panel._commit_style()
    qapp.processEvents()

    assert "尚无绑定" in panel._status.text(), "must explain how to create a binding"
    app._window.close()
    qapp.processEvents()


def test_controls_scroll_area_allows_vertical_scroll(tmp_path, qapp):
    """The controls column must keep a vertical scrollbar so the property card
    (holding the four action buttons) is reachable on short windows — disabling
    it clipped the buttons off-screen (unclickable)."""
    from PyQt6.QtCore import Qt
    app = _make_app(tmp_path, qapp, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    panel = app._window._edit_overlay._button_panel
    assert panel._controls_scroll.verticalScrollBarPolicy() == \
        Qt.ScrollBarPolicy.ScrollBarAsNeeded, "controls column must be scrollable"
    app._window.close()
    qapp.processEvents()


# -- RoundedButton 渲染回归：渐变 / 悬浮边框 / 仅悬停边框 -----------------
# 说明：本沙箱为无头环境，QPixmap.render/grab 无法稳定产出像素，故不依赖
# 像素断言，而是直接断言 paintEvent 内部分支所依据的状态条件（与源码 if 语句
# 一一对应），从而确定性地锁定修复行为。
def test_gradient_top_bottom_reach_paint(qapp):
    """『渐变顶/渐变底』之前因属性名不匹配（外部赋 gradient_hover_top，
    paintEvent 读 _grad_top）而完全无效。现应通过属性桥接落到 _grad_top/_grad_bot。"""
    from ningbingjue.core.color.rgb import Color
    btn = RoundedButton()
    btn.resize(80, 36)
    btn.set_colors(Color.from_rgb(0, 0, 255), Color.from_rgb(0, 0, 255),
                   Color.from_rgb(0, 255, 0), Color.from_rgb(255, 255, 255))
    btn.hover_gradient = True
    btn.gradient_hover_top = Color.from_rgb(255, 0, 0)
    btn.gradient_hover_bottom = Color.from_rgb(0, 0, 255)
    # paintEvent 第 183-184 行直接使用这两个内部字段
    assert btn._grad_top.red() == 255 and btn._grad_top.green() == 0
    assert btn._grad_bot.blue() == 255
    # 悬停时 paintEvent 的第 181 行分支应成立 → 走渐变画刷
    btn.set_hover_state(True)
    assert btn.hover_gradient and btn._hovered, "悬停+渐变开关应触发渐变绘制分支"


def test_hover_border_overrides_on_hover(qapp):
    """『悬浮边框』（HoverBorderColor）应在悬停时覆盖常规边框色。
    断言 paintEvent 第 195 行的覆盖条件成立。"""
    from ningbingjue.core.color.rgb import Color
    btn = RoundedButton()
    btn.resize(80, 36)
    btn.set_colors(Color.from_rgb(0, 0, 255), Color.from_rgb(0, 0, 255),
                   Color.from_rgb(0, 255, 0), Color.from_rgb(255, 255, 255))
    btn.hover_border = Color.from_rgb(255, 0, 0)
    assert btn._hover_border.red() == 255
    btn.set_hover_state(True)
    # paintEvent: if self._hovered and self._hover_border.alpha() != 0: border = _hover_border
    assert btn._hovered and btn._hover_border.alpha() != 0, \
        "悬停时应采用悬浮边框色覆盖常规边框"


def test_hover_only_border_hides_idle_border(qapp):
    """『仅悬停边框』勾选后，静止态不画边框；悬停时才出现。
    断言 paintEvent 第 198 行的 NoPen 分支条件。"""
    from ningbingjue.core.color.rgb import Color
    btn = RoundedButton()
    btn.resize(80, 36)
    btn.set_colors(Color.from_rgb(0, 0, 255), Color.from_rgb(0, 0, 255),
                   Color.from_rgb(255, 0, 0), Color.from_rgb(255, 255, 255))
    btn.hover_only_border = True
    # 静止态：paintEvent 第 198 行分支成立 → 不画边框
    btn.set_hover_state(False)
    assert btn.hover_only_border and not btn._hovered, \
        "仅悬停边框: 静止态应进入 NoPen 分支"
    # 悬停态：分支不成立 → 画边框
    btn.set_hover_state(True)
    assert not (btn.hover_only_border and not btn._hovered), \
        "仅悬停边框: 悬停时应退出 NoPen 分支、绘制边框"


def test_editor_color_groups_and_renamed_labels(qapp):
    """编辑器颜色应按状态分组，且『前景→文字』『悬停前景→悬浮文字』已重命名，
    并新增『悬浮边框』字段。"""
    from ningbingjue.ui.button_edit import _COLOR_FIELDS, _COLOR_GROUPS
    keys = [k for k, _ in _COLOR_FIELDS]
    assert "HoverBorderColor" in keys, "应新增悬浮边框字段"
    labels = [l for _, l in _COLOR_FIELDS]
    assert "文字" in labels and "悬浮文字" in labels, "前景应改名为文字/悬浮文字"
    assert "前景" not in labels and "悬停前景" not in labels
    group_names = [g for g, _ in _COLOR_GROUPS]
    assert group_names == ["常规态", "悬停态", "开关态"], \
        "应按 常规/悬停/开关 三态分组（括号说明已去掉）"


def test_apply_style_sets_hover_border(qapp):
    """_apply_style_to_ctrl 应把 HoverBorderColor 应用到支持该属性的控件。"""
    from ningbingjue.core.color.rgb import Color
    from ningbingjue.core.color.styles import _apply_style_to_ctrl
    btn = RoundedButton()
    st = ButtonStyle(
        colors={
            "BackColor": "0000ff", "HoverColor": "0000ff", "BorderColor": "00ff00",
            "ForeColor": "ffffff", "HoverForeColor": "ffffff",
            "HoverBorderColor": "ff0000",
        })
    _apply_style_to_ctrl(btn, st)
    assert btn._hover_border.red() == 255, "HoverBorderColor 应写入控件悬浮边框"


# -- 滚动条 / 滑块滚轮 / 分组居中 回归 ----------------------------------
def test_sliders_ignore_mouse_wheel(tmp_path, qapp):
    """切角/圆角/边框 三个滑块不应被鼠标滚轮改变数值（避免滚动编辑器时
    误调几何参数）。"""
    from PyQt6.QtCore import Qt, QPoint, QPointF
    from PyQt6.QtGui import QWheelEvent
    from ningbingjue.ui.button_edit import _NoWheelSlider
    app = _make_app(tmp_path, qapp)
    panel = app._window._edit_overlay._button_panel
    for attr in ("_cut_slider", "_corner_slider", "_border_slider"):
        sl = getattr(panel, attr)
        assert isinstance(sl, _NoWheelSlider), f"{attr} 必须是 _NoWheelSlider"
        before = sl.value()
        # 喂一个滚轮事件，数值不应变化
        c = QPointF(panel._preview.rect().center())
        ev = QWheelEvent(c, c, QPoint(0, 0), QPoint(0, -120), Qt.MouseButton.NoButton,
                         Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollBegin, False)
        sl.wheelEvent(ev)
        assert sl.value() == before, f"{attr}: 滚轮不应改变滑块值"


def test_scrollbars_invisible_but_scrollable(tmp_path, qapp):
    """两个滚动区（画布 + 控制区）的滚动条应隐藏（width/height 0），但策略仍
    为 AsNeeded，保证内容溢出时仍能通过滚轮滚动、按钮永远可达。"""
    from PyQt6.QtCore import Qt
    app = _make_app(tmp_path, qapp)
    panel = app._window._edit_overlay._button_panel
    for sa in (panel._canvas_scroll, panel._controls_scroll):
        assert sa.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded, \
            "纵向滚动策略必须保持 AsNeeded（仍可滚）"
        assert "width: 0px" in sa.styleSheet(), "纵向滚动条应隐藏(width:0)"
        assert "height: 0px" in sa.styleSheet(), "横向滚动条应隐藏(height:0)"
    app._window.close()
    qapp.processEvents()


def test_color_groups_centered_flag(qapp):
    """颜色分组名为常规态/悬停态/开关态；三分组统一左对齐（无居中分组）。"""
    from ningbingjue.ui.button_edit import _COLOR_GROUPS, _CENTERED_GROUPS
    names = [g for g, _ in _COLOR_GROUPS]
    assert names == ["常规态", "悬停态", "开关态"], "分组名应去掉括号"
    # 设计决策：三颜色分组统一左对齐，常规态不再单独居中。
    assert _CENTERED_GROUPS == set(), "三分组应统一左对齐，无居中分组"


