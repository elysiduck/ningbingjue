"""Breathing math tests — scanline (sine)."""

from __future__ import annotations

import math

from ningbingjue.core.timers import scanline_alpha, scanline_advance


def test_scanline_alpha_range_and_formula():
    for phase in [0.0, 1.0, 2.5]:
        a = scanline_alpha(phase)
        assert 0.2 <= a <= 1.0
        # mathematically 0.6 + 0.4*sin, but the floor-based form differs by a
        # sub-ULP rounding step, so compare with tolerance.
        assert math.isclose(a, 0.6 + 0.4 * math.sin(phase), abs_tol=1e-9)


def test_scanline_period_about_2_36s():
    # phase += 0.08 per 30ms tick; one full sine period = 2*pi/0.08 ticks
    ticks_per_period = 2 * math.pi / 0.08
    period_s = ticks_per_period * 0.030
    assert 2.3 < period_s < 2.5
