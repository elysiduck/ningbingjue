"""Shared page-appearance style application (「主题与蒙版」单一真值源).

Each ``apply_*`` does TWO things atomically:

1. 写 live config —— ``app.config_manager.config.<field>``（内存态，随后由
   ``app.save_all()`` / ``config_manager.save()`` 落盘）；
2. 推给真实 HUD —— 调 ``app._window`` 上已有的 ``apply_card_bg`` /
   ``apply_card_fill`` / ``apply_editor_dim`` / ``apply_card_layout``。

这样「主题与蒙版」页（自建卡）与旧 LayoutEditPanel / LayoutEffectPanel（原卡）
改同一项时，走的是同一个函数 = 同一份内存态 + 同一份 HUD 推送，天然同步，
不会出现「两边改值不同步 / 互相覆盖」。

本模块是叶子模块：只接收 ``app`` 作为参数，不反向 import ``ui``，避免循环依赖。
alpha 相关参数统一用 0-255（与 window 方法、config 字段一致）；百分比 UI 由
调用方在 ThemeMaskPanel 内转换。
"""
from __future__ import annotations

from typing import Any


def _cfg(app: Any):
    cm = getattr(app, "config_manager", None)
    return getattr(cm, "config", None) if cm is not None else None


def _win(app: Any):
    return getattr(app, "_window", None)


def apply_card_bg(app: Any, enabled: bool, mode: str,
                  color_hex: str, image_path: str) -> None:
    """卡片区背景 / 替换底色（同一 card_bg 设置）。

    ``enabled`` 关 → 保留原始磨砂托盘；开 → 纯色或图片铺底。
    """
    cfg = _cfg(app)
    if cfg is not None:
        cfg.card_bg_enabled = bool(enabled)
        cfg.card_bg_mode = "image" if str(mode) == "image" else "solid"
        cfg.card_bg_color = str(color_hex or "")
        cfg.card_bg_image = str(image_path or "")
    win = _win(app)
    if win is not None and hasattr(win, "apply_card_bg"):
        try:
            win.apply_card_bg(
                bool(enabled),
                "image" if str(mode) == "image" else "solid",
                str(color_hex or ""),
                str(image_path or ""),
            )
        except Exception:
            pass


def apply_card_fill(app: Any, color_hex: str, alpha: int) -> None:
    """单卡填充：每卡填充色 + 不透明度（alpha 0-255）。"""
    a = max(0, min(255, int(alpha)))
    cfg = _cfg(app)
    if cfg is not None:
        cfg.card_fill_color = str(color_hex or "")
        cfg.card_fill_alpha = int(a)
    win = _win(app)
    if win is not None and hasattr(win, "apply_card_fill"):
        try:
            win.apply_card_fill(str(color_hex or ""), a)
        except Exception:
            pass


def apply_editor_dim(app: Any, alpha: int) -> None:
    """编辑页蒙版深度（alpha 0-255）。"""
    a = max(0, min(255, int(alpha)))
    cfg = _cfg(app)
    if cfg is not None:
        cfg.editor_dim_alpha = int(a)
    win = _win(app)
    if win is not None and hasattr(win, "apply_editor_dim"):
        try:
            win.apply_editor_dim(a)
        except Exception:
            pass


def apply_card_margin(app: Any, margin_px: int) -> None:
    """外圈留白（px）。同时读 card_gap 一起推给 apply_card_layout。"""
    m = max(0, int(margin_px))
    cfg = _cfg(app)
    gap = 4
    if cfg is not None:
        cfg.card_outer_margin = m
        gap = max(0, int(getattr(cfg, "card_gap", 4) or 4))
    win = _win(app)
    if win is not None and hasattr(win, "apply_card_layout"):
        try:
            win.apply_card_layout(gap, m)
        except Exception:
            pass


def apply_tray_settings(app: Any, color_hex: str, opacity_pct: int,
                        border_width: int, blur_px: int) -> None:
    """底板（托盘）四件套：颜色 / 透明度% / 外框线宽度 / 模糊。

    「主题与蒙版」页（2026-08-30 起）与「呼吸灯自定义」页共用本函数 =
    单一真值源：同时写 cfg.tray_*（tray_border 由 width>0 派生，保持与旧
    字段一致）+ 推 ``win.apply_tray``（border_width 走关键字参数，兼容旧
    4 参调用）。
    """
    w = max(0, min(4, int(border_width)))
    op = max(0, min(100, int(opacity_pct)))
    bl = max(0, min(50, int(blur_px)))
    cfg = _cfg(app)
    if cfg is not None:
        cfg.tray_color = str(color_hex or "0e1117")
        cfg.tray_opacity = op
        cfg.tray_border = w > 0
        cfg.tray_border_width = w
        cfg.tray_blur = bl
    win = _win(app)
    if win is not None and hasattr(win, "apply_tray"):
        try:
            win.apply_tray(str(color_hex or "0e1117"), op, w > 0, bl,
                           border_width=w)
        except Exception:
            pass


def apply_page_bg(app: Any, color_hex: str) -> None:
    """页面底色（整个 HUD 窗口背景）。空串 = 跟随主题 bg_app。"""
    hexv = str(color_hex or "").strip().lstrip("#")
    if len(hexv) != 6:
        hexv = ""
    cfg = _cfg(app)
    if cfg is not None:
        cfg.page_bg_color = hexv
    win = _win(app)
    if win is not None and hasattr(win, "apply_page_bg"):
        try:
            win.apply_page_bg(hexv)
        except Exception:
            pass
