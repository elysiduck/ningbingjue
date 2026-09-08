"""Color subsystem public API.

Importing this package has no GUI side-effects; all functions are pure Python
(or operate on the shared :data:`core.color.state.STATE`).
"""

from __future__ import annotations

from .api import (
    get_adaptive_highlight,
    get_button_state,
    get_color_derivative,
    get_freeze_highlight_colors,
    get_light_theme_fill,
    get_page_token_colors,
    get_slider_colors,
    get_theme_accent_fallback,
    get_tile_grid_bg,
)
from .palette import (
    BG_APP,
    BG_CARD,
    COLOR_BLUE,
    COLOR_CYAN,
    COLOR_GREEN,
    COLOR_MAUVE,
    COLOR_ORANGE,
    COLOR_PEACH,
    COLOR_RED,
    COLOR_TEAL,
    COLOR_YELLOW,
    FG_PRIMARY,
    FG_SECONDARY,
)
from .rgb import Color, color_to_hex, hex_to_color
from .semantic import (
    DEFAULT_ACCENT,
    get_gamepad_button_colors,
    get_gamepad_hint_colors,
    get_theme_accent,
    get_theme_color,
    is_light_theme,
    set_theme_accent,
)
from .state import STATE, reset_state, set_active_theme
from .themes import (
    LIGHT_THEME,
    THEME_NAMES,
    apply_theme,
    get_theme_accent_colors,
    update_freeze_highlight_color,
)

__all__ = [
    "Color", "color_to_hex", "hex_to_color",
    "BG_APP", "BG_CARD", "FG_PRIMARY", "FG_SECONDARY",
    "COLOR_BLUE", "COLOR_GREEN", "COLOR_RED", "COLOR_PEACH", "COLOR_MAUVE",
    "COLOR_TEAL", "COLOR_YELLOW", "COLOR_ORANGE", "COLOR_CYAN",
    "get_color_derivative", "get_button_state", "get_slider_colors",
    "get_theme_accent_fallback", "get_freeze_highlight_colors",
    "get_page_token_colors", "get_light_theme_fill",
    "get_adaptive_highlight", "get_tile_grid_bg",
    "get_theme_color", "get_gamepad_button_colors", "get_gamepad_hint_colors",
    "get_theme_accent", "set_theme_accent", "DEFAULT_ACCENT", "is_light_theme",
    "THEME_NAMES", "LIGHT_THEME", "apply_theme", "get_theme_accent_colors",
    "update_freeze_highlight_color",
    "STATE", "reset_state", "set_active_theme",
]
