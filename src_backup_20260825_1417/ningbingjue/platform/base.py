"""Platform adapter base — the seam that lets the core run headless.

Every external Windows dependency (process suspend/resume, RTSS CLI, Lossless
Scaling, ryzenadj, window overlay) is behind an interface.  Production code uses
the real Win32-backed implementation; tests inject a Mock (see ``*_mock``
factories).  This is the "接口抽象 + Mock" strategy agreed with the user.
"""

from __future__ import annotations

import abc
from typing import Any


class ExternalAdapter(abc.ABC):
    """Marker base for all platform adapters."""

    @property
    @abc.abstractmethod
    def available(self) -> bool:
        """Whether the backing native tool/API is usable right now."""
        ...


__all__ = ["ExternalAdapter", "Any"]
