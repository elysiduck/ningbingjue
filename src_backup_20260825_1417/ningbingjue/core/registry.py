"""§5.14 Lightweight plugin registry.

Mirrors ``$global:BlockRegistry`` + ``Register-Block``.  A ``Block`` carries its
type/name/color/card plus a ``hooks`` dict of callables; the main engine only
ever talks to a block through those hooks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class Block:
    """A registered feature block (e.g. rtss, freeze, ls, power, tile)."""

    type: str
    name: str
    color: Any
    card: Any = None
    hooks: dict[str, Callable] = field(default_factory=dict)
    order: int = 0

    def hook(self, name: str) -> Optional[Callable]:
        return self.hooks.get(name)


class BlockRegistry:
    """Ordered, type-keyed registry of blocks."""

    def __init__(self) -> None:
        self._blocks: dict[str, Block] = {}
        self._order: list[str] = []

    def register(self, block: Block) -> None:
        if block.type not in self._blocks:
            self._order.append(block.type)
        self._blocks[block.type] = block

    def get(self, block_type: str) -> Optional[Block]:
        return self._blocks.get(block_type)

    def __contains__(self, block_type: str) -> bool:
        return block_type in self._blocks

    def keys(self) -> list[str]:
        return list(self._order)

    def values(self) -> list[Block]:
        return [self._blocks[t] for t in self._order]

    def items(self) -> list[tuple[str, Block]]:
        return [(t, self._blocks[t]) for t in self._order]

    def __iter__(self):
        return iter(self._order)

    def card_to_type(self) -> dict[Any, str]:
        return {b.card: t for t, b in self._blocks.items() if b.card is not None}

    def all_cards(self) -> list[Any]:
        return [self._blocks[t].card for t in self._order if self._blocks[t].card is not None]
