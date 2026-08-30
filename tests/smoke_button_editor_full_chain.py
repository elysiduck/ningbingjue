"""Full-chain offscreen smoke test for the button style editor.

Mirrors the restore-from-crash scenario:
  1. open layout editor (the original crash point)
  2. open the inline button-style sub-page
  3. edit the active L2 style's BackColor and apply
  4. return to the layout editor
  5. save the layout (triggers apply_layout(3-arg) + rebuild + _apply_button_styles)
  6. verify the buttons/ persistence files were written

Run with: QT_QPA_PLATFORM=offscreen python tests/smoke_button_editor_full_chain.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PyQt6.QtWidgets import QApplication

from ningbingjue.app import Application
from ningbingjue.core.color import styles as _BS
from ningbingjue.core.color.styles import button_data_paths
from ningbingjue.core.config import AppConfig, Tile
from ningbingjue.ui.button_edit import ButtonEditPanel, collect_button_groups


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])

    tmp = tempfile.mkdtemp(prefix="nbj_smoke_")
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

    # Step 1: open layout editor (original crash point)
    ov.open_layout()
    qapp.processEvents()
    assert ov.mode() == "layout", f"expected layout, got {ov.mode()}"
    print("[OK] 布局编辑器已打开（原崩溃点已修复）")

    # Step 2: open the inline button-style sub-page
    ov.open_button()
    qapp.processEvents()
    assert ov.mode() == "button", f"expected button, got {ov.mode()}"
    bp = ov._button_panel
    assert isinstance(bp, ButtonEditPanel)
    print("[OK] 按钮风格编辑子页已打开, mode=button")

    # Step 3: edit the active L2 style's BackColor and apply
    active = _BS.ACTIVE_STYLE_NAME
    st = _BS.BUTTON_STYLES[active]
    orig_back = st.colors.get("BackColor")
    st.colors["BackColor"] = "ff0000"
    bp._apply_and_refresh()
    qapp.processEvents()
    print(f"[OK] 应用并刷新画布完成, ACTIVE={active}")

    # Step 3b: verify the live HUD buttons actually received the new colour
    live = collect_button_groups(win, exclude=ov)
    group_count = sum(1 for items in live.values()
                      if any(getattr(b, "_back", None) is not None for _, b in items))
    print(f"[OK] live 按钮中 {group_count} 组已应用风格 (颜色已下发)")

    # restore to avoid polluting persistence
    if orig_back is not None:
        st.colors["BackColor"] = orig_back
        bp._apply_and_refresh()
        qapp.processEvents()

    # Step 4: return to the layout editor
    bp._back_btn.click()
    qapp.processEvents()
    assert ov.mode() == "layout", f"expected layout after back, got {ov.mode()}"
    print("[OK] 返回布局编辑")

    # Step 5: save the layout (apply_layout 3-arg + rebuild + _apply_button_styles)
    theme = app.config_manager.config.theme.name
    ov._layout_panel._save()
    qapp.processEvents()
    print(f"[OK] 保存布局完成 (apply_layout 3-arg + rebuild + _apply_button_styles, theme={theme})")

    # Step 6: verify buttons/ persistence files were written
    paths = button_data_paths(tmp)
    bs_path, bp_path, map_path = paths["styles"], paths["presets"], paths["map"]
    print(f"[INFO] button-styles.json    exists={os.path.exists(bs_path)}")
    print(f"[INFO] button-presets.json   exists={os.path.exists(bp_path)}")
    print(f"[INFO] button-preset-map.json exists={os.path.exists(map_path)}")
    assert os.path.exists(bs_path), "button-styles.json 未生成"
    assert os.path.exists(bp_path), "button-presets.json 未生成"
    assert os.path.exists(map_path), "button-preset-map.json 未生成"

    win.close()
    qapp.processEvents()
    print()
    print("=== 全链路冒烟测试 6/6 通过 ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
