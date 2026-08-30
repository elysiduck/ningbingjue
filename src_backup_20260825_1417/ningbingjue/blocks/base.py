"""Block base — the standard Hooks contract every block implements.

Mirrors ``blocks/_template.ps1``.  ``Register-Block @{ Type; Name; Color; Card; Hooks }``
becomes ``BlockBlueprint(...)`` -> ``Block``.
"""

from __future__ import annotations

from typing import Any, Callable

from ..core.registry import Block

# The full standard hook surface (order-insensitive).
# NOTE: OnNavigate / OnItemConfirm were removed — card-internal gamepad/keyboard
# navigation is fully handled by the panel-driven InteractionController
# (panels.py nav_items()), so the block-level navigation hooks were dead code.
STANDARD_HOOKS: tuple[str, ...] = (
    "GetCardHeight", "OnRefresh", "OnLayout", "OnThemeChange", "OnSyncTitleColor",
    "OnSaveConfig", "OnLoadConfig", "OnEditLayout", "OnCheckEditMode",
    "OnEnterEditMode", "OnExitEditMode", "OnActivateItem", "OnDeactivateItem",
    "OnApplyLock", "OnToggleLock", "OnPowerResume",
)


class BlockBlueprint:
    """Builder mirroring the PowerShell ``Register-Block`` hashtable."""

    def __init__(
        self,
        block_type: str,
        name: str,
        color: Any,
        card: Any = None,
        hooks: dict[str, Callable] | None = None,
    ) -> None:
        self.type = block_type
        self.name = name
        self.color = color
        self.card = card
        self.hooks = dict(hooks or {})

    def to_block(self) -> Block:
        return Block(
            type=self.type,
            name=self.name,
            color=self.color,
            card=self.card,
            hooks=self.hooks,
        )
