"""渲染呼吸灯整合预览的实际外观（offscreen），产出 PNG 供人工核对。

输出（项目根）：
  _shot_breath_0.5s.png / _shot_breath_2.4s.png / _shot_breath_8.0s.png
  —— 同一套参数下，只改「呼吸时间间隔」，用来直观确认波形确实随周期伸缩。
"""
import os
import sys
from types import SimpleNamespace as NS

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "src"))
_ROOT = os.path.dirname(_HERE)

from PyQt6.QtCore import QPoint                            # noqa: E402
from PyQt6.QtGui import QColor, QPixmap, QRegion           # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget          # noqa: E402
from ningbingjue.ui.edit_overlay import BreathingEditPanel  # noqa: E402

app = QApplication(sys.argv)

cfg = NS(breath_period_s=2.4, breath_max_brightness=1.0, breath_color="7fd1ff",
         breath_enabled=True, breath_floor=0.1, breath_flash_ms=180,
         breath_hold_ms=120, breath_scrim_ms=200,
         tray_color="0e1117", tray_opacity=86, tray_border=True, tray_blur=18,
         editor_dim_alpha=118, card_bg_enabled=False, card_bg_mode="solid",
         card_bg_color="0e1117", card_bg_image="",
         card_fill_color="141821", card_fill_alpha=165)
panel = BreathingEditPanel(NS(config_manager=NS(config=cfg), _window=None),
                           NS(faq=lambda: None, open_layout=lambda: None,
                              _apply_window_border=lambda: None))
panel.refresh()
panel.resize(760, 720)
panel.show()
app.processEvents()

pv = panel._preview
pv.resize(720, 146)
pv._canvas.resize(700, 100)
app.processEvents()


def shot(tenths, name, focus=True):
    """渲染预览。focus=False 时按下「失去焦点」让黑罩显现。"""
    panel._period_slider.setValue(tenths)
    app.processEvents()
    if focus:
        pv.demo_gain()
    else:
        pv.demo_lose()
    for _ in range(12):                 # 让状态机推进到稳定（黑罩/冲亮完成）
        app.processEvents()
        pv._tick()
    dpr = max(1.0, float(pv.devicePixelRatio()))
    pm = QPixmap(int(pv.width() * dpr), int(pv.height() * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(QColor(14, 17, 23))         # 与 preview 的 stylesheet 背景一致
    pv.render(pm, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
    out = os.path.join(_ROOT, name)
    pm.save(out)
    print("saved:", out, pm.size())


shot(5, "_shot_breath_0.5s.png")
shot(24, "_shot_breath_2.4s.png")
shot(80, "_shot_breath_8.0s.png")
shot(24, "_shot_breath_unfocus.png", focus=False)
