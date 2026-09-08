"""Regression test for the interactive PyQt6 panels.

Builds the real overlay (offscreen) and ticks every panel, ensuring the
state-driven wiring does not throw — the panels must stay constructable and
refreshable even without a visible display.
"""

import os
import threading
import time

import pytest
from PyQt6.QtWidgets import QApplication

from ningbingjue.app import Application
from ningbingjue.ui.app_window import _RefreshWorker
from ningbingjue.ui.panels import BlockPanel

ROOT = (
    r"D:\下载\localsend\凝冰结魄-覆盖任务栏-修复-完全解耦-备份-20260717172824"
    r"\凝冰结魄-覆盖任务栏-修复-完全解耦-备份"
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_overlay_builds_six_panels(qapp):
    app = Application(ROOT, headless=False).boot()
    panels = app._window.findChildren(BlockPanel)
    # At minimum one panel per base block type (6); extra tile blocks add more.
    assert len(panels) >= 6
    qapp.processEvents()
    for p in panels:
        p.tick()
    qapp.processEvents()
    app._window.close()
    qapp.processEvents()


def test_block_interactions_do_not_throw(qapp):
    app = Application(ROOT, headless=False).boot()
    rtss = app.blocks["rtss"]
    before = rtss.lock_enabled
    rtss.toggle_lock()
    assert rtss.lock_enabled != before  # toggle flips state
    powb = app.blocks["power"]
    before_tdp = powb.tdp_lock
    powb.toggle_tdp_lock()
    assert powb.tdp_lock != before_tdp
    powb.apply_power()
    proc = app.blocks["procmon"]
    if proc.configs:
        proc.toggle_config(0)
    ls = app.blocks["ls"]
    if ls.presets:
        ls._load(0)
    tile = app.blocks["tile"]
    if tile.tiles:
        # launch may fail to find the target off-host, but must not raise
        tile.launch(tile.tiles[0])
    app._window.close()
    qapp.processEvents()


def test_heavy_refresh_runs_off_main_thread(qapp):
    """The background worker must run slow refresh on a worker thread so the
    UI thread never blocks on ``wmic`` / ``ryzenadj`` subprocess spawns."""
    ran: dict[str, str] = {}

    def slow_task() -> None:
        ran["thread"] = threading.current_thread().name
        time.sleep(0.25)

    worker = _RefreshWorker()
    worker.set_tasks([slow_task])
    t0 = time.perf_counter()
    worker.start()
    # The UI thread must stay responsive while the slow task runs.
    qapp.processEvents()
    elapsed = time.perf_counter() - t0
    worker.wait()
    assert ran.get("thread") != "MainThread"
    # processEvents returned without waiting for the 0.25s task.
    assert elapsed < 0.1
