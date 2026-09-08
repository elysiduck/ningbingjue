"""Feature blocks (ports of ``blocks/*.ps1``)."""

from __future__ import annotations

from .changelog import ChangelogBlock
from .fan import FanBlock
from .freeze import FreezeBlock
from .ls import LSBlock
from .perfmon import PerfMonBlock
from .power import PowerBlock
from .procmon import ProcMonBlock
from .rtss import RtssBlock
from .smt import SmtBlock
from .tasklist import TaskListBlock
from .tile import TileBlock
from .undervolt import UndervoltBlock

__all__ = [
    "RtssBlock",
    "ProcMonBlock",
    "FreezeBlock",
    "LSBlock",
    "PowerBlock",
    "TileBlock",
    "UndervoltBlock",
    "PerfMonBlock",
    "SmtBlock",
    "FanBlock",
    "TaskListBlock",
    "ChangelogBlock",
]
