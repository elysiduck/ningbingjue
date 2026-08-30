"""End-to-end boot test — all 6 blocks wire together headless."""

from __future__ import annotations

import json
import os
import tempfile

from ningbingjue.app import Application


def _boot_with_all_cards():
    tmp = tempfile.mkdtemp()
    cfg = os.path.join(tmp, "layout.json")
    with open(cfg, "w", encoding="utf-8") as fh:
        json.dump({
            "Cards": ["rtss", "procmon", "freeze", "ls", "power", "tile"],
            "PageBreaks": [0],
            "Theme": {"Name": "少女粉"},
            "CardColors": {},
            "ProcMonConfigs": [{"Name": "脚本A", "Path": "C:/a.exe"}],
            "PowerConfig": {"Tdp": 12, "Gpu": 700, "TdpLock": True, "GpuLock": False, "GpuFloatEnabled": False},
            "Tiles": [{"Name": "官网", "LaunchPath": "http://x", "LaunchType": "URL", "Col": 0, "Row": 0}],
        }, fh, ensure_ascii=False)
    return Application(tmp, config_path=cfg, headless=True).boot(), tmp


def test_all_six_blocks_registered():
    app, _ = _boot_with_all_cards()
    for t in ("rtss", "procmon", "freeze", "ls", "power", "tile"):
        assert t in app.registry, f"{t} not registered"
        assert t in app.blocks
    assert app.nav.total_pages() == 1


def test_card_height_hooks_work():
    app, _ = _boot_with_all_cards()
    for t in app.registry:
        block = app.registry.get(t)
        h = block.hook("GetCardHeight")
        assert h is not None
        assert isinstance(h(), int) and h() > 0


def test_backward_compat_shims_wired():
    app, _ = _boot_with_all_cards()
    expected = {
        "Set-RTSSLock", "Toggle-Lock", "Apply-Lock",
        "Load-RTSSConfig", "Save-RTSSConfig",
        "Refresh-ProcMonPanel", "Toggle-ProcMonScript",
        "Save-ProcMonConfig", "Load-ProcMonConfig",
        "Refresh-FreezePanel",
        "Refresh-LSPanel", "Load-LSPreset",
        "Update-PowerStatus", "Apply-PowerSettings",
        "Save-PowerConfig", "Load-PowerConfig",
        "Toggle-PowerLock", "Toggle-GpuLock", "Toggle-GpuFloat",
        "Layout-TilePanel", "Save-TileConfigs", "Load-TileConfigs",
        "Invoke-TileLaunch", "Handle-FreezeHotkey", "Handle-FreezeEnter",
    }
    for name in expected:
        assert name in app.shims, f"missing shim {name}"


def test_procmon_toggle_via_shim():
    app, _ = _boot_with_all_cards()
    toggle = app.shims["Toggle-ProcMonScript"]
    block = app.blocks["procmon"]
    before = block.configs[0]["Running"]
    toggle(0)  # start scriptA (mock pm records start)
    after = block.configs[0]["Running"]
    assert after != before


def test_save_all_persists():
    app, tmp = _boot_with_all_cards()
    app.save_all()
    with open(os.path.join(tmp, "layout.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    assert "PowerConfig" in data
    assert data["PowerConfig"]["Tdp"] == 12
    assert "Tiles" in data and len(data["Tiles"]) == 1
    assert data["ProcMonConfigs"][0]["Name"] == "脚本A"
