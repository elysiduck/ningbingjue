"""复现+验证：按钮布局编辑器列表里部分按钮宽度过宽（被 HUD 布局拉伸的全宽按钮）。

修复目标（2026-08-21）：
- 风扇「应用」/ 降压「自动降压」/ SMT「热核视图」这类**无显式宽度约束**的按钮，
  克隆应按内容自然宽度(sizeHint)显示，而不是复制 HUD 拉伸后的几何(392px)。
- 有显式宽度约束（fixedWidth/minWidth/maxWidth）的按钮保持忠实复制（如
  「自动锁帧」149、「GPU浮动」193、「应用[power]」193）。

运行：QT_QPA_PLATFORM=offscreen python -m pytest tests/test_btnlist_clone_width.py -q -s
"""
from __future__ import annotations

import json

import pytest
from PyQt6.QtWidgets import QApplication

from ningbingjue.app import Application
from ningbingjue.core.config import AppConfig, Tile
from ningbingjue.ui.button_edit import _PreviewItem
from ningbingjue.ui.gamepad_hint import HintButton


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _boot(tmp_path, qapp):
    cfg = tmp_path / "layout.json"
    ac = AppConfig()
    ac.tiles = [Tile(name="A", launch_path="C:/a", launch_type="FILE"),
                Tile(name="B", launch_path="C:/b", launch_type="FILE")]
    cfg.write_text(json.dumps(ac.to_dict(), ensure_ascii=False), encoding="utf-8")
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    qapp.processEvents()
    return app


def test_preview_clone_width_matches_source(tmp_path, qapp):
    app = _boot(tmp_path, qapp)
    win = app._window
    ov = win._edit_overlay
    qapp.processEvents()

    ov.open_button()
    qapp.processEvents()
    bp = ov._button_panel
    bp.show()
    for _ in range(3):
        bp._canvas.resize(bp._canvas.size())
        qapp.processEvents()

    rows = []
    for i in range(bp._canvas_lay.count()):
        it = bp._canvas_lay.itemAt(i)
        w = it.widget() if it else None
        if not isinstance(w, _PreviewItem):
            continue
        clone = w._clone
        key = getattr(clone, "_src_key", "?")
        src = bp._live_button(key)
        if src is None:
            continue
        rows.append((src.width(), clone.width(), key,
                     src.text()[:16], type(src).__name__,
                     src.minimumWidth(), src.maximumWidth(),
                     clone.sizeHint().width()))

    rows.sort(key=lambda r: -(r[1] - r[0]))
    print(f"\n{'srcW':>5} {'cloneW':>6}  {'csh':>5}  key  (type:text)"
          f"  src[min,max]")
    bad_full = []   # 无显式约束却仍被复制成远超内容宽度的几何（HUD 拉伸遗留）
    for srcw, clonew, key, txt, tname, smin, smax, csh in rows:
        d = clonew - srcw          # 仅供显示：HUD 可能拉伸/压缩源，故不用于判定
        mark = ""
        no_explicit_w = (smin <= 0 and smax >= 16777215)
        if no_explicit_w and (clonew - csh) >= 50:
            # 无显式宽度约束的按钮，克隆却比自身内容宽度(sizeHint)宽出 >=50px：
            # 说明克隆继承了 HUD 拉伸后的几何（2026-08-21 修复目标）。
            mark = "  <<< 无约束却被拉伸(超内容>=50)"
            bad_full.append((key, csh, clonew))
        elif no_explicit_w and abs(clonew - csh) <= 2:
            mark = "  按内容宽度 ✓"
        elif not no_explicit_w:
            # 有显式宽度约束：克隆应忠实复制该约束（与内容 sizeHint 无关）。
            mark = "  显式约束(忠实复制) ✓"
        else:
            mark = "  无约束但宽度≠sizeHint"
        print(f"{srcw:>5} {clonew:>6}  {csh:>5}  {tname}:{txt} [{key}]"
              f"  src[{smin},{smax}]{mark}")
    print("[total]", len(rows), "| 无约束被拉伸:", len(bad_full))
    # 关键判据：无显式约束的按钮，克隆宽度应≈自身内容宽度(sizeHint)，
    # 而不是被 HUD 拉伸几何带偏。以克隆自身 sizeHint 为基准 → 平台无关。
    assert not bad_full, f"无显式约束按钮仍被拉伸超内容宽度: {bad_full}"

    # 明确断言用户报告的三个按钮：克隆宽度应接近内容自然宽度（sizeHint），
    # 而不是 HUD 拉伸后的几何。以克隆自身的 sizeHint 为基准（而非写死的魔法数），
    # 避免字体/文案微调导致的内容宽度漂移被误判为回归。
    for key in ("fan#4", "undervolt#0", "smt#0"):
        clone = None
        for i in range(bp._canvas_lay.count()):
            it = bp._canvas_lay.itemAt(i)
            w = it.widget() if it else None
            if isinstance(w, _PreviewItem) and getattr(w._clone, "_src_key", "") == key:
                clone = w._clone
                break
        assert clone is not None, f"未找到克隆 {key}"
        src = bp._live_button(key)
        assert src is not None, f"未找到源按钮 {key}"
        csh = clone.sizeHint().width()
        print(f"\n[{key}] clone.width()={clone.width()} clone.sizeHint={csh} "
              f"src.width()={src.width()}")
        # 克隆应按内容自然宽度显示（≈自身 sizeHint），而非 HUD 拉伸/压缩几何。
        # 以克隆自身 sizeHint 为基准 → 平台无关（offscreen 与真实窗口一致）。
        assert abs(clone.width() - csh) <= 8, \
            f"{key} 克隆宽度 {clone.width()} 偏离内容自然宽度 {csh}"
