"""ProcMon block tests (headless, MockProcessManager)."""

from __future__ import annotations

from ningbingjue.blocks.procmon import ProcMonBlock
from ningbingjue.core.config import AppConfig
from ningbingjue.platform.process_manager import MockProcessManager, ProcessInfo


def _cfg(items):
    cfg = AppConfig()
    cfg.procmon_configs = [{"Name": n, "Path": p} for n, p in items]
    return cfg


def test_detect_running_by_cmdline():
    cfg = _cfg([("脚本A", "C:/tools/a.exe")])
    block = ProcMonBlock(cfg, MockProcessManager())
    procs = [ProcessInfo(pid=11, name="powershell.exe", cmdline="C:/tools/a.exe -arg", memory_mb=50)]
    assert block.detect_running({"Name": "脚本A", "Path": "C:/tools/a.exe"}, procs) is True
    assert block.detect_running({"Name": "脚本B", "Path": "C:/x/b.exe"}, procs) is False


def test_detect_running_by_basename():
    cfg = _cfg([("b", "C:/x/b.exe")])
    block = ProcMonBlock(cfg, MockProcessManager())
    procs = [ProcessInfo(pid=12, name="b.exe", cmdline="", memory_mb=10)]
    assert block.detect_running({"Name": "b", "Path": "C:/x/b.exe"}, procs) is True


def test_refresh_sets_running_and_renders():
    cfg = _cfg([("a", "C:/a.exe"), ("b", "C:/b.exe")])
    pm = MockProcessManager([ProcessInfo(pid=9, name="a.exe", cmdline="C:/a.exe", memory_mb=30)])
    block = ProcMonBlock(cfg, pm)
    block.refresh_panel()
    assert block.configs[0]["Running"] is True
    assert block.configs[1]["Running"] is False
    assert block.buttons[0].text.startswith("●")
    assert block.buttons[1].text.startswith("○")


def test_toggle_starts_and_stops():
    cfg = _cfg([("a", "C:/a.exe")])
    pm = MockProcessManager([ProcessInfo(pid=9, name="a.exe", cmdline="C:/a.exe", memory_mb=30)])
    block = ProcMonBlock(cfg, pm)
    block.refresh_panel()
    # currently running -> toggle stops
    block.toggle_config(0)
    assert not block.configs[0]["Running"]
    assert pm.stopped
    # now stopped -> toggle starts
    block.toggle_config(0)
    assert block.configs[0]["Running"]
    assert pm.started


def test_save_and_load_roundtrip():
    cfg = _cfg([("a", "C:/a.exe"), ("b", "C:/b.exe")])
    block = ProcMonBlock(cfg, MockProcessManager())
    data = block.save_config()
    assert data == [{"Name": "a", "Path": "C:/a.exe"}, {"Name": "b", "Path": "C:/b.exe"}]
    # reload into a fresh block
    cfg2 = AppConfig()
    cfg2.procmon_configs = data
    block2 = ProcMonBlock(cfg2, MockProcessManager())
    block2.load_config()
    assert [c["Name"] for c in block2.configs] == ["a", "b"]


def test_navigation_cycles_focus():
    cfg = _cfg([("a", "C:/a.exe"), ("b", "C:/b.exe"), ("c", "C:/c.exe")])
    block = ProcMonBlock(cfg, MockProcessManager())
    block.refresh_panel()
    block.on_activate_item()
    assert block.focus_idx.value == 0
    assert block.buttons[0].focused
    block.on_navigate(1)
    assert block.focus_idx.value == 1
    assert block.buttons[1].focused
    block.on_navigate(1)
    assert block.focus_idx.value == 2
    block.on_navigate(1)  # wraps
    assert block.focus_idx.value == 0


def test_item_confirm_toggles_focused():
    cfg = _cfg([("a", "C:/a.exe")])
    pm = MockProcessManager()
    block = ProcMonBlock(cfg, pm)
    block.refresh_panel()
    block.on_activate_item()
    block.on_item_confirm()  # starts a.exe
    assert block.configs[0]["Running"]
    assert pm.started
