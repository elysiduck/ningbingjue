"""Mutable color/theme global state.

The original script relies on ``$global:ActiveThemeName`` / ``$global:ThemeAccentColors``
read by every color API.  We centralise that mutable state here so the pure
functions stay testable (a test can ``reset_state()`` between cases).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .rgb import Color


@dataclass
class ColorState:
    active_theme_name: str = "原夜"
    theme_accent_colors: dict[str, Any] = field(default_factory=dict)
    freeze_highlight_color: Color = Color.from_argb(255, 175, 90, 70)
    freeze_highlight_line_color: Color = Color.from_argb(255, 255, 200, 110)
    active_button_style: str = "暗黑科技"
    registered_buttons: list = field(default_factory=list)
    # -- palette overrides from layout.json Theme section (None = use constants) --
    bg_app: Color | None = None
    bg_card: Color | None = None
    fg_primary: Color | None = None
    fg_secondary: Color | None = None
    # -- canonical theme accent (the single "theme color" of the app) ---------
    # Driven by the breathing-light colour (cfg.breath_color).  The breathing
    # glow, the window neon border, the tray icon, and any future consumer all
    # read this one field so accent is adjusted in a single place.  None means
    # "use the default" (see DEFAULT_ACCENT in semantic.py).
    theme_accent: Color | None = None


# Single process-wide state instance.
STATE = ColorState()


def reset_state() -> None:
    """Reset mutable color state to defaults (used by tests for isolation)."""
    STATE.active_theme_name = "原夜"
    STATE.theme_accent_colors = {}
    STATE.freeze_highlight_color = Color.from_argb(255, 175, 90, 70)
    STATE.freeze_highlight_line_color = Color.from_argb(255, 255, 200, 110)
    STATE.active_button_style = "暗黑科技"
    STATE.registered_buttons = []
    STATE.bg_app = None
    STATE.bg_card = None
    STATE.fg_primary = None
    STATE.fg_secondary = None
    STATE.theme_accent = None


def apply_palette_overrides(theme: Any) -> None:
    """Apply ``layout.json`` Theme overrides to the base palette (BG/BgCard/Fg)."""
    from .rgb import Color

    def _csv(csv: str | None, default: Color) -> Color:
        if not csv:
            return default
        parts = [p.strip() for p in csv.split(",")]
        if len(parts) == 3:
            try:
                return Color.from_rgb(int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError:
                return default
        return default

    from .palette import BG_APP, BG_CARD, FG_PRIMARY, FG_SECONDARY

    STATE.bg_app = _csv(getattr(theme, "bg_app", None), BG_APP)
    STATE.bg_card = _csv(getattr(theme, "bg_card", None), BG_CARD)
    STATE.fg_primary = _csv(getattr(theme, "fg_primary", None), FG_PRIMARY)
    # The original Theme section does not always carry fg_secondary; tolerate its
    # absence so palette overrides still apply from bg_app/bg_card/fg_primary.
    STATE.fg_secondary = _csv(getattr(theme, "fg_secondary", None), FG_SECONDARY)


def set_active_theme(name: str) -> None:
    STATE.active_theme_name = name
