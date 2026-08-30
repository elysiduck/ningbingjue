"""BreathingController + breathing-config tests (no GUI / no threads)."""

from __future__ import annotations

import math

from ningbingjue.core.config import AppConfig, breath_speed_to_period
from ningbingjue.ui.breathing import BreathingController


def test_default_controller_reproduces_original_curve():
    bc = BreathingController()
    # Default period ~2.36 s => phase step ~0.08 per 30 ms tick (the original
    # fixed step).  Within 1e-3 of 0.08 — the tiny delta is invisible.
    assert abs(bc._step - 0.08) < 1e-3
    for phase in [0.0, 1.1, 3.0]:
        assert math.isclose(bc.scanline_sample(phase),
                            0.6 + 0.4 * math.sin(phase), abs_tol=1e-9)


def test_set_period_changes_step():
    bc = BreathingController()
    bc.set_period(4.72)  # double the default period => half the step
    assert abs(bc._step - 0.04) < 1e-3
    bc.set_period(1.18)  # half the default period => double the step
    assert abs(bc._step - 0.16) < 1e-3


def test_max_brightness_scales_peak_keeps_floor():
    # peak = floor + (max - floor); trough stays at the fixed floor 0.2
    assert abs(BreathingController.scanline_sample(math.pi / 2, 0.5) - 0.5) < 1e-9
    assert abs(BreathingController.scanline_sample(-math.pi / 2, 0.5) - 0.2) < 1e-9
    # default max 1.0 keeps the original 0.6 + 0.4*sin curve
    assert abs(BreathingController.scanline_sample(math.pi / 2, 1.0) - 1.0) < 1e-9


def test_scan_tick_emits_scaled_alpha_range():
    bc = BreathingController()
    bc.set_max_brightness(0.5)
    captured: list[float] = []
    bc.on_scanline = captured.append
    for _ in range(400):  # a few full periods
        bc._scan_tick()
    assert max(captured) <= 0.5 + 1e-6
    assert min(captured) >= 0.2 - 1e-6


def test_breath_speed_to_period_default():
    # speed 50 (default) => ~2.36 s, matching the original mapping.
    assert abs(breath_speed_to_period(50) - 2.356) < 1e-2


def test_appconfig_migrates_legacy_breath_speed():
    cfg = AppConfig.from_dict({"BreathSpeed": 50})
    assert abs(cfg.breath_period_s - 2.356) < 1e-2
    assert cfg.breath_color == ""
    assert cfg.breath_max_brightness == 1.0


def test_appconfig_breath_roundtrip():
    cfg = AppConfig(breath_period_s=3.0, breath_color="ffaac8",
                    breath_max_brightness=0.6)
    d = cfg.to_dict()
    cfg2 = AppConfig.from_dict(d)
    assert abs(cfg2.breath_period_s - 3.0) < 1e-9
    assert cfg2.breath_color == "ffaac8"
    assert abs(cfg2.breath_max_brightness - 0.6) < 1e-9
