"""Theme manager — applies an accent theme and propagates it to blocks.

Mirrors ``Apply-ThemeAccents``: compute the accent dict, update the shared
color state, then walk every registered block calling its ``OnThemeChange``
hook with the accent dict (exactly how the PowerShell ``$accent`` is passed).
"""

from __future__ import annotations

from typing import Any

from .color.state import STATE
from .color.themes import apply_theme
from .registry import BlockRegistry


class ThemeManager:
    def __init__(self, registry: BlockRegistry) -> None:
        self.registry = registry

    def apply(self, name: str) -> dict[str, Any]:
        """Activate ``name`` and propagate to all blocks' ``OnThemeChange``."""
        accent = apply_theme(name)
        for block_type in self.registry:
            block = self.registry.get(block_type)
            hook = block.hook("OnThemeChange") if block else None
            if hook:
                try:
                    hook(accent)
                except Exception:
                    # a block must never break theme application
                    pass
        return accent

    def current(self) -> str:
        return STATE.active_theme_name

    def propagate(self) -> None:
        """Re-fire ``OnThemeChange`` on every registered block (no state change).

        Used after the breathing colour changes so all blocks re-read the
        canonical theme accent (``get_theme_accent``).  The accent dict passed
        to the hooks is kept for API compatibility but blocks ignore it and read
        the breathing colour directly.
        """
        accent = STATE.theme_accent_colors
        for block_type in self.registry:
            block = self.registry.get(block_type)
            hook = block.hook("OnThemeChange") if block else None
            if hook:
                try:
                    hook(accent)
                except Exception:
                    # a block must never break propagation
                    pass
