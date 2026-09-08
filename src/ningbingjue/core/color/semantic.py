"""§7 Semantic color API — the unified entry point blocks should use.

``get_theme_color`` returns ``{Back, Hover, Border, Fore}`` (and a few special
shapes).  ``get_gamepad_hint_colors`` returns the A/B/X/Y colors.
"""

from __future__ import annotations

from typing import Any

from .api import (
    get_button_state,
    get_light_theme_fill,
    get_slider_colors,
)
from .palette import (
    COLOR_BLUE,
    COLOR_GREEN,
    COLOR_MAUVE,
    COLOR_RED,
    COLOR_YELLOW,
)
from .rgb import Color
from .state import STATE


def get_gamepad_button_colors(palette_color_name: str) -> dict[str, Color]:
    """``Get-GamepadButtonColors`` — A/B/X/Y badge colors per palette name."""
    is_light = is_light_theme()

    if is_light:
        table = {
            "Teal": ((165, 225, 190), (185, 238, 205), (140, 205, 168), (60, 100, 70)),
            "Peach": ((245, 168, 182), (252, 190, 202), (228, 145, 162), (160, 48, 65)),
            "Mauve": ((225, 175, 232), (240, 198, 245), (205, 148, 218), (130, 50, 145)),
            "Yellow": ((245, 158, 142), (252, 182, 168), (222, 132, 122), (165, 55, 40)),
            "Blue": ((165, 205, 240), (185, 222, 250), (138, 182, 225), (45, 80, 140)),
            "Red": ((240, 168, 178), (250, 190, 198), (222, 145, 155), (160, 40, 55)),
            "Green": ((172, 222, 175), (195, 235, 198), (148, 200, 152), (50, 105, 55)),
        }
        if palette_color_name in table:
            back, hover, border, fore = table[palette_color_name]
            return {
                "Back": Color.from_rgb(*back),
                "Hover": Color.from_rgb(*hover),
                "Border": Color.from_rgb(*border),
                "Fore": Color.from_rgb(*fore),
            }
        # fall back to Mauve
        back, hover, border, fore = table["Mauve"]
        return {
            "Back": Color.from_rgb(*back), "Hover": Color.from_rgb(*hover),
            "Border": Color.from_rgb(*border), "Fore": Color.from_rgb(*fore),
        }

    accent = STATE.theme_accent_colors if STATE.theme_accent_colors else None
    role_map = {
        "Teal": "RtssTitleColor", "Peach": "FreezeTitleColor", "Mauve": "ThemeTitleColor",
        "Yellow": "LSTitleColor", "Blue": "RtssTitleColor",
    }
    if palette_color_name in role_map:
        key = role_map[palette_color_name]
        role = accent.get(key) if accent else None
        if role is None:
            role = {
                "Teal": (45, 215, 175), "Peach": (255, 130, 40), "Mauve": COLOR_MAUVE,
                "Yellow": COLOR_YELLOW, "Blue": COLOR_BLUE,
            }[palette_color_name]
            role = Color(*role) if isinstance(role, tuple) else role
    elif palette_color_name == "Green":
        role = COLOR_GREEN
    elif palette_color_name == "Red":
        role = COLOR_RED
    elif palette_color_name == "Orange":
        role = (255, 150, 45)
        role = Color(*role)
    else:
        role = COLOR_MAUVE

    # hover = role * 0.55 (clamped to >=40)
    hover = Color.from_rgb(
        min(255, max(40, int(role.r * 0.55))),
        min(255, max(40, int(role.g * 0.55))),
        min(255, max(40, int(role.b * 0.55))),
    )
    if palette_color_name == "Red":
        hover = Color.from_rgb(90, 30, 35)
    back = Color.from_rgb(
        min(255, max(10, int(BG_CARD_LIKE.r + role.r * 0.22))),
        min(255, max(10, int(BG_CARD_LIKE.g + role.g * 0.22))),
        min(255, max(10, int(BG_CARD_LIKE.b + role.b * 0.22))),
    )
    return {"Back": back, "Hover": hover, "Border": role, "Fore": role}


# BgCard used for the back-color computation (mirrors $global:BgCard in PS).
BG_CARD_LIKE = Color.from_rgb(28, 30, 38)


def get_theme_color(
    semantic: str, role: Color | None = None, style: str = "frosted"
) -> dict[str, Any]:
    """``Get-ThemeColor`` — semantic-name -> colors. Returns dicts of Color."""
    if semantic == "Action.Confirm":
        return get_button_state(role=COLOR_GREEN, style="solid")
    if semantic == "Action.Cancel":
        return get_button_state(role=COLOR_RED, style="tinted")
    if semantic == "Action.Delete":
        return get_button_state(role=COLOR_RED, style="tinted")
    if semantic == "Action.Primary":
        if role is None:
            role = COLOR_MAUVE
        return get_button_state(role=role, style=style)
    if semantic == "Action.Muted":
        return get_button_state(style="muted")
    if semantic == "Status.Active":
        return get_button_state(role=COLOR_GREEN, style="frosted")
    if semantic == "Status.Inactive":
        return get_button_state(style="muted")
    if semantic == "Status.Warning":
        return get_button_state(role=COLOR_YELLOW, style="frosted")
    if semantic == "Gamepad.A":
        return get_gamepad_button_colors("Green")
    if semantic == "Gamepad.B":
        return get_gamepad_button_colors("Red")
    if semantic == "Gamepad.X":
        return get_gamepad_button_colors("Blue")
    if semantic == "Gamepad.Y":
        return get_gamepad_button_colors("Yellow")
    if semantic == "Toggle.On":
        return get_button_state(role=COLOR_GREEN, style="tinted")
    if semantic == "Toggle.Off":
        return get_button_state(role=COLOR_RED, style="tinted")
    if semantic == "Slider":
        if role is None:
            role = COLOR_BLUE
        return get_slider_colors(role)
    if semantic == "Input.Field":
        return {
            "Back": get_light_theme_fill([252, 240, 245], [18, 20, 26]),
            "Fore": get_light_theme_fill([170, 50, 85], [255, 255, 255]),
        }
    if semantic == "Card.Border":
        return get_light_theme_fill([210, 185, 200], [50, 48, 62])
    if semantic == "Menu.Dropdown":
        return get_light_theme_fill([248, 235, 242], [38, 38, 45])
    if semantic == "Ghost.Drag":
        return get_light_theme_fill([245, 220, 235], [55, 42, 65])
    if semantic == "Text.Placeholder":
        return get_light_theme_fill([180, 150, 165], [80, 85, 100])
    if semantic == "Text.HelpBar":
        return get_light_theme_fill([170, 140, 155], [100, 110, 130])
    if semantic == "Combo.Background":
        return get_light_theme_fill([252, 240, 245], [18, 20, 26])
    # default: treat as palette color name
    return get_gamepad_button_colors(semantic)


def get_gamepad_hint_colors() -> dict[str, Color]:
    """``Get-GamepadHintColors`` — A=green B=red X=blue Y=yellow."""
    return {
        "A": COLOR_GREEN,
        "B": COLOR_RED,
        "X": COLOR_BLUE,
        "Y": COLOR_YELLOW,
    }


# -- canonical theme accent -----------------------------------------------
# The breathing light colour *is* the app's single theme color.  Everything
# that wants "the theme color" (window neon border, breathing glow, tray
# icon, future consumers) reads get_theme_accent() instead of hardcoding a
# colour.  This keeps accent adjustment to one knob (呼吸灯自定义).
DEFAULT_ACCENT = Color.from_rgb(127, 209, 255)  # neon cyan, matches original look


def set_theme_accent(hex_or_color: str | Color | None) -> None:
    """Set the canonical theme accent from the breathing-light colour.

    Accepts a ``"rrggbb"`` / ``"#rrggbb"`` hex string, a :class:`Color`, or
    ``None`` to clear (falls back to :data:`DEFAULT_ACCENT`).  Invalid/empty
    input clears the override rather than raising.
    """
    if hex_or_color is None:
        STATE.theme_accent = None
        return
    if isinstance(hex_or_color, Color):
        STATE.theme_accent = hex_or_color
        return
    c = Color.from_hex(hex_or_color)
    STATE.theme_accent = None if c.is_transparent() else c


def get_theme_accent() -> Color:
    """Return the canonical theme color (the breathing-light colour).

    Falls back to :data:`DEFAULT_ACCENT` when no custom colour is set.  This is
    the public interface future code should call to read "the theme color".
    """
    return STATE.theme_accent if STATE.theme_accent is not None else DEFAULT_ACCENT


# Relative-luminance threshold above which the breathing colour is treated as a
# "light" colour and the HUD flips to its light surface set.  Tuned so the
# default neon-cyan accent stays on the dark surface (matches the original look)
# — only genuinely light breathing colours trigger light mode.
LIGHT_MODE_LUMINANCE_THRESHOLD = 0.72


def is_light_theme() -> bool:
    """Auto light/dark: derived from the breathing-light (theme-accent) colour.

    The breathing colour *is* the canonical theme colour, so light mode follows
    its brightness rather than a fixed theme name.  Previously this was hard-wired
    to the ``少女粉`` theme name; now any bright accent (e.g. a near-white or
    pastel breathing colour) flips the HUD to its light surfaces.
    """
    c = get_theme_accent()

    def _lin(v: float) -> float:
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    lum = 0.2126 * _lin(c.r) + 0.7152 * _lin(c.g) + 0.0722 * _lin(c.b)
    return lum > LIGHT_MODE_LUMINANCE_THRESHOLD
