"""Regression test: the button-style editor must list buttons from EVERY page,
not just the currently-visible (page-1) cards.

Run: QT_QPA_PLATFORM=offscreen python nbj_allpages_test.py
"""
import os
import sys
import tempfile
import json

sys.path.insert(0, r"C:\Users\13567\WorkBuddy\2026-07-18-10-22-49\ningbingjue\src")
sys.path.insert(0, r"C:\Users\13567\WorkBuddy\2026-07-18-10-22-49\ningbingjue\runtime\python\Lib\site-packages")

from PyQt6.QtWidgets import QApplication
from ningbingjue.app import Application
from ningbingjue.core.config import AppConfig, Tile
from ningbingjue.ui.button_edit import ButtonEditPanel, collect_button_groups


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])

    tmp = tempfile.mkdtemp(prefix="nbj_allpages_")
    cfg_path = os.path.join(tmp, "layout.json")
    ac = AppConfig()
    ac.tiles = [Tile(name="A", launch_path="C:/a", launch_type="FILE"),
                Tile(name="B", launch_path="C:/b", launch_type="FILE")]
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(ac.to_dict(), ensure_ascii=False))

    app = Application(tmp, config_path=cfg_path, headless=False).boot()
    win = app._window
    ov = win._edit_overlay
    qapp.processEvents()

    # Force a 2-page layout: first half on page 0, second half on page 1, and
    # rebuild the HUD so it currently shows ONLY page 0.
    order = app.full_block_order()
    assert len(order) >= 2, f"need >=2 blocks, got {order}"
    split = max(1, len(order) // 2)
    app.config_manager.config.page_breaks = [0, split]
    win.rebuild()
    qapp.processEvents()

    pages = app.render_pages()
    print(f"[INFO] full order : {order}")
    print(f"[INFO] pages      : {pages}")
    assert len(pages) >= 2, f"expected >=2 pages, got {pages}"

    # Block types that live ONLY on pages other than the first one.
    page0_types = {t.split("#")[0] for t in pages[0]}
    other_page_types = {
        t.split("#")[0] for page in pages[1:] for t in page
        if not t.startswith("tile")
    } - page0_types
    print(f"[INFO] other-page block types (must appear in editor): {sorted(other_page_types)}")
    assert other_page_types, "no non-tile block lives on a later page to test"

    # Sanity: with the HUD on page 0 only, the LIVE collection must NOT contain
    # the other-page blocks — this is the exact bug we are fixing.
    live_before = collect_button_groups(win, exclude=ov)
    live_missing = other_page_types - set(live_before.keys())
    print(f"[INFO] live (page-0 only) missing other-page types: {sorted(live_missing)}")
    assert live_missing, "precondition failed: other-page blocks already visible pre-fix?"

    # Open the button-style editor (this should expand the HUD to all pages).
    ov.open_button()
    qapp.processEvents()
    bp = ov._button_panel
    assert isinstance(bp, ButtonEditPanel)
    assert win._editor_all_pages is True, "all-pages flag must be ON in button mode"

    # Collect the groups the editor's list is built from.
    groups = bp._collect_groups()
    found_types = set(groups.keys())
    print(f"[INFO] editor collected block types: {sorted(found_types)}")

    missing = other_page_types - found_types
    assert not missing, f"editor is MISSING buttons from other pages: {sorted(missing)}"

    total_other = sum(len(groups.get(bt, [])) for bt in other_page_types)
    print(f"[OK] editor lists {total_other} button(s) from non-page-1 blocks")
    assert total_other > 0

    # ---- revert path: leaving the editor must collapse back to one page ----
    ov.open_layout()  # simulate the editor's "返回" (back) button
    qapp.processEvents()
    assert win._editor_all_pages is False, "all-pages must revert when leaving button mode"
    live_after = collect_button_groups(win, exclude=ov)
    assert not (other_page_types & set(live_after.keys())), \
        "HUD should be back to a single page after leaving the editor"
    print("[OK] leaving button mode reverts HUD to a single page")

    win.close()
    qapp.processEvents()
    print()
    print("=== all-pages button list regression test PASSED ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
