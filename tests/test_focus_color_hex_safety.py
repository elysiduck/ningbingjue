"""P0 regression: focus_color / focus_opacity must never crash the app.

History (2026-08-12):
  Two P0 bugs shared one root cause — ``core/config.py`` and
  ``ui/interaction.py`` parsed user-facing config fields with bare ``int()``
  / ``int(..., 16)`` after a *length-only* check, so a hand-edited
  ``layout.json`` (or a typo in the editor's hex box) crashed the entire app:

  * #P0-1: ``build_focus_style("gghhii", 25)`` → ``int("gg", 16)`` raised
    ``ValueError`` in ``_FocusRect.__init__`` / ``InteractionController.__init__``,
    bubbling up to ``AppWindow`` construction → app refused to start.
    The editor's ``_apply_focus_live`` also wrote the bad hex to disk, so the
    crash reproduced on every subsequent launch.

  * #P0-2: ``AppConfig.from_dict({"FocusOpacity": "abc"})`` → ``int("abc")``
    raised, ``ConfigManager.load`` caught it and silently fell back to
    ``AppConfig()`` — losing EVERY field (cards / power / tiles / presets…),
    not just the bad one.

Fix:
  * ``core/config.py`` adds ``_safe_hex`` + ``_coerce_focus_opacity`` +
    ``_coerce_int`` + ``_coerce_float`` as the single source of truth.
  * ``from_dict`` uses them for focus_color / focus_opacity / tray_opacity /
    tray_blur / breath_max_brightness / page_breaks.
  * ``build_focus_style`` validates hex legality (not just length).
  * ``button_edit._apply_focus_live`` rejects non-hex input before writing.

These tests lock the fix in place.
"""
from __future__ import annotations

import pytest

from ningbingjue.core.config import (
    AppConfig,
    _coerce_focus_opacity,
    _coerce_int,
    _coerce_float,
    _safe_hex,
)
from ningbingjue.ui.button_edit import _coerce_focus_opacity as _reexported_coerce
from ningbingjue.ui.interaction import build_focus_style


# ---------------------------------------------------------------------------
# _safe_hex
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("e6ff1e", "e6ff1e"),       # normal
    ("E6FF1E", "e6ff1e"),       # uppercase normalised
    ("  e6ff1e  ", "e6ff1e"),   # whitespace stripped
    ("e6ff1", "e6ff1e"),        # too short → default
    ("e6ff1e0", "e6ff1e"),      # too long → default
    ("gghhii", "e6ff1e"),       # 6 chars but non-hex → default (P0-1 case)
    ("xyzxyz", "e6ff1e"),       # 6 chars but non-hex → default
    ("12 34", "e6ff1e"),        # contains space → default
    ("", "e6ff1e"),             # empty → default
    (None, "e6ff1e"),           # None → default
    (123456, "123456"),         # int → str → valid 6-hex-digit (123456 is a real CSS color)
    (3.14, "e6ff1e"),           # float → str "3.14" len 4 → default
    ("000000", "000000"),       # all zeros is valid hex
    ("ffffff", "ffffff"),       # all fs is valid hex
])
def test_safe_hex(raw, expected):
    assert _safe_hex(raw) == expected


def test_safe_hex_custom_default():
    assert _safe_hex("bad", default="ff0000") == "ff0000"
    assert _safe_hex("ff0000", default="000000") == "ff0000"


# ---------------------------------------------------------------------------
# _coerce_focus_opacity (re-exported from core, back-compat with old tests)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (0, 0),                     # legitimate 0 preserved (the original bug)
    ("0", 0),
    ("50", 50),
    (70, 70),
    (100, 100),
    (150, 100),                 # clamp high
    (-5, 0),                    # clamp low
    (None, 25),                 # missing → default
    ("", 25),
    ("abc", 25),                # P0-2 case: non-numeric → default
    (True, 25),                 # bool is int subclass but int(True)=1; here
                                # we accept it as 1? No — True != "" and is
                                # not None, so int(True)=1, clamped to 1.
])
def test_coerce_focus_opacity(raw, expected):
    # Special case: bool True → int(True) == 1, which is a valid opacity.
    # We don't special-case bools; the contract is "int-parseable → use it".
    if raw is True:
        assert _coerce_focus_opacity(raw) == 1
    else:
        assert _coerce_focus_opacity(raw) == expected


def test_coerce_focus_opacity_reexported_from_button_edit():
    """The editor module re-exports _coerce_focus_opacity from core so
    existing `from ningbingjue.ui.button_edit import _coerce_focus_opacity`
    imports (tests, other modules) keep working after the move."""
    assert _reexported_coerce is _coerce_focus_opacity
    assert _reexported_coerce(0) == 0
    assert _reexported_coerce("abc") == 25


# ---------------------------------------------------------------------------
# _coerce_int / _coerce_float (used for tray_opacity, tray_blur, etc.)
# ---------------------------------------------------------------------------

def test_coerce_int():
    assert _coerce_int(86, 0) == 86
    assert _coerce_int("86", 0) == 86
    assert _coerce_int(None, 86) == 86
    assert _coerce_int("", 86) == 86
    assert _coerce_int("abc", 86) == 86
    assert _coerce_int(True, 0) == 1   # bool is int; int(True)=1


def test_coerce_float():
    assert _coerce_float(1.0, 0.0) == 1.0
    assert _coerce_float("0.5", 0.0) == 0.5
    assert _coerce_float(None, 1.0) == 1.0
    assert _coerce_float("", 1.0) == 1.0
    assert _coerce_float("abc", 1.0) == 1.0


# ---------------------------------------------------------------------------
# build_focus_style must not crash on any input (P0-1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_hex", [
    "gghhii",       # 6 chars, non-hex — the original crash case
    "xyzxyz",
    "12 34 56",
    "",             # empty
    "e6ff1",        # too short
    "e6ff1e0",      # too long
    None,           # None
    "%%ffff",       # symbols
])
def test_build_focus_style_survives_bad_hex(bad_hex):
    """The original bug: build_focus_style("gghhii", 25) raised ValueError
    in _FocusRect.__init__, killing the whole app.  Now it must fall back to
    the default lime-yellow and never raise."""
    ss = build_focus_style(bad_hex, 25)
    assert isinstance(ss, str)
    assert "rgba(" in ss
    assert "border: 2px solid #e6ff1e" in ss   # fell back to default


def test_build_focus_style_keeps_valid_hex():
    ss = build_focus_style("ff0000", 50)
    assert "rgba(255,0,0,0.500)" in ss
    assert "border: 2px solid #ff0000" in ss


def test_build_focus_style_zero_alpha_still_works():
    """Pre-existing invariant from test_focus_opacity_zero.py: opacity=0
    must render as 0.000 alpha.  Re-asserted here to make sure the hex-
    validation refactor didn't regress it."""
    ss = build_focus_style("e6ff1e", 0)
    assert "0.000" in ss
    assert "0.250" not in ss


# ---------------------------------------------------------------------------
# AppConfig.from_dict must not crash on any single bad field (P0-2)
# ---------------------------------------------------------------------------

def test_from_dict_survives_bad_focus_opacity_string():
    """The original bug: FocusOpacity="abc" made from_dict raise ValueError,
    ConfigManager.load caught it and silently returned default AppConfig(),
    losing every other field.  Now from_dict must coerce the bad value to
    the default and keep all other fields intact."""
    d = {
        "Cards": ["power", "freeze", "tile"],
        "FocusColor": "ff0000",
        "FocusOpacity": "abc",   # ← would have crashed
        "TrayOpacity": 70,
    }
    cfg = AppConfig.from_dict(d)
    assert cfg.cards == ["power", "freeze", "tile"]   # other fields kept
    assert cfg.focus_color == "ff0000"
    assert cfg.focus_opacity == 25                      # bad value → default
    assert cfg.tray_opacity == 70


def test_from_dict_survives_bad_focus_color():
    d = {"FocusColor": "gghhii", "FocusOpacity": 40}
    cfg = AppConfig.from_dict(d)
    assert cfg.focus_color == "e6ff1e"   # bad hex → default
    assert cfg.focus_opacity == 40       # other field kept


def test_from_dict_survives_bad_tray_opacity():
    d = {"TrayOpacity": "not-a-number", "Cards": ["power"]}
    cfg = AppConfig.from_dict(d)
    assert cfg.tray_opacity == 86   # default
    assert cfg.cards == ["power"]


def test_from_dict_survives_bad_breath_brightness():
    d = {"BreathMaxBrightness": "oops", "Cards": ["freeze"]}
    cfg = AppConfig.from_dict(d)
    assert cfg.breath_max_brightness == 1.0
    assert cfg.cards == ["freeze"]


def test_from_dict_survives_bad_page_breaks():
    d = {"PageBreaks": [0, "bad", 3], "Cards": ["a", "b", "c", "d"]}
    cfg = AppConfig.from_dict(d)
    assert cfg.page_breaks == [0, 0, 3]   # "bad" → 0 (coerced)


def test_from_dict_preserves_legitimate_zero_opacity():
    """The original test_focus_opacity_zero.py invariant: FocusOpacity=0 must
    survive a round-trip through from_dict (0 is falsy, must not be eaten by
    a naive `or 25` fallback)."""
    cfg = AppConfig.from_dict({"FocusOpacity": 0})
    assert cfg.focus_opacity == 0


def test_from_dict_clamps_out_of_range_opacity():
    cfg = AppConfig.from_dict({"FocusOpacity": 150})
    assert cfg.focus_opacity == 100
    cfg = AppConfig.from_dict({"FocusOpacity": -5})
    assert cfg.focus_opacity == 0


def test_from_dict_empty_dict_returns_defaults():
    """No fields at all → all defaults, no crash."""
    cfg = AppConfig.from_dict({})
    assert cfg.focus_color == "e6ff1e"
    assert cfg.focus_opacity == 25
    assert cfg.cards == []
