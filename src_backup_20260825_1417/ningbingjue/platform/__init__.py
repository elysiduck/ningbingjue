"""Platform adapters — external Windows tool abstractions + mocks."""

from __future__ import annotations

from .base import ExternalAdapter
from .process_manager import MockProcessManager, ProcessManager, Win32ProcessManager
from .rtss_cli import MockRtssCli, RtssCli, Win32RtssCli
from .lossless_scaling import LosslessScaling, MockLosslessScaling
from .ryzenadj import MockRyzenAdj, RyzenAdj
from .battery import get_battery_status
from .window import MockOverlayWindow, OverlayWindow

__all__ = [
    "ExternalAdapter",
    "ProcessManager", "Win32ProcessManager", "MockProcessManager",
    "RtssCli", "Win32RtssCli", "MockRtssCli",
    "LosslessScaling", "MockLosslessScaling",
    "RyzenAdj", "MockRyzenAdj",
    "get_battery_status",
    "OverlayWindow", "MockOverlayWindow",
]
