"""Regression: focus overlay opacity = 0 must survive editor load + preview.

The buggy ``int(getattr(cfg, "focus_opacity", 25) or 25)`` swallowed the
*legitimate* value 0 (falsy) and replaced it with the default 25 — so the
editor's opacity slider (and the preview selection frame) displayed 25 even
though the saved value was 0.  ``_coerce_focus_opacity`` only falls back for a
genuinely missing / ``None`` / empty value, preserving 0.  ``build_focus_style``
then renders 0% as a transparent mask (``0.000`` alpha), not ``0.250``.
"""
from __future__ import annotations

import pytest

from ningbingjue.ui.button_edit import _coerce_focus_opacity
from ningbingjue.ui.interaction import build_focus_style


@pytest.mark.parametrize("raw,expected", [
    (0, 0),
    (0.0, 0),
    ("0", 0),
    ("50", 50),
    (70, 70),
    (100, 100),
    (150, 100),       # clamp high
    (-5, 0),          # clamp low
    (None, 25),       # genuinely missing -> default
    ("", 25),
    ("abc", 25),      # garbage -> default
])
def test_coerce_focus_opacity_keeps_zero(raw, expected):
    assert _coerce_focus_opacity(raw) == expected


def test_coerce_focus_opacity_custom_default():
    assert _coerce_focus_opacity(None, default=25) == 25


def test_build_focus_style_zero_alpha():
    ss = build_focus_style("e6ff1e", 0)
    assert "0.000" in ss, f"expected 0 alpha, got {ss!r}"
    assert "0.250" not in ss


def test_build_focus_style_25_alpha():
    ss = build_focus_style("e6ff1e", 25)
    assert "0.250" in ss
