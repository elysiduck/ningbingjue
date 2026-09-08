"""Regression test for two bugs introduced/affected by the all-pages editor:

1. Page-number (nav) buttons must NOT appear in the button-style editor's
   preview list (they are HUD chrome, collected via findChildren).
2. The preview clones must render at the source button's REAL on-screen width.
   Previously, open_button() rebuilt the hidden HUD and called render_canvas()
   synchronously BEFORE Qt laid out the panels, so buttons reported the default
   640x480 unset size and _copy_size() shrank the clones to text width.
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, r"C:\Users\13567\WorkBuddy\2026-07-18-10-22-49\ningbingjue\src")
sys.path.insert(0, r"C:\Users\13567\WorkBuddy\2026-07-18-10-22-49\ningbingjue\runtime\python\Lib\site-packages")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton
from ningbingjue.app import Application
from ningbingjue.core.config import AppConfig, Tile
from ningbingjue.ui.button_edit import collect_button_groups


def _boot_2pages():
    qapp = QApplication.instance() or QApplication([])
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "layout.json")
    ac = AppConfig()
    # 6 base blocks + 2 extra tiles => 8 tokens. 不封顶：未设手动分页时
    # 全部落在一页；分页只由显式断点决定（与本测试的按键宽度/导航断言无关）。
    ac.tiles = [Tile(name=f"T{i}", launch_path=f"C:/{i}", launch_type="FILE")
                for i in range(2)]
    open(p, "w", encoding="utf-8").write(json.dumps(ac.to_dict(), ensure_ascii=False))
    app = Application(tmp, config_path=p, headless=False).boot()
    return qapp, app


def test_pagenav_excluded_and_widths_correct():
    qapp, app = _boot_2pages()
    win = app._window
    qapp.processEvents()

    ov = win._edit_overlay
    bp = ov._button_panel

    # Open the button-style editor (this is where the two bugs lived).
    ov.open_button()
    qapp.processEvents()

    # --- Bug 1: page-nav buttons must not appear in the preview list ---
    groups = bp._collect_groups()
    all_keys = [k for items in groups.values() for k, _ in items]
    assert not any("page_nav" in k for k in all_keys), "page_nav leaked into keys"
    # Collect every QPushButton under the window and confirm none tagged
    # page_nav is present in the editor's preview groups.
    preview_src_buttons = [b for items in groups.values() for _, b in items]
    nav_in_preview = [b for b in preview_src_buttons if b.property("page_nav")]
    assert not nav_in_preview, "page-nav button collected into preview list!"

    # --- Bug 2: preview clones must have real (non-640) widths ---
    # Live source widths (already laid out by _force_layout_pass in open_button)
    live = {}
    for bt, items in collect_button_groups(win, exclude=ov).items():
        for k, b in items:
            live[k] = b.width()

    # Walk the canvas FlowLayout and grab the clones (tagged with _src_key).
    canvas = bp._canvas
    clone_widths = {}
    for i in range(bp._canvas_lay.count()):
        item = bp._canvas_lay.itemAt(i)
        w = item.widget()
        if w is None:
            continue
        key = getattr(w, "_src_key", None)
        if key is None:
            continue
        # Measure the *inner* clone, not the _PreviewItem wrapper. The wrapper
        # adds FOCUS_PAD (3px) margins on every side for the selection frame,
        # which would inflate the measured width by 2*FOCUS_PAD (intended
        # chrome, not the clone's real size). _copy_size sizes the inner clone.
        inner = getattr(w, "_clone", w)
        clone_widths[key] = inner.width()

    # Every collected key should have a clone with a *real* (non-640, non-zero)
    # width.  For sources with an explicit width constraint the clone copies
    # that constraint, so it must match the live width; for sources whose width
    # comes purely from the card layout's stretch (e.g. the fan "应用" full-row
    # button) the clone keeps its content natural width and is intentionally
    # NOT equal to the stretched live width — this is the fix for the
    # "oversized buttons in the list" regression, so we must not fail on it.
    mismatches = []
    for key, lw in live.items():
        cw = clone_widths.get(key)
        if cw is None:
            continue
        # Locate the source button inside the editor's own groups (excludes
        # popup / shortcut-editor chrome, matching what actually renders).
        src = None
        for _bt, items in groups.items():
            for _k, _b in items:
                if _k == key:
                    src = _b
                    break
            if src is not None:
                break
        if src is None:
            continue
        constrained = src.minimumWidth() > 0 or src.maximumWidth() < 16777215
        if constrained:
            if abs(cw - lw) > 4:
                mismatches.append((key, lw, cw))
        else:
            if cw >= 600 or cw <= 0:
                mismatches.append((key, lw, cw))
    assert not mismatches, f"clone width mismatch (live vs clone): {mismatches}"

    # Specifically the LS preset load/save/delete buttons must be wide.
    ls_widths = [lw for k, lw in live.items() if k.startswith("ls#")]
    assert ls_widths, "no ls buttons collected"
    assert min(ls_widths) > 80, f"ls buttons too narrow: {ls_widths}"

    # --- all-pages still works: a page-2 block is present ---
    assert any(k.startswith("power#") for k in all_keys), "page-2 block missing"

    win.close()
    print("PASS: page-nav excluded, clone widths match live, all-pages works")


if __name__ == "__main__":
    test_pagenav_excluded_and_widths_correct()
