"""§ Navigation — page/zone/item-mode + 2D matrix navigation.

Faithful port of ``Switch-Page``, ``Switch-Zone``, ``Enter-ItemMode``,
``Exit-ItemMode``, ``MoveItemFocus``, ``Navigate-Matrix``, ``Default-BlockNavigate``,
``Clear-BlockFocus`` and ``Set-ControlFocusVisual``.

A control participates in navigation by exposing (any subset):
* ``ctrl_col`` / ``ctrl_row`` (int) — explicit grid coordinates, OR
* ``tag`` with ``.col`` / ``.row``, OR
* ``location`` with ``.x`` / ``.y`` (pixels, auto-detected span).
* ``set_focus_visual(focused)`` — visual feedback hook (optional).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


class FocusHolder:
    """Mutable int holder standing in for PowerShell ``[ref]$idx``."""

    __slots__ = ("value",)

    def __init__(self, value: int = 0) -> None:
        self.value = value


def set_control_focus_visual(ctrl: Any, focused: bool) -> None:
    if ctrl is None:
        return
    setter = getattr(ctrl, "set_focus_visual", None)
    if callable(setter):
        try:
            setter(focused)
            return
        except Exception:
            return
    if hasattr(ctrl, "focused"):
        try:
            ctrl.focused = focused
        except Exception:
            pass


def clear_block_focus(items: list[Any]) -> None:
    for ctrl in items:
        if ctrl is not None:
            set_control_focus_visual(ctrl, False)


def default_block_navigate(items: list[Any], direction: int, focus: FocusHolder) -> None:
    """1-D cyclic navigation (``Default-BlockNavigate``)."""
    count = len(items)
    if count == 0:
        return
    old = focus.value
    if 0 <= old < count:
        set_control_focus_visual(items[old], False)
    new = old + direction
    if new < 0:
        new = count - 1
    elif new >= count:
        new = 0
    focus.value = new
    set_control_focus_visual(items[new], True)


def _detect_row_span(controls: list[Any]) -> int:
    ys = sorted({getattr(c.location, "y", None) for c in controls if c is not None and getattr(c, "location", None) is not None})
    ys = [y for y in ys if y is not None]
    if len(ys) <= 1:
        return 40
    diffs = [ys[i] - ys[i - 1] for i in range(1, len(ys)) if (ys[i] - ys[i - 1]) > 5]
    if not diffs:
        return 40
    mid = diffs[len(diffs) // 2]
    return mid


def _detect_col_span(controls: list[Any]) -> int:
    xs = sorted({getattr(c.location, "x", None) for c in controls if c is not None and getattr(c, "location", None) is not None})
    xs = [x for x in xs if x is not None]
    if len(xs) <= 1:
        return 100
    diffs = [xs[i] - xs[i - 1] for i in range(1, len(xs)) if (xs[i] - xs[i - 1]) > 5]
    if not diffs:
        return 100
    mid = diffs[len(diffs) // 2]
    return mid


def _extract_position(ctrl: Any, controls: list[Any], index: int) -> dict[str, Any]:
    col = 0
    row = 0
    found = False
    cc = getattr(ctrl, "ctrl_col", None)
    cr = getattr(ctrl, "ctrl_row", None)
    if cc is not None and cr is not None:
        col, row, found = int(cc), int(cr), True
    if not found:
        tag = getattr(ctrl, "tag", None)
        if tag is not None and hasattr(tag, "col") and hasattr(tag, "row"):
            col, row, found = int(tag.col), int(tag.row), True
    if not found:
        loc = getattr(ctrl, "location", None)
        if loc is not None:
            span_y = _detect_row_span(controls)
            span_x = _detect_col_span(controls)
            row = round(loc.y / max(1, span_y))
            col = round(loc.x / max(1, span_x))
        else:
            col, row = index % 4, index // 4
    return {"ctrl": ctrl, "col": col, "row": row, "index": index}


def navigate_matrix(
    controls: list[Any],
    direction: int,
    focus: FocusHolder,
    on_focus_changed: Optional[Callable[[int, int], None]] = None,
) -> None:
    """``Navigate-Matrix`` — 2D grid navigation.

    direction: -1 up, 1 down, -10 left, 10 right.  Edges wrap within row/column,
    with a sequential fallback when a column has a single control.
    """
    count = len(controls)
    if count == 0:
        return
    old_idx = focus.value
    if old_idx < 0 or old_idx >= count:
        old_idx = 0
        focus.value = 0

    positions = [_extract_position(c, controls, i) for i, c in enumerate(controls) if c is not None]
    if not positions:
        return

    cur = next((p for p in positions if p["index"] == old_idx), positions[0])

    if abs(direction) == 10:
        delta = 1 if direction > 0 else -1
        same_row = sorted([p for p in positions if p["row"] == cur["row"] and p["index"] != old_idx], key=lambda p: p["col"])
        if same_row:
            if delta > 0:
                target = next((p for p in same_row if p["col"] > cur["col"]), same_row[0])
            else:
                target = next((p for p in reversed(same_row) if p["col"] < cur["col"]), same_row[-1])
            new_idx = target["index"]
        else:
            new_idx = old_idx
    else:
        delta = 1 if direction > 0 else -1
        same_col = sorted([p for p in positions if p["col"] == cur["col"] and p["index"] != old_idx], key=lambda p: p["row"])
        if same_col:
            if delta > 0:
                target = next((p for p in same_col if p["row"] > cur["row"]), same_col[0])
            else:
                target = next((p for p in reversed(same_col) if p["row"] < cur["row"]), same_col[-1])
            new_idx = target["index"]
        else:
            ordered = sorted(positions, key=lambda p: (p["row"], p["col"]))
            pos_idx = next((j for j, p in enumerate(ordered) if p["index"] == old_idx), -1)
            if pos_idx >= 0:
                new_pos = (pos_idx + 1) % len(ordered) if delta > 0 else (pos_idx - 1 + len(ordered)) % len(ordered)
                new_idx = ordered[new_pos]["index"]
            else:
                new_idx = old_idx

    if new_idx != old_idx and 0 <= new_idx < count:
        set_control_focus_visual(controls[old_idx], False)
        focus.value = new_idx
        if on_focus_changed:
            try:
                on_focus_changed(old_idx, new_idx)
            except Exception:
                pass
        else:
            set_control_focus_visual(controls[new_idx], True)


@dataclass
class NavigationController:
    """Page/zone/item-mode state machine wired to the BlockRegistry.

    Page segmentation lives on the *expanded* block order (one entry per
    panel: ``tile`` plus ``tile#0`` … ``tile#N`` for each extra tile block),
    which is exactly what the HUD renders and what the layout editor rows
    map to.  ``app.render_pages()`` is the single source of truth; it splits
    ``full_block_order()`` at ``config.page_breaks``.
    """

    registry: Any
    config: Any
    app: Any = None
    current_page: int = 0
    focus_zone: int = 0  # -1 == EXIT button virtual focus
    in_item_mode: bool = False
    item_mode_type: Optional[str] = None

    def _pages(self) -> list[list[str]]:
        if self.app is not None:
            return self.app.render_pages()
        return self.config.pages()

    def current_page_cards(self) -> list[str]:
        pages = self._pages()
        if not pages:
            return []
        return pages[self.current_page % len(pages)]

    def total_pages(self) -> int:
        return max(1, len(self._pages()))

    def _fire_page_hooks(self) -> None:
        """Re-run each block's OnRefresh/OnLayout hook for the current page.

        ``tile#N`` tokens resolve to the base ``tile`` block so the hook
        actually fires (the registry key is ``tile``, not ``tile#N``).

        The ``tile`` block is a STATIC launcher with no volatile data — it must
        never be refreshed on a timer or on page entry (that churn caused the
        intermittent "磁贴全黑刷新" flash).  It is skipped here; tile content
        only changes through the editor (rebuild() + tile.load_config()), which
        is handled separately.
        """
        for card_type in self.current_page_cards():
            base = card_type.split("#")[0] if "#" in card_type else card_type
            if base == "tile":
                continue
            block = self.registry.get(base)
            if not block:
                continue
            for hk in ("OnRefresh", "OnLayout"):
                hook = block.hook(hk)
                if hook:
                    try:
                        hook()
                    except Exception:
                        pass

    def switch_page(self, direction: int) -> None:
        pages = self._pages()
        if not pages:
            return
        new = (self.current_page + direction) % len(pages)
        self.current_page = new
        self.exit_item_mode()
        self.focus_zone = 0
        # NOTE: deliberately do NOT call _fire_page_hooks() here.  Firing every
        # block's OnRefresh/OnLayout on a page switch spawned heavy work
        # (ryzenadj subprocesses for power/rtss) synchronously on the UI thread
        # → the "切换页面极度卡顿" complaint.  Live blocks still refresh on their
        # own 2s timer, and the HUD repaints current state via sync_all(), so
        # the newly-shown page is correct within one tick without the jank.
        # Static blocks (tile) never needed refreshing at all (see _fire_page_hooks).
        self.update_zone_highlight()

    def goto_page(self, index: int) -> None:
        """Jump to an absolute page index (the HUD page-bar buttons)."""
        pages = self._pages()
        if not pages:
            return
        new = index % len(pages)
        self.current_page = new
        self.exit_item_mode()
        self.focus_zone = 0
        # Same rationale as switch_page: no per-block hook firing on switch.
        self.update_zone_highlight()

    def switch_zone(self, direction: int) -> None:
        cards = self.current_page_cards()
        max_zone = len(cards) - 1
        if self.focus_zone == -1:
            if direction == 1:
                self.focus_zone = 0
            else:
                return
        elif self.focus_zone + direction < 0:
            self.focus_zone = -1
        elif self.focus_zone + direction <= max_zone:
            self.focus_zone += direction
        else:
            return
        self.update_zone_highlight()

    def update_zone_highlight(self) -> None:
        # Headless: the UI layer would repaint here.
        pass

    def enter_item_mode(self) -> None:
        if self.in_item_mode:
            return
        cards = self.current_page_cards()
        if not cards or self.focus_zone < 0 or self.focus_zone >= len(cards):
            return
        focus_card = cards[self.focus_zone]
        block = self.registry.get(focus_card)
        if block is None:
            return
        self.item_mode_type = block.type
        self.in_item_mode = True
        self.update_zone_highlight()
        if block and block.hook("OnActivateItem"):
            try:
                block.hook("OnActivateItem")()
            except Exception:
                self.in_item_mode = False
                self.update_zone_highlight()

    def exit_item_mode(self) -> None:
        type_ = self.item_mode_type
        self.in_item_mode = False
        self.item_mode_type = None
        if type_ and type_ in self.registry:
            block = self.registry.get(type_)
            if block and block.hook("OnDeactivateItem"):
                try:
                    block.hook("OnDeactivateItem")()
                except Exception:
                    pass
        self.update_zone_highlight()

    def move_item_focus(self, direction: int) -> None:
        if not self.in_item_mode:
            return
        type_ = self.item_mode_type
        if not type_ or type_ not in self.registry:
            return
        block = self.registry.get(type_)
        if block and block.hook("OnNavigate"):
            try:
                block.hook("OnNavigate")(direction)
            except Exception:
                pass
