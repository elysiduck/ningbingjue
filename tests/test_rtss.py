"""rtss block logic tests — lock toggle, apply, config persistence, navigation."""

from __future__ import annotations

import json
import os

import pytest

from ningbingjue.blocks.rtss import RtssBlock
from ningbingjue.platform.rtss_cli import MockRtssCli


def _block(tmp_path, make_rtss_ui):
    cli = MockRtssCli()
    block = RtssBlock(cli, str(tmp_path), ui=make_rtss_ui())
    return block, cli


def test_toggle_lock_enables_and_calls_cli(tmp_path, make_rtss_ui):
    block, cli = _block(tmp_path, make_rtss_ui)
    assert block.lock_enabled is False
    block.toggle_lock()
    assert block.lock_enabled is True
    assert block.lock_applied is True
    assert ("limiter", True) in cli.calls
    assert ("limit", 30) in cli.calls  # 出厂默认 FPS = 30（非 60）
    # config persisted
    data = json.loads((tmp_path / "rtss_config.json").read_text(encoding="utf-8"))
    assert data["LockEnabled"] is True
    assert data["FPS"] == 30


def test_apply_lock_disabled_sends_zero(tmp_path, make_rtss_ui):
    block, cli = _block(tmp_path, make_rtss_ui)
    block.lock_enabled = False
    block.apply_lock()
    assert ("limiter", False) in cli.calls
    assert ("limit", 0) in cli.calls


def test_update_slider_ui_clamps_and_flags(tmp_path, make_rtss_ui):
    block, _ = _block(tmp_path, make_rtss_ui)
    block.lock_enabled = True
    block.update_slider_ui(45)
    assert block.fps == 45
    assert block.lock_applied is False
    block.update_slider_ui(999)
    assert block.fps == 60
    block.update_slider_ui(-5)
    assert block.fps == 20


def test_load_config_restores_state(tmp_path, make_rtss_ui):
    (tmp_path / "rtss_config.json").write_text(
        json.dumps({"LockEnabled": True, "FPS": 30}), encoding="utf-8"
    )
    block, _ = _block(tmp_path, make_rtss_ui)
    block.load_config()
    assert block.lock_enabled is True
    assert block.fps == 30


@pytest.mark.xfail(
    reason="旧 headless 导航契约：RtssBlock 从未实现 on_item_confirm"
           "（全项目 0 处实现）。Phase 2 后焦点由 InteractionController 统一"
           "接管，GUI 不更新 block.focus_idx，此路径当前无调用方。留档待补。",
    strict=False,
)
def test_navigation_confirm_toggles_on_lock_button(tmp_path, make_rtss_ui):
    block, _ = _block(tmp_path, make_rtss_ui)
    block.on_activate_item()
    assert block.focus_idx.value == 0
    block.on_item_confirm()  # idx 0 -> toggle
    assert block.lock_enabled is True


@pytest.mark.xfail(
    reason="旧 headless 导航契约：RtssBlock 从未实现 on_item_confirm / on_navigate"
           "（全项目 0 处实现前者，后者只有 power.py 有）。Phase 2 后滑块调节"
           "由 InteractionController 接管，此路径当前无调用方。留档待补。",
    strict=False,
)
def test_navigation_slider_adjust_changes_fps(tmp_path, make_rtss_ui):
    block, _ = _block(tmp_path, make_rtss_ui)
    block.fps = 40
    block.on_activate_item()
    # move focus to the thumb (col 1)
    block.on_navigate(10)
    assert block.focus_idx.value == 1
    block.on_item_confirm()  # enter slider adjust mode
    assert block.slider_adjust is True
    # nudge right -> +5 fps
    block.on_navigate(10)
    assert block.fps == 45


def test_on_theme_change_updates_title_color(tmp_path, make_rtss_ui):
    block, _ = _block(tmp_path, make_rtss_ui)
    accent = {"RtssTitleColor": __import__("ningbingjue.core.color.palette", fromlist=["COLOR_BLUE"]).COLOR_BLUE}
    block.on_theme_change(accent)
    assert block.ui["lbl_rtss_title"].fore_color is not None
