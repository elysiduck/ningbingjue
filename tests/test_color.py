"""Color system unit tests — verify derivation math matches the original PS."""

from __future__ import annotations

import pytest

from ningbingjue.core.color.api import (
    get_button_state,
    get_color_derivative,
    get_slider_colors,
    get_theme_accent_fallback,
)
from ningbingjue.core.color.palette import BG_CARD, COLOR_BLUE, COLOR_GREEN, COLOR_MAUVE
from ningbingjue.core.color.rgb import Color, color_to_hex, hex_to_color
from ningbingjue.core.color.semantic import get_gamepad_button_colors, get_theme_color


def test_color_hex_roundtrip():
    c = Color.from_rgb(230, 255, 30)
    assert c.to_hex() == "E6FF1E"
    assert hex_to_color("E6FF1E") == c
    assert hex_to_color("") == Color.transparent()
    assert color_to_hex(None) == "00000000"


def test_color_derivative_math():
    # BG_CARD=(28,30,38), COLOR_BLUE=(95,175,235), ratio 0.22
    out = get_color_derivative(COLOR_BLUE, 0.22, BG_CARD)
    assert out == Color.from_rgb(42, 61, 81)


def test_color_derivative_clamps():
    # negative ratio would clamp channels to >=0
    out = get_color_derivative(COLOR_BLUE, -5.0, BG_CARD)
    assert out.r >= 0 and out.g >= 0 and out.b >= 0


def test_button_state_styles():
    muted = get_button_state(style="muted")
    assert muted["Back"] == Color.from_rgb(20, 22, 28)
    assert muted["Fore"] == muted["Border"]

    frosted = get_button_state(role=COLOR_BLUE, style="frosted")
    solid = get_button_state(role=COLOR_BLUE, style="solid")
    # solid Back uses 0.25, frosted 0.22
    assert solid["Back"] != frosted["Back"]


def test_slider_colors():
    sc = get_slider_colors(COLOR_BLUE)
    assert sc["Fill"] == COLOR_BLUE
    assert sc["ThumbFore"] == Color.from_rgb(255, 255, 255)


def test_theme_accent_fallback():
    accent = {"RtssTitleColor": COLOR_BLUE}
    assert get_theme_accent_fallback(accent, "RtssTitleColor") == COLOR_BLUE
    # missing key -> fallback color
    assert get_theme_accent_fallback(accent, "Nope", fallback=COLOR_MAUVE) == COLOR_MAUVE


def test_semantic_gamepad_a_matches_button_colors():
    a = get_theme_color("Gamepad.A")
    green = get_gamepad_button_colors("Green")
    assert a["Back"] == green["Back"]


def test_semantic_action_confirm_is_green_solid():
    confirm = get_theme_color("Action.Confirm")
    assert confirm["Back"] == get_button_state(role=COLOR_GREEN, style="solid")["Back"]


def test_semantic_slider_uses_role():
    sl = get_theme_color("Slider", role=COLOR_BLUE)
    assert sl["Fill"] == COLOR_BLUE
