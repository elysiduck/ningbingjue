"""凝冰结魄 (Níngbīngjiépò) — Python port of the PowerShell overlay HUD.

This package is a faithful, modular re-implementation of the original
``ds8ZZZ.ps1`` + ``blocks/*.ps1`` overlay.  The core engine (color system,
block registry, navigation, theme, config) runs headless; the UI layer
(``ui.*``, PyQt6) and platform adapters (``platform.*``) are validated on the
Windows host.
"""

from __future__ import annotations

__version__ = "0.1.0"

# --- Process DPI awareness (must run before any QApplication is created) --------
# 必须在任何 PyQt6 / QApplication 之前声明 PerMonitorV2，否则：
#   * bundled python.exe 没有嵌入 PerMonitorV2 manifest（实测确认）
#   * Windows 把整窗按 DPR 位图拉伸 → HUD 全屏糊 + 窗口溢出裁边
# 此模块位于 src/ 内部，会被 _ext/src 覆盖机制带上掌机（run.py 不在 src/，
# 所以历来改 run.py 在掌机是无效的）。设为顶层包初始化，任何
# `from ningbingjue...` 都会先经过这里。
try:
    from .core._dpi import setup_process_dpi_awareness
    setup_process_dpi_awareness()
except Exception:  # noqa: BLE001 - 失败也不阻塞：UNAWERE 仍可启动，仅糊
    pass
