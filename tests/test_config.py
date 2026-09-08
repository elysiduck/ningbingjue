"""Config manager tests — layout.json load/save + page derivation."""

from __future__ import annotations

import json

from ningbingjue.core.color.rgb import Color
from ningbingjue.core.config import AppConfig, ConfigManager


SAMPLE = {
    "ExtraTileBlocks": [{"BlockName": "新区块_1", "ShowBlockTitle": True, "BlockThemeColor": "", "Tiles": []}],
    "PowerConfig": {"Gpu": 700, "Tdp": 10, "TdpLock": False, "GpuFloatEnabled": False, "GpuLock": False},
    "CardColors": {"power": "255,130,140", "ls": "95,175,235"},
    "Tiles": [{"Name": "新磁贴", "Col": 0, "Row": 0, "Width": 1, "Height": 1}],
    "ProcMonConfigs": {},
    "Theme": {"BgCard": "28,28,28", "Name": "碳黑", "DecorSeed": 0, "BgApp": "16,16,16",
              "EditApplyTheme": "Green", "FgPrimary": "225,225,225", "EditCancelTheme": "Red"},
    "Cards": ["tile", "ls", "procmon", "rtss", "power", "freeze", "tile"],
    "PageBreaks": [0],
    "ButtonStyle": "ZZZ",
}


def test_load_and_roundtrip(tmp_path):
    p = tmp_path / "layout.json"
    p.write_text(json.dumps(SAMPLE), encoding="utf-8")
    cm = ConfigManager()
    cfg = cm.load(str(p))
    assert cfg.theme.name == "碳黑"
    assert cfg.cards == SAMPLE["Cards"]
    assert cfg.power_config.tdp == 10
    assert cfg.card_color("ls") == Color.from_rgb(95, 175, 235)

    # round-trip
    out = tmp_path / "out.json"
    cm.save(str(out))
    cm2 = ConfigManager()
    cfg2 = cm2.load(str(out))
    assert cfg2.to_dict() == cfg.to_dict()


def test_pages_derived_from_cards_and_breaks():
    cm = ConfigManager()
    cm.config = AppConfig.from_dict(SAMPLE)
    pages = cm.pages()
    assert len(pages) == 1
    assert pages[0] == SAMPLE["Cards"]


def test_multiple_pages():
    data = dict(SAMPLE)
    data["PageBreaks"] = [0, 3]
    cm = ConfigManager()
    cm.config = AppConfig.from_dict(data)
    pages = cm.pages()
    assert len(pages) == 2
    assert pages[0] == SAMPLE["Cards"][0:3]
    assert pages[1] == SAMPLE["Cards"][3:]
