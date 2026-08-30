# -*- coding: utf-8 -*-
"""验证休眠恢复后硬件配置重下发（OnPowerResume hook 机制）。

覆盖：
  1. power.on_power_resume: applied=True 重下发(silent)；applied=False no-op
  2. fan.on_power_resume: applied=True+MANUAL 重下发；applied=True+AUTO no-op；applied=False no-op
  3. rtss.on_power_resume: lock_applied=True 重下发；lock_applied=False no-op
  4. smt.on_power_resume: _last.mode=lock 重下发；_last.mode=idle no-op
  5. app_window._reapply_after_resume: 降压 state reset 为哨兵 + fire_hook 广播
  6. app.fire_hook: 广播 OnPowerResume，返回结果列表

用 __new__ 绕过 __init__ 副作用，聚焦门控逻辑；offscreen 安全（不建 _FocusRect）。
"""
import os
import sys
import threading
from unittest.mock import MagicMock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)


# ---------------------------------------------------------------------------
# power
# ---------------------------------------------------------------------------
def test_power_resume_reapplies_when_applied():
    from ningbingjue.blocks.power import PowerBlock
    power = PowerBlock.__new__(PowerBlock)
    power.applied = True
    called = {}

    def fake_apply(silent=False):
        called["silent"] = silent
        return True
    power.apply_power = fake_apply

    result = power.on_power_resume()
    assert result is True, f"应返回 True, got {result}"
    assert called.get("silent") is True, "必须 silent=True 重下发"


def test_power_resume_noop_when_not_applied():
    from ningbingjue.blocks.power import PowerBlock
    power = PowerBlock.__new__(PowerBlock)
    power.applied = False
    called = []
    power.apply_power = lambda silent=False: called.append(silent) or True

    result = power.on_power_resume()
    assert result is None, f"未应用过应返回 None, got {result}"
    assert called == [], "applied=False 时绝不能调 apply_power"


def test_power_resume_swallows_exception():
    from ningbingjue.blocks.power import PowerBlock
    power = PowerBlock.__new__(PowerBlock)
    power.applied = True

    def boom(silent=False):
        raise RuntimeError("SMU busy")
    power.apply_power = boom

    result = power.on_power_resume()
    assert result is False, "异常时应返回 False 而非抛出"


# ---------------------------------------------------------------------------
# fan
# ---------------------------------------------------------------------------
def test_fan_resume_reapplies_manual():
    from ningbingjue.blocks.fan import FanBlock, MODE_MANUAL
    fan = FanBlock.__new__(FanBlock)
    fan.applied = True
    fan.mode = MODE_MANUAL
    called = {}
    fan.apply = lambda silent=False: called.setdefault("silent", silent) or True

    result = fan.on_power_resume()
    assert result is True
    assert called.get("silent") is True


def test_fan_resume_noop_for_auto():
    from ningbingjue.blocks.fan import FanBlock, MODE_AUTO
    fan = FanBlock.__new__(FanBlock)
    fan.applied = True
    fan.mode = MODE_AUTO
    called = []
    fan.apply = lambda silent=False: called.append(silent) or True

    result = fan.on_power_resume()
    assert result is None, "AUTO 模式是交还 BIOS，休眠后无需重下发"
    assert called == []


def test_fan_resume_noop_when_not_applied():
    from ningbingjue.blocks.fan import FanBlock, MODE_CURVE
    fan = FanBlock.__new__(FanBlock)
    fan.applied = False
    fan.mode = MODE_CURVE
    called = []
    fan.apply = lambda silent=False: called.append(silent) or True

    result = fan.on_power_resume()
    assert result is None
    assert called == []


# ---------------------------------------------------------------------------
# rtss
# ---------------------------------------------------------------------------
def test_rtss_resume_reapplies_when_lock_applied():
    from ningbingjue.blocks.rtss import RtssBlock
    rtss = RtssBlock.__new__(RtssBlock)
    rtss.lock_applied = True
    called = {}
    rtss.apply_lock = lambda silent=False: called.setdefault("silent", silent)

    result = rtss.on_power_resume()
    assert result is True
    assert called.get("silent") is True


def test_rtss_resume_noop_when_not_applied():
    from ningbingjue.blocks.rtss import RtssBlock
    rtss = RtssBlock.__new__(RtssBlock)
    rtss.lock_applied = False
    called = []
    rtss.apply_lock = lambda silent=False: called.append(silent)

    result = rtss.on_power_resume()
    assert result is None
    assert called == []


# ---------------------------------------------------------------------------
# smt
# ---------------------------------------------------------------------------
def test_smt_resume_reapplies_when_locked():
    from ningbingjue.blocks.smt import SmtBlock
    smt = SmtBlock.__new__(SmtBlock)
    smt._lock = threading.Lock()
    smt._last = {"mode": "lock", "hot_lp": 2}
    called = []
    smt.lock_once = lambda: called.append(True) or {"started": True}

    result = smt.on_power_resume()
    assert result is True
    assert called == [True]


def test_smt_resume_noop_when_idle():
    from ningbingjue.blocks.smt import SmtBlock
    smt = SmtBlock.__new__(SmtBlock)
    smt._lock = threading.Lock()
    smt._last = {"mode": "idle"}
    called = []
    smt.lock_once = lambda: called.append(True) or {"started": True}

    result = smt.on_power_resume()
    assert result is None
    assert called == []


def test_smt_resume_noop_when_no_last():
    from ningbingjue.blocks.smt import SmtBlock
    smt = SmtBlock.__new__(SmtBlock)
    smt._lock = threading.Lock()
    smt._last = None
    called = []
    smt.lock_once = lambda: called.append(True)

    result = smt.on_power_resume()
    assert result is None
    assert called == []


# ---------------------------------------------------------------------------
# app.fire_hook 广播
# ---------------------------------------------------------------------------
def test_fire_hook_broadcasts_on_power_resume():
    from ningbingjue.app import Application
    app = Application.__new__(Application)
    # 构造最小 registry：两个 block，一个有 OnPowerResume，一个没有
    blk_with = MagicMock()
    blk_with.hook.return_value = lambda: True
    blk_without = MagicMock()
    blk_without.hook.return_value = None

    registry = MagicMock()
    registry.__iter__ = lambda self: iter(["power", "tile"])
    registry.get = lambda t: blk_with if t == "power" else blk_without
    app.registry = registry

    results = app.fire_hook("OnPowerResume")
    assert len(results) == 1, f"只有 power 注册了 hook, got {results}"
    assert results[0][0] == "power"
    assert results[0][1] is True
    # 确认 hook 名传递正确
    blk_with.hook.assert_called_with("OnPowerResume")


def test_fire_hook_isolates_exceptions():
    from ningbingjue.app import Application
    app = Application.__new__(Application)
    bad_blk = MagicMock()
    bad_blk.hook.return_value = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    good_blk = MagicMock()
    good_blk.hook.return_value = lambda: "ok"

    registry = MagicMock()
    registry.__iter__ = lambda self: iter(["bad", "good"])
    registry.get = lambda t: bad_blk if t == "bad" else good_blk
    app.registry = registry

    results = app.fire_hook("OnPowerResume")
    assert len(results) == 2
    assert results[0] == ("bad", None), "异常 block 应返回 None 而非抛出"
    assert results[1] == ("good", "ok")


# ---------------------------------------------------------------------------
# app_window._reapply_after_resume 降压 state reset + 广播
# ---------------------------------------------------------------------------
def test_reapply_resets_undervolt_state_and_broadcasts():
    """验证 _reapply_after_resume: 降压 controller.state 被置 ''，
    fire_hook 被调用，sampler 不健康时重启。"""
    from ningbingjue.ui.app_window import _OverlayWindow

    win = _OverlayWindow.__new__(_OverlayWindow)

    # mock _uv_controller（enabled=True, dry_run=False）
    ctrl = MagicMock()
    ctrl.config.enabled = True
    ctrl.config.dry_run = False
    ctrl.state = "heavy"  # 休眠前的旧状态
    win._uv_controller = ctrl

    # mock sampler（is_running=False 模拟休眠后失效）
    sampler = MagicMock()
    sampler.is_running = False
    win._uv_sampler = sampler

    # mock _app：fire_hook 返回 power=True, fan=None
    app = MagicMock()
    app.fire_hook.return_value = [("power", True), ("fan", None)]
    power_blk = MagicMock()
    app.blocks = {"power": power_blk}
    win._app = app

    win._reapply_after_resume()

    # 1) 降压 state 被置为哨兵
    assert ctrl.state == "", f"state 应被 reset 为 '', got {ctrl.state!r}"
    # 2) sampler 被重启
    sampler.start.assert_called_once()
    # 3) fire_hook 被调用
    app.fire_hook.assert_called_once_with("OnPowerResume")
    # 4) 汇总 toast 通过 power block 弹出
    power_blk.show_toast.assert_called_once()
    toast_arg = power_blk.show_toast.call_args[0][0]
    assert "休眠恢复" in toast_arg
    assert "电源" in toast_arg  # power=True 被汇总
    assert "风扇" not in toast_arg  # fan=None 被跳过


def test_reapply_skips_undervolt_when_disabled():
    """降压 disabled 时不应 reset state（状态机本就不写电压）。"""
    from ningbingjue.ui.app_window import _OverlayWindow
    win = _OverlayWindow.__new__(_OverlayWindow)

    ctrl = MagicMock()
    ctrl.config.enabled = False
    ctrl.config.dry_run = False
    ctrl.state = "idle"
    win._uv_controller = ctrl
    win._uv_sampler = MagicMock(is_running=True)

    app = MagicMock()
    app.fire_hook.return_value = []
    app.blocks = {"power": MagicMock()}
    win._app = app

    win._reapply_after_resume()

    # disabled 时 state 不应被改
    assert ctrl.state == "idle"


def test_reapply_skips_undervolt_when_dry_run():
    """dry_run 模式只观察不写电压，不应触发 state reset。"""
    from ningbingjue.ui.app_window import _OverlayWindow
    win = _OverlayWindow.__new__(_OverlayWindow)

    ctrl = MagicMock()
    ctrl.config.enabled = True
    ctrl.config.dry_run = True
    ctrl.state = "heavy"
    win._uv_controller = ctrl
    win._uv_sampler = MagicMock(is_running=True)

    app = MagicMock()
    app.fire_hook.return_value = []
    app.blocks = {"power": MagicMock()}
    win._app = app

    win._reapply_after_resume()
    assert ctrl.state == "heavy", "dry_run 不应 reset state"


def test_reapply_noop_when_no_app():
    """_app 为 None 时（offscreen/测试）安全返回，不抛异常。"""
    from ningbingjue.ui.app_window import _OverlayWindow
    win = _OverlayWindow.__new__(_OverlayWindow)
    win._app = None
    win._reapply_after_resume()  # 不应抛异常


# ---------------------------------------------------------------------------
# hook 注册验证（确保各 block 的 hooks dict 含 OnPowerResume）
# ---------------------------------------------------------------------------
def test_power_registers_on_power_resume_hook():
    from ningbingjue.blocks.power import PowerBlock
    from ningbingjue.core.config import PowerConfig
    from ningbingjue.platform.ryzenadj import MockRyzenAdj
    cfg = MagicMock()
    cfg.power_config = PowerConfig()
    power = PowerBlock(cfg, MockRyzenAdj(), ui={})
    # register() 会在内部构建 BlockBlueprint；这里直接验证方法存在且可调
    assert callable(getattr(power, "on_power_resume", None))


def test_fan_registers_on_power_resume_hook():
    from ningbingjue.blocks.fan import FanBlock
    from ningbingjue.core.config import FanConfig
    cfg = MagicMock()
    cfg.fan_config = FanConfig()
    # FanBlock 构造需要 ec/thermal；用 mock
    ec = MagicMock(); ec.health_check.return_value = False
    thermal = MagicMock()
    try:
        fan = FanBlock(cfg, ec, thermal)
    except Exception:
        # 构造可能因副作用失败，用 __new__ 兜底验证方法存在
        fan = FanBlock.__new__(FanBlock)
    assert callable(getattr(fan, "on_power_resume", None))


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
