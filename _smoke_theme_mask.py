"""Offscreen smoke test (v2): construct ThemeMaskPanel + drive apply_* + paintEvent directly."""
import os, sys, traceback
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, "src")

from types import SimpleNamespace
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPaintEvent

from ningbingjue.core.config import AppConfig
import ningbingjue.ui.edit_overlay as eo

def step(msg, fn):
    try:
        fn()
        print("OK  -", msg)
    except Exception as ex:
        print("FAIL-", msg, "->", repr(ex))
        traceback.print_exc()

app = QApplication([])
cfg = AppConfig()
fake_cm = SimpleNamespace(config=cfg, save=lambda: None)
fake_app = SimpleNamespace(config_manager=fake_cm, _window=None)
fake_breath = SimpleNamespace(
    _card_bg_enabled=False, _card_bg_mode="solid", _card_bg_color_hex="0e1117",
    _card_bg_image="", _card_fill_hex="141821", _card_fill_alpha=165,
    _editor_dim_alpha=118, _tray_opacity=86, _tray_color_hex="39d3c4",
    _tray_blur=18, _tray_border=True, _color_hex="39d3c4")
fake_eff = SimpleNamespace(_margin=8)
fake_overlay = SimpleNamespace(open_layout=lambda: None)

panel = None
def construct():
    global panel
    panel = eo.ThemeMaskPanel(fake_app, fake_overlay, fake_breath, fake_eff)
step("construct ThemeMaskPanel", construct)

def cards_exist():
    assert hasattr(panel, "_card_bg_card") and hasattr(panel, "_card_fill_card")
    assert hasattr(panel, "_editor_dim_card") and hasattr(panel, "_margin_card")
step("4 setting cards present", cards_exist)

def apply_all():
    panel._on_card_bg_enabled(True)
    panel._on_card_bg_mode()
    panel._on_card_fill_alpha(50)
    panel._on_editor_dim(40)
    panel._on_margin(12)
step("drive all apply_* handlers", apply_all)

def cfg_written():
    assert cfg.card_bg_enabled is True
    assert cfg.card_fill_alpha == round(50/100*255)
    assert cfg.editor_dim_alpha == round(40/100*255)
    assert cfg.card_outer_margin == 12
step("live config written by apply_*", cfg_written)

def paint_regions():
    panel._preview.resize(640, 340)
    for rid in ("tint", "base", "card", "dim", "ring"):
        panel._set_highlight(rid)
        ev = QPaintEvent(panel._preview.rect())
        panel._preview.paintEvent(ev)
step("paintEvent for all 5 regions", paint_regions)

def refresh_save_cancel():
    panel.refresh()
    panel._save()
    panel._cancel()
step("refresh/save/cancel", refresh_save_cancel)

def nav():
    items = panel.nav_items()
    assert len(items) > 0
step("nav_items build", nav)

print("SMOKE DONE")
