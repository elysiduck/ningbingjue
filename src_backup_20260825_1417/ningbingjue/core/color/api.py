"""§3 Palette toolset — unified color derivation API (7 APIs).

Every role-color derivative MUST go through these functions; blocks must never
call ``FromArgb`` directly.  This mirrors the ``colors.ps1`` contract.
"""

from __future__ import annotations

from typing import Any

from .palette import (
    BG_APP,
    BG_CARD,
    COLOR_BLUE,
    COLOR_MAUVE,
    COLOR_PEACH,
    COLOR_RED,
    COLOR_TEAL,
    FG_SECONDARY,
)
from .rgb import Color, _clamp
from .state import STATE


def _is_light_theme() -> bool:
    """Lazy import to avoid a circular import with ``.semantic`` (which imports
    this module).  Light/dark now follows the breathing-light brightness, not a
    fixed theme name."""
    from .semantic import is_light_theme
    return is_light_theme()


def _default_base() -> Color:
    return STATE.bg_card if STATE.bg_card is not None else BG_CARD


def get_color_derivative(
    color: Color | None, ratio: float = 0.30, base: Color | None = None
) -> Color:
    """API 1 — blend ``color`` over ``base`` (default ``BgCard``) by ``ratio``.

    Formula (identical to PS): ``int(base + (color - base) * ratio)`` then clamp.
    """
    if color is None:
        color = Color.gray()
    if base is None or not isinstance(base, Color):
        base = _default_base()
    r = int(base.r + (color.r - base.r) * ratio)
    g = int(base.g + (color.g - base.g) * ratio)
    b = int(base.b + (color.b - base.b) * ratio)
    return Color(255, _clamp(r), _clamp(g), _clamp(b))


def get_button_state(
    role: Color | None = None, style: str = "frosted", light: bool = False
) -> dict[str, Color]:
    """API 2 — 4-state button colors (Back/Hover/Border/Fore)."""
    if _is_light_theme():
        light = True
    if role is None:
        role = COLOR_MAUVE

    if light:
        return {
            "Back": get_color_derivative(role, 0.85, Color.from_rgb(252, 235, 242)),
            "Hover": get_color_derivative(role, 1.00, Color.from_rgb(255, 250, 252)),
            "Border": role,
            "Fore": get_color_derivative(role, 0.30, Color.from_rgb(50, 30, 40)),
        }
    if style == "solid":
        return {
            "Back": get_color_derivative(role, 0.25),
            "Hover": get_color_derivative(role, 0.40),
            "Border": role,
            "Fore": role,
        }
    if style == "frosted":
        return {
            "Back": get_color_derivative(role, 0.22),
            "Hover": get_color_derivative(role, 0.40),
            "Border": role,
            "Fore": role,
        }
    if style == "tinted":
        return {
            "Back": get_color_derivative(role, 0.10),
            "Hover": get_color_derivative(role, 0.25),
            "Border": get_color_derivative(role, 0.60),
            "Fore": get_color_derivative(role, 0.85),
        }
    if style == "muted":
        return {
            "Back": Color.from_rgb(20, 22, 28),
            "Hover": Color.from_rgb(38, 40, 48),
            "Border": FG_SECONDARY,
            "Fore": FG_SECONDARY,
        }
    # default branch
    return get_button_state(role=role, style="frosted", light=light)


def get_slider_colors(role: Color | None = None, light: bool = False) -> dict[str, Color]:
    """API 3 — Track/Fill/Thumb colors in one call."""
    if _is_light_theme():
        light = True
    if role is None:
        role = COLOR_BLUE

    if light:
        return {
            "Track": get_color_derivative(role, 0.35, Color.from_rgb(235, 228, 238)),
            "Fill": get_color_derivative(role, 0.65, Color.from_rgb(240, 235, 245)),
            "ThumbBack": get_color_derivative(role, 0.75, Color.from_rgb(252, 240, 245)),
            "ThumbHover": role,
            "ThumbBorder": get_color_derivative(role, 0.90, Color.from_rgb(255, 255, 255)),
            "ThumbFore": Color.from_rgb(255, 255, 255),
        }
    return {
        "Track": get_color_derivative(role, 0.15),
        "Fill": role,
        "ThumbBack": get_color_derivative(role, 0.35),
        "ThumbHover": get_color_derivative(role, 0.55),
        "ThumbBorder": role,
        "ThumbFore": Color.from_rgb(255, 255, 255),
    }


def get_theme_accent_fallback(
    accent: dict[str, Any] | None,
    accent_key: str,
    fallback: Color | None = None,
    fallback_rgb: tuple[int, int, int] = (175, 130, 235),
) -> Color:
    """API 4 — fallback when a theme accent key is missing."""
    if accent and accent.get(accent_key) is not None:
        return accent[accent_key]
    if fallback is not None and isinstance(fallback, Color):
        return fallback
    return Color.from_rgb(*fallback_rgb)


def get_freeze_highlight_colors(light: bool = False) -> dict[str, Color]:
    """API 5 — freeze highlight fill colors."""
    if _is_light_theme():
        light = True
    if light:
        return {
            "Candidate": Color.from_rgb(255, 215, 175),
            "Frozen": Color.from_rgb(195, 220, 245),
        }
    return {
        "Candidate": get_color_derivative(COLOR_PEACH, 0.40),
        "Frozen": get_color_derivative(COLOR_BLUE, 0.40),
    }


def get_page_token_colors() -> dict[str, Color]:
    """API 6 — exit button (red semantic)."""
    return {
        "Back": get_color_derivative(COLOR_RED, 0.15),
        "Hover": get_color_derivative(COLOR_RED, 0.30),
        "Border": COLOR_RED,
        "Fore": COLOR_RED,
    }


def get_light_theme_fill(light_rgb: tuple[int, int, int], dark_rgb: tuple[int, int, int]) -> Color:
    """API 7 — conditional fill that switches on the light theme."""
    is_light = _is_light_theme()
    rgb = light_rgb if is_light else dark_rgb
    return Color.from_rgb(*rgb)


def get_adaptive_highlight(base: Color) -> Color:
    """Aux — darken bright colors, lighten dark colors (±30 / ±28)."""
    total = base.r + base.g + base.b
    if total >= 380:
        return Color(base.a, max(0, base.r - 30), max(0, base.g - 30), max(0, base.b - 30))
    return Color(base.a, min(255, base.r + 28), min(255, base.g + 28), min(255, base.b + 28))


def get_tile_grid_bg() -> Color:
    """Aux — tile preview grid background, always slightly darker than BgApp."""
    bg = BG_APP
    total = bg.r + bg.g + bg.b
    if total >= 380:
        return Color(bg.a, max(0, bg.r - 35), max(0, bg.g - 35), max(0, bg.b - 35))
    return Color(bg.a, max(0, bg.r - 2), max(0, bg.g - 2), min(255, bg.b + 3))
