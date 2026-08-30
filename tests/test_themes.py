"""Theme system tests — 10 themes, accent application, freeze-highlight rule."""

from __future__ import annotations

from ningbingjue.core.color.palette import COLOR_MAUVE, FG_SECONDARY
from ningbingjue.core.color.rgb import Color
from ningbingjue.core.color.state import STATE, apply_palette_overrides, reset_state
from ningbingjue.core.color.themes import (
    THEME_NAMES,
    apply_theme,
    get_theme_accent_colors,
)
from ningbingjue.core.config import ThemeConfig


def test_ten_themes_exist():
    assert len(THEME_NAMES) == 10
    assert "少女粉" in THEME_NAMES
    assert "原夜" in THEME_NAMES


def test_base_accent_present_in_every_theme():
    for name in THEME_NAMES:
        accent = get_theme_accent_colors(name)
        assert accent["RtssTitleColor"] is not None
        assert accent["ProcMonFocusBorder"] is not None


def test_specific_theme_overrides():
    yuan = get_theme_accent_colors("原夜")
    assert yuan["TileTitleColor"] == Color.from_rgb(175, 195, 235)
    sha = get_theme_accent_colors("少女粉")
    assert sha["ThemeTitleColor"] == Color.from_rgb(210, 70, 120)
    assert sha.get("TileShowBorder") is False


def test_apply_theme_sets_state_and_freeze_highlight():
    apply_theme("少女粉")
    assert STATE.active_theme_name == "少女粉"
    # Update-FreezeHighlightColor overrides the theme-dict value:
    assert STATE.freeze_highlight_color == Color.from_argb(255, 255, 180, 160)
    apply_theme("樱粉")
    assert STATE.freeze_highlight_color == Color.from_argb(255, 255, 130, 165)
    apply_theme("原夜")
    assert STATE.freeze_highlight_color == Color.from_argb(255, 175, 90, 70)


def test_apply_palette_overrides_from_theme_config():
    # Regression: ThemeConfig has no fg_secondary field, yet apply_palette_overrides
    # must not crash and should fall back to the FG_SECONDARY constant.
    reset_state()
    theme = ThemeConfig(bg_app="16,16,16", bg_card="28,30,38", fg_primary="225,225,225")
    apply_palette_overrides(theme)
    assert STATE.bg_app == Color.from_rgb(16, 16, 16)
    assert STATE.bg_card == Color.from_rgb(28, 30, 38)
    assert STATE.fg_primary == Color.from_rgb(225, 225, 225)
    assert STATE.fg_secondary == FG_SECONDARY  # missing field -> default constant
    reset_state()
