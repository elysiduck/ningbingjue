"""Tests for the power/tile/procmon port-faithfulness fixes."""

from __future__ import annotations

from ningbingjue.core.config import AppConfig
from ningbingjue.platform.process_manager import (
    MockProcessManager,
    ProcessInfo,
    Win32ProcessManager,
)
from ningbingjue.platform.ryzenadj import MockRyzenAdj
from ningbingjue.blocks.tile import TileBlock
from ningbingjue.blocks.procmon import ProcMonBlock


def test_win32_stop_matching_is_callable():
    # stop_matching is now part of the abstract contract.
    assert hasattr(Win32ProcessManager, "stop_matching")


def test_mock_stop_matching_kills_by_cmdline():
    pm = MockProcessManager(processes=[
        ProcessInfo(pid=101, name="cmd.exe", cmdline="cmd.exe /c C:\\scripts\\a.bat"),
        ProcessInfo(pid=102, name="notepad.exe", cmdline="notepad"),
    ])
    hit = pm.stop_matching("C:\\scripts\\a.bat")
    assert hit is True
    assert 101 in pm.stopped
    # the unrelated process survives
    assert len(pm.processes) == 1 and pm.processes[0].pid == 102


def test_tile_add_remove_updates_config():
    cfg = AppConfig()
    b = TileBlock(cfg, MockProcessManager())
    assert len(b.tiles) == 0
    b.add_tile("记事本", "C:\\Windows\\notepad.exe")
    assert len(b.tiles) == 1
    assert b.tiles[0].name == "记事本"
    assert b.tiles[0].launch_type == "EXE"
    assert len(cfg.tiles) == 1
    b.update_tile(0, name="NP", width=2, height=2)
    assert b.tiles[0].name == "NP"
    assert b.tiles[0].width == 2
    b.remove_tile(0)
    assert len(b.tiles) == 0
    assert len(cfg.tiles) == 0


def test_tile_detect_type():
    import os
    assert TileBlock.detect_type("https://x.com") == "URL"
    assert TileBlock.detect_type("C:\\a\\b.png") == "IMG"
    assert TileBlock.detect_type("C:\\a.bat") == "BAT"
    assert TileBlock.detect_type(os.getcwd()) == "DIR"  # existing directory


def test_procmon_toggle_starts_then_stops_by_path():
    cfg = AppConfig()
    cfg.procmon_configs = {"脚本A": {"Path": "C:\\scripts\\a.bat"}}
    pm = MockProcessManager(processes=[
        ProcessInfo(pid=555, name="cmd.exe", cmdline="cmd.exe /c C:\\scripts\\a.bat"),
    ])
    b = ProcMonBlock(cfg, pm)
    assert len(b.configs) == 1
    # start
    b.toggle_config(0)
    assert "C:\\scripts\\a.bat" in pm.started
    assert b.configs[0]["Running"] is True
    # stop -> must resolve by command-line match, not by image name
    b.configs[0]["Running"] = True
    b.toggle_config(0)
    assert 555 in pm.stopped
    assert b.configs[0]["Running"] is False


def test_procmon_add_remove_config():
    cfg = AppConfig()
    b = ProcMonBlock(cfg, MockProcessManager())
    assert len(b.configs) == 0
    b.add_config("监控B", "C:\\b.ps1")
    assert len(b.configs) == 1
    assert cfg.procmon_configs  # persisted to config
    b.remove_config(0)
    assert len(b.configs) == 0
