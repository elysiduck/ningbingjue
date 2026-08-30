"""Window/overlay helpers — pin the form above the taskbar (Windows only).

Mirrors the overlay behavior of the PowerShell form (always-on-top, covering the
taskbar).  Headless hosts use ``MockOverlayWindow``.
"""

from __future__ import annotations

import sys
from typing import Any

from .base import ExternalAdapter


class OverlayWindow(ExternalAdapter):
    def __init__(self, hwnd: int | None = None) -> None:
        self.hwnd = hwnd

    @property
    def available(self) -> bool:
        return sys.platform.startswith("win")

    def set_always_on_top(self, hwnd: int | None = None) -> bool:
        return self.available

    def cover_taskbar(self, rect: Any | None = None) -> bool:
        return self.available


class MockOverlayWindow(OverlayWindow):
    def __init__(self) -> None:
        super().__init__(None)
        self.calls: list[tuple[str, Any]] = []

    @property
    def available(self) -> bool:
        return True

    def set_always_on_top(self, hwnd: int | None = None) -> bool:
        self.calls.append(("always_on_top", hwnd))
        return True

    def cover_taskbar(self, rect: Any | None = None) -> bool:
        self.calls.append(("cover", rect))
        return True
