"""§4 Theme accent template system — 10 complete themes.

``get_theme_accent_colors(name)`` returns a *fresh* dict each call (no global
mutation).  ``apply_theme(name)`` updates the shared :data:`STATE` and, exactly
like the original ``Update-FreezeHighlightColor``, overrides the freeze-highlight
color based on the active theme name (this intentionally wins over any
``FreezeHighlightColor`` key present in the theme dict — matching runtime).
"""

from __future__ import annotations

from typing import Any

from .palette import (
    COLOR_BLUE,
    COLOR_CYAN,
    COLOR_MAUVE,
    COLOR_PEACH,
    COLOR_RED,
    COLOR_YELLOW,
)
from .rgb import Color
from .semantic import is_light_theme
from .state import STATE

# Switch order of the original ``Get-ThemeAccentColors``.
THEME_NAMES: list[str] = [
    "樱粉", "少女粉", "原夜", "暗金", "暮紫",
    "深海", "幽林", "碳黑", "琥珀", "墨蓝",
]

LIGHT_THEME = "少女粉"


def _base_accent() -> dict[str, Any]:
    """Lines 335-363 of colors.ps1 — the starting accent dict for every theme."""
    return {
        "RtssTitleColor": COLOR_BLUE,
        "FreezeTitleColor": COLOR_PEACH,
        "ThemeTitleColor": COLOR_MAUVE,
        "LSTitleColor": COLOR_YELLOW,
        "TileTitleColor": COLOR_MAUVE,
        "ExitBtnBack": Color.from_rgb(48, 22, 28),
        "ExitBtnHover": Color.from_rgb(78, 32, 38),
        "ExitBtnBorder": COLOR_RED,
        "ExitBtnFore": COLOR_RED,
        "SlashGlowColor": Color.from_argb(35, 230, 255, 30),
        "SlashMainBaseR": 110, "SlashMainBaseG": 110, "SlashMainBaseB": 120,
        "SlashMainPeakR": 230, "SlashMainPeakG": 255, "SlashMainPeakB": 30,
        "SlashHintColor": Color.from_rgb(230, 255, 30),
        "EditApplyTheme": "Green",
        "EditCancelTheme": "Red",
        "EditNewPageTheme": "Mauve",
        "LsBtnLoadTheme": "Green",
        "LsBtnSaveTheme": "Yellow",
        "LsBtnDeleteTheme": "Red",
        "EditTitleColor": COLOR_YELLOW,
        "RtssFocusBorder": COLOR_BLUE,
        "FreezeFocusBorder": COLOR_PEACH,
        "ThemeFocusBorder": COLOR_MAUVE,
        "LSFocusBorder": COLOR_YELLOW,
        "TileFocusBorder": COLOR_MAUVE,
        "ProcMonTitleColor": COLOR_CYAN,
        "ProcMonFocusBorder": COLOR_CYAN,
    }


def get_theme_accent_colors(theme_name: str) -> dict[str, Any]:
    """Return the full accent dict for ``theme_name`` (defaults to base if unknown)."""
    accent = _base_accent()
    name = theme_name if theme_name in THEME_NAMES else "原夜"

    if name == "樱粉":
        accent.update({
            "RtssTitleColor": Color.from_rgb(255, 130, 110),
            "FreezeTitleColor": Color.from_rgb(255, 155, 80),
            "ThemeTitleColor": Color.from_rgb(240, 160, 210),
            "LSTitleColor": Color.from_rgb(255, 195, 100),
            "TileTitleColor": Color.from_rgb(235, 155, 205),
            "ExitBtnBack": Color.from_rgb(55, 30, 42),
            "ExitBtnHover": Color.from_rgb(80, 40, 58),
            "ExitBtnFore": Color.from_rgb(255, 190, 210),
            "SlashGlowColor": Color.from_argb(30, 255, 150, 180),
            "SlashMainBaseR": 130, "SlashMainBaseG": 60, "SlashMainBaseB": 80,
            "SlashMainPeakR": 255, "SlashMainPeakG": 160, "SlashMainPeakB": 200,
            "SlashHintColor": Color.from_rgb(255, 180, 210),
            "EditApplyTheme": "Teal", "EditCancelTheme": "Peach", "EditNewPageTheme": "Mauve",
            "EditTitleColor": Color.from_rgb(255, 180, 210),
            "RtssFocusBorder": Color.from_rgb(255, 130, 110),
            "FreezeFocusBorder": Color.from_rgb(255, 155, 80),
            "ThemeFocusBorder": Color.from_rgb(240, 160, 210),
            "LSFocusBorder": Color.from_rgb(255, 195, 100),
            "TileFocusBorder": Color.from_rgb(235, 155, 205),
            "ProcMonTitleColor": Color.from_rgb(120, 195, 215),
            "ProcMonFocusBorder": Color.from_rgb(120, 195, 215),
            "FreezeHighlightColor": Color.from_argb(255, 255, 120, 155),
            "FreezeHighlightLineColor": Color.from_argb(255, 255, 175, 195),
        })
    elif name == "少女粉":
        accent.update({
            "RtssTitleColor": Color.from_rgb(220, 100, 90),
            "FreezeTitleColor": Color.from_rgb(220, 110, 80),
            "ThemeTitleColor": Color.from_rgb(210, 70, 120),
            "LSTitleColor": Color.from_rgb(215, 105, 95),
            "TileTitleColor": Color.from_rgb(195, 95, 145),
            "FgSecondary": Color.from_rgb(185, 100, 120),
            "ExitBtnBack": Color.from_rgb(252, 198, 215),
            "ExitBtnHover": Color.from_rgb(255, 178, 200),
            "ExitBtnBorder": Color.from_rgb(240, 140, 160),
            "ExitBtnFore": Color.from_rgb(168, 38, 68),
            "SlashGlowColor": Color.from_argb(35, 255, 170, 200),
            "SlashMainBaseR": 220, "SlashMainBaseG": 150, "SlashMainBaseB": 170,
            "SlashMainPeakR": 255, "SlashMainPeakG": 120, "SlashMainPeakB": 160,
            "SlashHintColor": Color.from_rgb(230, 100, 140),
            "EditApplyTheme": "Teal", "EditCancelTheme": "Peach", "EditNewPageTheme": "Yellow",
            "EditTitleColor": Color.from_rgb(190, 60, 105),
            "LsBtnLoadTheme": "Green", "LsBtnSaveTheme": "Yellow", "LsBtnDeleteTheme": "Red",
            "FreezeBtnTheme": "Peach", "ResumeBtnTheme": "Teal",
            "EditRowBtnBack": Color.from_rgb(250, 188, 208),
            "EditRowBtnHover": Color.from_rgb(255, 210, 228),
            "EditRowBtnBorder": Color.from_rgb(235, 165, 185),
            "EditRowBtnFore": Color.from_rgb(152, 48, 72),
            "PageMoveBtnBack": Color.from_rgb(248, 175, 150),
            "PageMoveBtnHover": Color.from_rgb(255, 198, 175),
            "PageMoveBtnBorder": Color.from_rgb(228, 148, 130),
            "PageMoveBtnFore": Color.from_rgb(158, 62, 42),
            "PageSepColor": Color.from_rgb(220, 160, 180),
            "TileBgTransparent": True, "TileShowBorder": False,
            "RtssFocusBorder": Color.from_rgb(220, 100, 90),
            "FreezeFocusBorder": Color.from_rgb(220, 110, 80),
            "ThemeFocusBorder": Color.from_rgb(210, 70, 120),
            "LSFocusBorder": Color.from_rgb(215, 105, 95),
            "TileFocusBorder": Color.from_rgb(195, 95, 145),
            "ProcMonTitleColor": Color.from_rgb(92, 172, 192),
            "ProcMonFocusBorder": Color.from_rgb(92, 172, 192),
            "DecorSeed": 42,
            "GamepadFocusColor": Color.from_rgb(248, 120, 155),
            "EditColorMenuHighlight": Color.from_rgb(238, 180, 198),
            "EditColorMenuNormal": Color.from_rgb(252, 235, 242),
            "EditColorMenuBg": Color.from_rgb(252, 240, 245),
            "FreezeHighlightColor": Color.from_argb(255, 255, 138, 112),
            "FreezeHighlightLineColor": Color.from_argb(255, 242, 88, 68),
        })
    elif name == "原夜":
        accent.update({
            "TileTitleColor": Color.from_rgb(175, 195, 235),
            "TileFocusBorder": Color.from_rgb(175, 195, 235),
            "ProcMonTitleColor": Color.from_rgb(110, 215, 230),
            "ProcMonFocusBorder": Color.from_rgb(110, 215, 230),
            "FreezeHighlightColor": Color.from_argb(255, 255, 135, 40),
            "FreezeHighlightLineColor": Color.from_argb(255, 255, 210, 100),
        })
    elif name == "暗金":
        accent.update({
            "ThemeTitleColor": Color.from_rgb(232, 198, 112),
            "TileTitleColor": Color.from_rgb(232, 198, 112),
            "TileFocusBorder": Color.from_rgb(232, 198, 112),
            "ProcMonTitleColor": Color.from_rgb(100, 210, 218),
            "ProcMonFocusBorder": Color.from_rgb(100, 210, 218),
            "FreezeHighlightColor": Color.from_argb(255, 255, 195, 45),
            "FreezeHighlightLineColor": Color.from_argb(255, 255, 228, 130),
        })
    elif name == "暮紫":
        accent.update({
            "ThemeTitleColor": Color.from_rgb(205, 175, 232),
            "TileTitleColor": Color.from_rgb(205, 175, 232),
            "TileFocusBorder": Color.from_rgb(205, 175, 232),
            "ProcMonTitleColor": Color.from_rgb(120, 205, 220),
            "ProcMonFocusBorder": Color.from_rgb(120, 205, 220),
            "FreezeHighlightColor": Color.from_argb(255, 195, 120, 240),
            "FreezeHighlightLineColor": Color.from_argb(255, 228, 178, 252),
        })
    elif name == "深海":
        accent.update({
            "ThemeTitleColor": Color.from_rgb(95, 205, 235),
            "TileTitleColor": Color.from_rgb(148, 202, 235),
            "TileFocusBorder": Color.from_rgb(148, 202, 235),
            "ProcMonTitleColor": Color.from_rgb(80, 195, 215),
            "ProcMonFocusBorder": Color.from_rgb(80, 195, 215),
            "FreezeHighlightColor": Color.from_argb(255, 45, 205, 225),
            "FreezeHighlightLineColor": Color.from_argb(255, 130, 232, 248),
        })
    elif name == "幽林":
        accent.update({
            "ThemeTitleColor": Color.from_rgb(130, 225, 105),
            "TileTitleColor": Color.from_rgb(165, 228, 188),
            "TileFocusBorder": Color.from_rgb(165, 228, 188),
            "ProcMonTitleColor": Color.from_rgb(105, 220, 210),
            "ProcMonFocusBorder": Color.from_rgb(105, 220, 210),
            "FreezeHighlightColor": Color.from_argb(255, 115, 235, 70),
            "FreezeHighlightLineColor": Color.from_argb(255, 170, 248, 135),
        })
    elif name == "碳黑":
        accent.update({
            "ThemeTitleColor": Color.from_rgb(210, 210, 225),
            "TileTitleColor": Color.from_rgb(198, 198, 208),
            "TileFocusBorder": Color.from_rgb(198, 198, 208),
            "ProcMonTitleColor": Color.from_rgb(130, 215, 225),
            "ProcMonFocusBorder": Color.from_rgb(130, 215, 225),
            "FreezeHighlightColor": Color.from_argb(255, 175, 178, 200),
            "FreezeHighlightLineColor": Color.from_argb(255, 215, 218, 235),
        })
    elif name == "琥珀":
        accent.update({
            "ThemeTitleColor": Color.from_rgb(255, 170, 52),
            "TileTitleColor": Color.from_rgb(238, 188, 118),
            "TileFocusBorder": Color.from_rgb(238, 188, 118),
            "ProcMonTitleColor": Color.from_rgb(115, 205, 215),
            "ProcMonFocusBorder": Color.from_rgb(115, 205, 215),
            "FreezeHighlightColor": Color.from_argb(255, 255, 165, 35),
            "FreezeHighlightLineColor": Color.from_argb(255, 255, 215, 110),
        })
    elif name == "墨蓝":
        accent.update({
            "ThemeTitleColor": Color.from_rgb(105, 170, 225),
            "TileTitleColor": Color.from_rgb(168, 198, 232),
            "TileFocusBorder": Color.from_rgb(168, 198, 232),
            "ProcMonTitleColor": Color.from_rgb(105, 200, 218),
            "ProcMonFocusBorder": Color.from_rgb(105, 200, 218),
            "FreezeHighlightColor": Color.from_argb(255, 115, 178, 242),
            "FreezeHighlightLineColor": Color.from_argb(255, 175, 215, 252),
        })
    return accent




def update_freeze_highlight_color() -> None:
    """``Update-FreezeHighlightColor`` — now follows the breathing colour.

    The breathing colour *is* the canonical theme colour, so the freeze
    highlight tracks it instead of a fixed theme name.
    """
    from .semantic import get_theme_accent
    STATE.freeze_highlight_color = get_theme_accent()


def apply_theme(name: str) -> dict[str, Any]:
    """Activate a theme: set state, compute accent, refresh freeze highlight."""
    STATE.active_theme_name = name
    accent = get_theme_accent_colors(name)
    STATE.theme_accent_colors = accent
    update_freeze_highlight_color()
    return accent
