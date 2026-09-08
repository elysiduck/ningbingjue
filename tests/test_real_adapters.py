"""Regression tests for the REAL external adapters (not the Mock adapters).

These guard code paths that only fire on the Windows host when the actual tools
are present.  The normal unit suite exercises the Mock adapters, so the real
adapter wiring was previously unguarded and two real-adapter defects slipped
through: ``LosslessScaling`` lacked ``is_running()``, and ``build_ryzenadj``
referenced a non-existent ``Win32RyzenAdj`` class.
"""

from __future__ import annotations

import os
import tempfile

from ningbingjue.app import build_ryzenadj
from ningbingjue.platform.lossless_scaling import LosslessScaling
from ningbingjue.platform.process_manager import MockProcessManager, ProcessInfo
from ningbingjue.platform.ryzenadj import RyzenAdj


def test_real_lossless_is_running_detects_ls_process() -> None:
    pm = MockProcessManager(processes=[
        ProcessInfo(pid=1, name="explorer.exe"),
        ProcessInfo(pid=2, name="LosslessScaling.exe"),
    ])
    ls = LosslessScaling("settings.xml", "presets", process_manager=pm)
    assert ls.is_running() is True


def test_real_lossless_is_running_absent() -> None:
    pm = MockProcessManager(processes=[ProcessInfo(pid=1, name="explorer.exe")])
    ls = LosslessScaling("settings.xml", "presets", process_manager=pm)
    assert ls.is_running() is False


def test_build_ryzenadj_real_path_returns_ryzenadj_class() -> None:
    with tempfile.TemporaryDirectory() as d:
        rdir = os.path.join(d, "blocks", "ryzenadj-win64")
        os.makedirs(rdir)
        exe = os.path.join(rdir, "ryzenadj.exe")
        with open(exe, "w", encoding="ascii") as fh:
            fh.write("")
        inst = build_ryzenadj(d)
    assert isinstance(inst, RyzenAdj)
