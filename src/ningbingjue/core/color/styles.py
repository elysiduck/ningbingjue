"""§7 Button style engine — predefined styles + per-button overrides.

Mirrors ``colors.ps1``: ``Get-StyleDefaultColors``, ``$global:ButtonStyles``,
``Apply-ButtonStyle`` / ``Set-GlobalButtonStyle`` and the JSON (de)serialization
of ``buttons/button-styles.json``.

NOTE: the original ``Get-StyleDefaultColors`` tested ``$global:ActiveThemeName``
for the light theme.  Light/dark now follows the breathing-light (theme-accent)
brightness via :func:`is_light_theme`, so the old ``girlpink`` typo is gone.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .api import get_color_derivative
from .palette import COLOR_MAUVE, FG_SECONDARY
from .rgb import Color, color_to_hex, hex_to_color
from .semantic import is_light_theme
from .state import STATE


@dataclass
class ButtonStyle:
    """A button style definition (geometry + theme-linked colors)."""

    cut_size: int = 8
    corner_radius: int = 0
    border_thickness: int = 4
    hover_only_border: bool = False
    hover_gradient: bool = True
    link_theme: bool = True
    colors: dict[str, str] = field(default_factory=dict)  # hex strings

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "CutSize": self.cut_size,
            "CornerRadius": self.corner_radius,
            "BorderThickness": self.border_thickness,
            "HoverOnlyBorder": self.hover_only_border,
            "HoverGradient": self.hover_gradient,
            "LinkTheme": self.link_theme,
        }
        for k, v in self.colors.items():
            d[k] = v
        return d

    # Structural (non-color) keys written by :meth:`to_dict`.  Every other key
    # in the dict is treated as a colour so ALL preset attributes — including
    # ``HoverBorderColor`` and any future colour — are saved AND restored.
    _STRUCTURAL_KEYS = (
        "CutSize", "CornerRadius", "BorderThickness",
        "HoverOnlyBorder", "HoverGradient", "LinkTheme",
    )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ButtonStyle":
        colors = {
            k: str(d[k])
            for k in d
            if k not in cls._STRUCTURAL_KEYS and d.get(k)
        }
        return cls(
            cut_size=int(d.get("CutSize", 8)),
            corner_radius=int(d.get("CornerRadius", 0)),
            border_thickness=int(d.get("BorderThickness", 4)),
            hover_only_border=bool(d.get("HoverOnlyBorder", False)),
            hover_gradient=bool(d.get("HoverGradient", True)),
            link_theme=bool(d.get("LinkTheme", True)) if d.get("LinkTheme") is not None else True,
            colors=colors,
        )


def get_style_default_colors(style_name: str | None) -> dict[str, str]:
    """``Get-StyleDefaultColors`` — returns hex color dict for a style."""
    is_light = is_light_theme()
    accent = STATE.theme_accent_colors if STATE.theme_accent_colors else None
    role = accent.get("ThemeTitleColor") if accent else None
    if role is None:
        role = COLOR_MAUVE

    back = get_color_derivative(role, 0.22)
    hover = get_color_derivative(role, 0.40)
    border = role
    fore = role
    hover_fore = Color.from_rgb(255, 255, 255)
    grad_top = get_color_derivative(role, 0.55)
    grad_bot = get_color_derivative(role, 0.18)

    if is_light:
        back = Color.from_rgb(252, 240, 245)
        hover = Color.from_rgb(255, 250, 252)
        fore = Color.from_rgb(170, 50, 85)
        hover_fore = Color.from_rgb(170, 50, 85)

    if is_light:
        active_back = get_color_derivative(role, 0.65)
        active_border = role
        inactive_back = get_color_derivative(FG_SECONDARY, 0.40, Color.from_rgb(230, 225, 232))
        inactive_border = get_color_derivative(FG_SECONDARY, 0.70, Color.from_rgb(200, 195, 205))
    else:
        active_back = get_color_derivative(role, 0.30)
        active_border = role
        inactive_back = get_color_derivative(role, 0.08)
        inactive_border = get_color_derivative(role, 0.20)

    use_gradient = style_name is None or style_name == "darktech"
    return {
        "GradientHoverTop": color_to_hex(grad_top) if use_gradient else "00000000",
        "GradientHoverBottom": color_to_hex(grad_bot) if use_gradient else "00000000",
        "BackColor": color_to_hex(back),
        "HoverColor": color_to_hex(hover),
        "BorderColor": color_to_hex(border),
        "ForeColor": color_to_hex(fore),
        "HoverForeColor": color_to_hex(hover_fore),
        "ActiveBackColor": color_to_hex(active_back),
        "ActiveBorderColor": color_to_hex(active_border),
        "InactiveBackColor": color_to_hex(inactive_back),
        "InactiveBorderColor": color_to_hex(inactive_border),
    }


def _build_default_styles() -> dict[str, dict[str, str]]:
    """Return the four default themes, each with an empty assignment map.
    The themes are named containers — users fill them by assigning L1
    presets to buttons and saving the composition."""
    return {
        "暗黑科技": {},
        "柔和圆角": {},
        "极简线条": {},
        "霓虹边框": {},
    }


# In-memory registry of button style *themes* (整体风格).
# Each theme is a snapshot of the current button→preset assignments:
#   theme_name → {button_key: preset_name}
# The four default themes start with empty assignments — buttons keep
# their native (HUD-default) look until the user assigns L1 presets.
BUTTON_STYLES: dict[str, dict[str, str]] = _build_default_styles()
ACTIVE_STYLE_NAME = "暗黑科技"


def set_global_button_style(style_name: str) -> None:
    """``Set-GlobalButtonStyle`` — activate a theme and push its saved preset
    assignments to every registered button.  Buttons without an assignment
    stay at their native HUD default (no uniform style layer)."""
    global ACTIVE_STYLE_NAME
    if style_name not in BUTTON_STYLES:
        style_name = ACTIVE_STYLE_NAME
    ACTIVE_STYLE_NAME = style_name
    assignments = BUTTON_STYLES.get(style_name, {})
    # Load the theme's saved assignments into the active binding map so
    # subsequent apply_styles_to_buttons picks them up.
    BUTTON_PRESET_MAP.clear()
    BUTTON_PRESET_MAP.update(assignments)
    live = [b for b in STATE.registered_buttons if b is not None]
    STATE.registered_buttons = live
    for btn in STATE.registered_buttons:
        if btn is None:
            continue
        # Registered buttons are identified by their registration order
        # (ps1-style), so we don't have a key_map here.  Instead we apply
        # the theme through the app_window._apply_button_styles path which
        # builds the proper key_map from collect_button_groups.


def load_button_styles_from_file(path: str) -> None:
    """``Load-ButtonStylesFromFile`` — merge JSON theme data into BUTTON_STYLES."""
    global ACTIVE_STYLE_NAME
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if key == "__Active__":
            continue
        if not isinstance(value, dict):
            continue
        # Value is {button_key: preset_name} — a theme snapshot.
        BUTTON_STYLES[key] = dict(value)
    active = data.get("__Active__")
    if active and active in BUTTON_STYLES:
        ACTIVE_STYLE_NAME = active


def save_button_styles_to_file(path: str) -> None:
    """``Save-ButtonStylesToFile`` — persist BUTTON_STYLES (+ active name)."""
    out: dict[str, Any] = {}
    for key, assignments in BUTTON_STYLES.items():
        out[key] = dict(assignments)
    out["__Active__"] = ACTIVE_STYLE_NAME
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# § L1 — per-button presets + binding map (port of $global:PerButtonPresets /
# $global:ButtonPresetMap).  A preset is just a ButtonStyle; the map binds a
# stable button key (block_type#local_index) to a preset name.
# ---------------------------------------------------------------------------
PER_BUTTON_PRESETS: dict[str, ButtonStyle] = {}
BUTTON_PRESET_MAP: dict[str, str] = {}
# 直接样式表：当「应用并刷新画布」未选中任何 L1 预设时，把当前预览样式
# 直接按 button_key 记到这里，使「所见即所得」的应用在编辑器关闭、HUD 重建
# 后依然生效（否则只改了内存里的实时按钮，rebuild() 一重建就回到默认样式）。
# 与 BUTTON_PRESET_MAP 互斥：绑定了预设的按钮以预设为准，此处记录被清空。
DIRECT_STYLES: dict[str, ButtonStyle] = {}
# 多选选择集的持久化（按钮风格编辑器里在预览列表选中的按钮 key）。
# 退出编辑器再进入时，应恢复这些按钮的选中态，而不是每次都重新选。
BUTTON_SELECTION: set[str] = set()


def _style_from_editor(geo: dict, colors: dict[str, str]) -> ButtonStyle:
    """Build a ButtonStyle from editor working values (mirrors SavePE)."""
    return ButtonStyle(
        cut_size=int(geo.get("cut_size", 8)),
        corner_radius=int(geo.get("corner_radius", 0)),
        border_thickness=int(geo.get("border_thickness", 3)),
        hover_only_border=bool(geo.get("hover_only_border", False)),
        hover_gradient=bool(geo.get("hover_gradient", False)),
        link_theme=bool(geo.get("link_theme", False)),
        colors={k: v for k, v in colors.items() if v},
    )


def apply_per_button_preset(ctrl: Any, preset_name: str) -> None:
    """Apply a single L1 preset to one button (RoundedButton or QPushButton)."""
    st = PER_BUTTON_PRESETS.get(preset_name)
    if st is None:
        return
    _apply_style_to_ctrl(ctrl, st)


def _apply_style_to_ctrl(ctrl: Any, st: ButtonStyle) -> None:
    """Push a ButtonStyle onto any button control (RoundedButton or QPushButton)."""
    colors = st.colors

    def _color(key: str) -> Color | None:
        hexv = colors.get(key)
        return hex_to_color(hexv) if hexv else None

    if hasattr(ctrl, "set_colors"):
        back = _color("BackColor") or get_color_derivative(COLOR_MAUVE, 0.22)
        hover = _color("HoverColor") or get_color_derivative(COLOR_MAUVE, 0.40)
        border = _color("BorderColor") or COLOR_MAUVE
        fore = _color("ForeColor") or COLOR_MAUVE
        hover_fore = _color("HoverForeColor") or Color.from_rgb(255, 255, 255)
        try:
            ctrl.set_colors(back, hover, border, fore, hover_fore)
        except Exception:
            pass
        if hasattr(ctrl, "cut_size"):
            try:
                ctrl.cut_size = int(st.cut_size or 0)
                # 防御：corner_radius 若被错误存成 None/空，强转 0，避免
                # RoundedButton.paintEvent 里 `None > 0` 抛异常导致整颗
                # 按钮不绘制、看起来像「圆角预设绑定后变矩形/没生效」。
                ctrl.corner_radius = int(st.corner_radius or 0)
                ctrl.border_thickness = int(st.border_thickness or 0)
                ctrl.hover_only_border = st.hover_only_border
                ctrl.hover_gradient = st.hover_gradient
            except Exception:
                pass
        grad_top = _color("GradientHoverTop")
        grad_bot = _color("GradientHoverBottom")
        if grad_top is not None and hasattr(ctrl, "gradient_hover_top"):
            try:
                ctrl.gradient_hover_top = grad_top
                ctrl.gradient_hover_bottom = grad_bot
            except Exception:
                pass
        # 悬浮边框：仅在控件支持时设置（RoundedButton 有该属性）。
        hb = _color("HoverBorderColor")
        if hb is not None and hasattr(ctrl, "hover_border"):
            try:
                ctrl.hover_border = hb
            except Exception:
                pass
        active_back = _color("ActiveBackColor")
        active_border = _color("ActiveBorderColor")
        inactive_back = _color("InactiveBackColor")
        inactive_border = _color("InactiveBorderColor")
        if (active_back and active_back != Color.transparent()) or (
            inactive_back and inactive_back != Color.transparent()
        ):
            if hasattr(ctrl, "set_active_colors"):
                try:
                    ctrl.set_active_colors(active_back, active_border, inactive_back, inactive_border)
                except Exception:
                    pass
        try:
            if hasattr(ctrl, "update"):
                ctrl.update()
        except Exception:
            pass
    else:
        # Native QPushButton: realize the preset through a stylesheet.
        # Mirror the RoundedButton branch's fallbacks (COLOR_MAUVE derivatives)
        # so a *sparse* preset (only hover/gradient colours defined) still
        # renders a styled button that matches the editor's RoundedButton
        # preview.  Without this, empty colours left the button at the OS
        # default grey — looking like "a preset I never picked" and diverging
        # from the preview the user actually selected.
        back = _color("BackColor") or get_color_derivative(COLOR_MAUVE, 0.22)
        hover = _color("HoverColor") or get_color_derivative(COLOR_MAUVE, 0.40)
        border = _color("BorderColor") or COLOR_MAUVE
        fore = _color("ForeColor") or COLOR_MAUVE
        hover_fore = _color("HoverForeColor") or Color.from_rgb(255, 255, 255)
        hover_border = _color("HoverBorderColor")
        bt = int(st.border_thickness or 0)
        # 防御：corner_radius 若被错误存成 None/空，强转 0，保证 border-radius
        # 始终是合法整数（否则 int(None) 抛异常被上层 swallow，按钮不被样式化
        # → 圆角预设绑定后看起来还是矩形）。
        cr = max(0, int(st.corner_radius or 0))
        cut = int(st.cut_size or 0)
        if cr == 0 and cut > 0 and hasattr(ctrl, "_bs_back"):
            # 切角预设：原生 QPushButton 只能圆角、无法做切角，背景/边框改由
            # HintButton 自绘（_paint_chamfer）。样式表此处【背景透明 + 无边框】，
            # 否则 Qt 的 background role 会在 paintEvent 之前用默认按钮面/背景
            # 把整块填满，切角处也被同色填满 → 看起来「切角没生效」。文字色保留（无害）。
            ss = (
                f"QPushButton {{ background: transparent; color: rgb({fore.r},{fore.g},{fore.b});"
                f" border: none; }}"
            )
        else:
            ss = (
                f"QPushButton {{ background: rgb({back.r},{back.g},{back.b});"
                f" color: rgb({fore.r},{fore.g},{fore.b});"
                f" border: {bt}px solid rgb({border.r},{border.g},{border.b});"
                f" border-radius: {cr}px; padding: 4px 10px; }}"
            )
            if hover or hover_fore or hover_border:
                ss += " QPushButton:hover {"
                if hover:
                    ss += f" background: rgb({hover.r},{hover.g},{hover.b});"
                if hover_fore:
                    ss += f" color: rgb({hover_fore.r},{hover_fore.g},{hover_fore.b});"
                if hover_border:
                    ss += f" border: {bt}px solid rgb({hover_border.r},{hover_border.g},{hover_border.b});"
                ss += " }"
        # 把解析出的颜色 + 几何存到按钮上，供 HintButton 在「切角」预设下自绘
        # 使用（原生 QPushButton 只能圆角、无法做切角）。仅对支持这些属性的
        # 按钮（HintButton）生效；普通 QPushButton 忽略。
        if hasattr(ctrl, "_bs_back"):
            ctrl._bs_back = (back.r, back.g, back.b)
            ctrl._bs_hover = (hover.r, hover.g, hover.b)
            ctrl._bs_border = (border.r, border.g, border.b)
            ctrl._bs_fore = (fore.r, fore.g, fore.b)
            ctrl._bs_bt = bt
            ctrl._cut_size = int(st.cut_size or 0)
            ctrl._corner_radius = cr
            # 先清零激活态颜色，避免切换/重建预设时残留旧预设的激活色（只有
            # 当前预设确实定义了 Active*/Inactive* 时才在下面重新写入）。
            ctrl._bs_active_back = None
            ctrl._bs_active_border = None
            ctrl._bs_inactive_back = None
            ctrl._bs_inactive_border = None
            # 激活态/未激活态颜色：预设定义了 ActiveBackColor/InactiveBackColor
            # （且非透明）时写入，供 HintButton.set_style 在按钮激活时改用激活
            # 色；几何（圆角/切角/边框厚度）由上面 _bs_* 保持不变。稀疏预设不
            # 写，按钮回落到「基础色 + 文字表达开关」。
            _ab = _color("ActiveBackColor")
            _abd = _color("ActiveBorderColor")
            _ib = _color("InactiveBackColor")
            _ibd = _color("InactiveBorderColor")
            if _ab is not None and _ab != Color.transparent():
                ctrl._bs_active_back = (_ab.r, _ab.g, _ab.b)
            if _abd is not None and _abd != Color.transparent():
                ctrl._bs_active_border = (_abd.r, _abd.g, _abd.b)
            if _ib is not None and _ib != Color.transparent():
                ctrl._bs_inactive_back = (_ib.r, _ib.g, _ib.b)
            if _ibd is not None and _ibd != Color.transparent():
                ctrl._bs_inactive_border = (_ibd.r, _ibd.g, _ibd.b)
        try:
            ctrl.setStyleSheet(ss)
        except Exception:
            pass
        # 缓存解析出的完整样式表，供 HintButton.set_style 在「状态切换 /
        # 周期刷新」重新套用默认外观时复用——否则 set_style 每次都会用自带
        # 的默认样式表（主题色 + 圆角7px）覆盖掉用户预设（圆角 + 悬浮色），
        # 表现为交互后「预设消失、变方角 + 绑定主题色」。见 gamepad_hint.py
        # 的 HintButton.set_style 预设保护分支。
        if hasattr(ctrl, "_bs_back"):
            try:
                ctrl._preset_ss = ss
                ctrl._preset_applied = True
            except Exception:
                pass


def apply_styles_to_buttons(buttons: list, key_map: dict[int, str] | None = None) -> None:
    """Apply the active theme's saved per-button preset assignments.

    For each button with a matching key in the active theme's assignment map,
    apply its bound L1 preset.  Buttons without an assignment stay at their
    native HUD default — there is no uniform L2 style layer.
    """
    assignments = BUTTON_STYLES.get(ACTIVE_STYLE_NAME, {})
    # Merge in-memory overrides (editor may have bound/unbound without saving).
    merged = dict(assignments)
    merged.update(BUTTON_PRESET_MAP)
    for btn in buttons:
        if btn is None:
            continue
        key = key_map.get(id(btn)) if key_map else None
        preset_name = merged.get(key) if key else None
        if preset_name and preset_name in PER_BUTTON_PRESETS:
            apply_per_button_preset(btn, preset_name)
        elif key and key in DIRECT_STYLES:
            # No preset bound, but a direct (no-preset) apply was persisted —
            # reproduce it on rebuild so the HUD keeps the edited style.
            _apply_style_to_ctrl(btn, DIRECT_STYLES[key])


def bind_button_preset(key: str, preset_name: str) -> None:
    BUTTON_PRESET_MAP[key] = preset_name


def unbind_button_preset(key: str) -> None:
    BUTTON_PRESET_MAP.pop(key, None)


# ---------------------------------------------------------------------------
# Persistence for L1 presets + binding map (buttons/button-presets.json,
# buttons/button-preset-map.json).  JSON errors are swallowed but never
# silently lose data (mirrors ConfigManager last_error_raw behaviour).
# ---------------------------------------------------------------------------
def save_per_button_presets_to_file(path: str) -> None:
    out: dict[str, Any] = {k: v.to_dict() for k, v in PER_BUTTON_PRESETS.items()}
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        pass


def load_per_button_presets_from_file(path: str) -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    PER_BUTTON_PRESETS.clear()
    for k, v in data.items():
        if isinstance(v, dict):
            PER_BUTTON_PRESETS[k] = ButtonStyle.from_dict(v)


def save_button_preset_map_to_file(path: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(dict(BUTTON_PRESET_MAP), fh, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        pass


def load_button_preset_map_from_file(path: str) -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    BUTTON_PRESET_MAP.clear()
    for k, v in data.items():
        BUTTON_PRESET_MAP[str(k)] = str(v)


def button_data_paths(script_root: str) -> dict[str, str]:
    """Resolve the three button-data files under ``<script_root>/buttons``."""
    d = os.path.join(script_root, "buttons")
    return {
        "styles": os.path.join(d, "button-styles.json"),
        "presets": os.path.join(d, "button-presets.json"),
        "map": os.path.join(d, "button-preset-map.json"),
        "selection": os.path.join(d, "button-selection.json"),
        "direct": os.path.join(d, "button-direct-styles.json"),
    }


def load_button_selection_from_file(path: str) -> None:
    """Restore the persisted multi-selection set (button keys)."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:
        return
    if not isinstance(data, list):
        return
    BUTTON_SELECTION.clear()
    for k in data:
        if isinstance(k, str):
            BUTTON_SELECTION.add(k)


def save_button_selection_to_file(path: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(sorted(BUTTON_SELECTION), fh, separators=(",", ":"),
                      ensure_ascii=False)
    except Exception:
        pass


def load_all_button_data(script_root: str) -> None:
    p = button_data_paths(script_root)
    load_button_styles_from_file(p["styles"])
    load_per_button_presets_from_file(p["presets"])
    load_button_preset_map_from_file(p["map"])
    load_button_selection_from_file(p["selection"])
    load_direct_styles_from_file(p["direct"])


def save_all_button_data(script_root: str) -> None:
    p = button_data_paths(script_root)
    os.makedirs(os.path.dirname(p["styles"]), exist_ok=True)
    save_button_styles_to_file(p["styles"])
    save_per_button_presets_to_file(p["presets"])
    save_button_preset_map_to_file(p["map"])
    save_button_selection_to_file(p["selection"])
    save_direct_styles_to_file(p["direct"])


def load_direct_styles_from_file(path: str) -> None:
    """Restore the per-button *direct* apply styles (no-preset applies)."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    DIRECT_STYLES.clear()
    for k, v in data.items():
        if isinstance(v, dict):
            try:
                DIRECT_STYLES[str(k)] = ButtonStyle.from_dict(v)
            except Exception:
                pass


def save_direct_styles_to_file(path: str) -> None:
    out = {k: v.to_dict() for k, v in DIRECT_STYLES.items()}
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        pass
