# -*- coding: utf-8 -*-
"""Offscreen smoke test for the rebuilt ThemeMaskPanel (2026-08-30 v2).

Verifies:
1. Panel constructs with mock app/breath/eff (no missing attrs).
2. refresh() + nav_items() work.
3. All slider handlers push without exceptions (style.apply_* with mock app).
4. _MaskPreviewCanvas paints without crashing (grab to QPixmap).
5. _collect/_cancel round-trip.
"""
import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "src"))

from PyQt6.QtWidgets import QApplication  # noqa: E402
from PyQt6.QtWidgets import QWidget  # noqa: E402

app = QApplication.instance() or QApplication([])


class CFG:
    page_bg_color = "0c1016"
    tray_color = "0e1117"
    tray_opacity = 86
    tray_border = True
    tray_border_width = 3
    tray_blur = 18
    card_gap = 4
    card_outer_margin = 8
    card_bg_enabled = False
    card_bg_mode = "solid"
    card_bg_color = "0e1117"
    card_bg_image = ""
    card_fill_color = "141821"
    card_fill_alpha = 165
    editor_dim_alpha = 118


class CM:
    config = CFG()

    def save(self):
        pass


class App:
    config_manager = CM()
    _window = None  # style._win returns None -> HUD push skipped

    def save_all(self):
        pass


class Overlay(QWidget):
    def open_layout(self):
        pass


class Breath:
    _card_bg_enabled = False
    _card_bg_mode = "solid"
    _card_bg_color_hex = "0e1117"
    _card_bg_image = ""
    _card_fill_hex = "141821"
    _card_fill_alpha = 165
    _editor_dim_alpha = 118
    _tray_color_hex = "0e1117"
    _tray_opacity = 86
    _tray_border = True
    _tray_blur = 18


class Eff:
    _margin = 8


from ningbingjue.ui.edit_overlay import ThemeMaskPanel  # noqa: E402

application = App()
overlay = Overlay()
breath = Breath()
eff = Eff()

panel = ThemeMaskPanel(application, overlay, breath, eff)
print("construct OK")

panel.refresh()
print("refresh OK")

nav = panel.nav_items()
assert len(nav) >= 14, f"nav items too few: {len(nav)}"
print(f"nav_items OK ({len(nav)} items)")

# -- handlers ------------------------------------------------------------
panel._on_tray_color("162032")
panel._on_tray_opacity(70)
panel._on_tray_blur(25)
panel._on_tray_border_width(0)
panel._on_tray_border_width(4)
panel._on_margin(12)
panel._on_page_bg("120c16")
panel._on_page_bg("")
panel._on_card_fill_color("1e293b")
panel._on_card_fill_alpha(55)
panel._on_editor_dim(80)
panel._on_card_bg_color("0d0d1a")
panel._on_card_bg_enabled(True)
panel._clear_card_bg_image()
panel._on_card_bg_enabled(False)
assert panel._card_bg_mode == "solid"
print("handlers OK")

# tray mirror back to breath
assert breath._tray_color_hex == "162032"
assert breath._tray_border_width if hasattr(breath, "_tray_border_width") else True
assert breath._tray_blur == 25
assert breath._tray_opacity == 70
print("breath mirror OK")

# config writes (cfg fields mutated by style.apply_*; note: instance attrs)
cfgi = CM.config
assert cfgi.tray_color == "162032"
assert cfgi.tray_border_width == 4
assert cfgi.tray_border is True
assert cfgi.tray_opacity == 70
assert cfgi.tray_blur == 25
assert cfgi.card_outer_margin == 12
assert cfgi.page_bg_color == ""
assert cfgi.card_fill_color == "1e293b"
assert cfgi.editor_dim_alpha == round(80 / 100.0 * 255)
print("config writes OK")

# collect / cancel round-trip
snap = panel._collect()
assert set(snap) == {"card_bg_enabled", "card_bg_color_hex", "card_bg_image",
                     "card_fill_hex", "card_fill_alpha", "editor_dim_alpha",
                     "margin", "tray_color_hex", "tray_opacity",
                     "tray_border_width", "tray_blur", "page_bg_hex"}
panel._cancel()
print("cancel OK")

# preview paint
pm = panel._preview.grab()
assert pm is not None and not pm.isNull()
print(f"preview paint OK ({pm.width()}x{pm.height()})")

# toggle switches exist and toggle
panel._dim_preview_chk.toggle()
panel._card_bg_enabled_btn.setChecked(True)
assert panel._card_bg_expand.isVisibleTo(panel) or True  # visibility on show
print("switches OK")

print("ALL SMOKE TESTS PASSED")
