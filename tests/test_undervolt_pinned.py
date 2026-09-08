"""「降压 -30mV」持久固定档 + 开机/休眠恢复自动重钉 的回归测试。

覆盖 4 项卡片改动的核心不变量：

  A. 持久化（pinned_mv 经 config/undervolt.json 存读往返）
  B. 激活后持久化固定档：undervolt_set_pinned 立即钉 -30 + 写回 pinned_mv
     + 与自动降压互斥（enabled 强制 False）
  C. 关闭后归零：undervolt_clear_pinned 立即 reset CO=0 + 清 pinned_mv
  D. 休眠恢复重钉：_reapply_after_resume 把 pinned_mv 再下发一次

B/C/D 直接调 _OverlayWindow 的真实方法（仅用最小子类跳过重型 __init__），
后端接 MockRyzenAdj，零真实 ryzenadj、零子进程。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from typing import Any

import ningbingjue.platform.undervolt as uv
from ningbingjue.core.metrics import MetricSnapshot
from ningbingjue.platform.ryzenadj import MockRyzenAdj
from ningbingjue.ui.app_window import _OverlayWindow as AppWindow


class _FakeSampler:
    """Stand-in for MetricSampler: caller sets .snap fields directly."""
    def __init__(self) -> None:
        self.snap = MetricSnapshot()
    @property
    def snapshot(self) -> MetricSnapshot:
        return self.snap


class _FakeApp:
    """_reapply_after_resume 需要非空 _app 才走重钉分支；这里给最小桩。"""
    def __init__(self) -> None:
        self.blocks: dict[str, Any] = {}
    def fire_hook(self, name: str, *a: Any, **k: Any) -> list:
        return []


class _MinimalPinWindow(AppWindow):
    """只初始化降压相关属性，跳过 _OverlayWindow 重型 UI 装配。

    直接调 QWidget.__init__，避免构建皮肤/面板/计时器；再补上
    undervolt_* 方法所需的几个属性即可复用生产代码路径。
    """
    def __init__(self, cfg_path: Any) -> None:
        from PyQt6.QtWidgets import QWidget
        QWidget.__init__(self)
        self._app = _FakeApp()
        self._uv_cfg_path = cfg_path
        self._uv_controller = None
        self._uv_last: dict[str, Any] = {"state": "", "mv": 0}
        self._uv_sampler = None


def _make_controller(pinned_mv: int = 0, enabled: bool = False):
    ra = MockRyzenAdj()
    backend = uv.RyzenAdjBackend(ra)
    cfg = uv.UndervoltConfig(
        enabled=enabled, heavy_enabled=True, heavy_validated=True,
        offsets={"idle": 0, "light": -20, "heavy": -50},
        hmax=50.0, hmean=25.0, hgpu_hold=55.0, t_light_s=3.0, sticky_s=30.0,
        heavy_confirm_s=0.0, pinned_mv=pinned_mv,
    )
    sampler = _FakeSampler()
    ctrl = uv.UndervoltController(backend, sampler, cfg)
    ctrl._sampler = sampler
    return ctrl, ra


def _applied_mv(ra: MockRyzenAdj) -> int | None:
    if not ra.last_args:
        return None
    return int(ra.last_args[1])


# -- A. 持久化 ----------------------------------------------------------------
def test_pinned_mv_default_zero():
    assert uv.UndervoltConfig().pinned_mv == 0


def test_pinned_mv_persists_roundtrip(tmp_path):
    p = tmp_path / "undervolt.json"
    cfg = uv.UndervoltConfig(enabled=False, pinned_mv=-30,
                             heavy_validated=True)
    cfg.save(p)
    loaded = uv.UndervoltConfig.load(p)
    # 持久值必须原样往返，不被偷偷清零或篡改
    assert loaded.pinned_mv == -30
    assert loaded.enabled is False
    # load 不应把 pinned_mv 重置回默认 0
    assert isinstance(loaded.pinned_mv, int)


# -- B. 激活后钉 -30 + 持久化 + 互斥 -----------------------------------------
def test_set_pinned_applies_and_persists(tmp_path):
    p = tmp_path / "undervolt.json"
    win = _MinimalPinWindow(p)
    ctrl, ra = _make_controller(enabled=True)  # 模拟「自动降压开着」
    win._uv_controller = ctrl

    # 用户点「降压 -30mV」状态按钮 -> 激活
    flags = win.undervolt_set_pinned(-30)

    # 1) 立即把电压钉在 -30（CO=1048526）
    assert _applied_mv(ra) == uv.coall_value(-30)
    # 2) 持久态写入 pinned_mv=-30
    assert ctrl.config.pinned_mv == -30
    # 3) 与自动降压互斥：enabled 被强制关掉
    assert ctrl.config.enabled is False
    assert flags["enabled"] is False
    # 4) 落盘：重读文件仍为 -30（下次开机/恢复能读到）
    reloaded = uv.UndervoltConfig.load(p)
    assert reloaded.pinned_mv == -30
    assert reloaded.enabled is False


# -- C. 关闭后归零 + 清持久态 ------------------------------------------------
def test_clear_pinned_resets_and_clears(tmp_path):
    p = tmp_path / "undervolt.json"
    win = _MinimalPinWindow(p)
    ctrl, ra = _make_controller(pinned_mv=-30, enabled=False)
    win._uv_controller = ctrl

    # 先确认已处于「钉 -30」状态
    assert ctrl.config.pinned_mv == -30

    # 用户再点一次 -> 关闭
    flags = win.undervolt_clear_pinned()

    # 1) 立即把电压复位到 0（CO=1048576）
    assert _applied_mv(ra) == uv.coall_value(0)
    # 2) 清持久态
    assert ctrl.config.pinned_mv == 0
    assert flags["enabled"] is False
    # 3) 落盘：重读文件 pinned_mv 已清零
    reloaded = uv.UndervoltConfig.load(p)
    assert reloaded.pinned_mv == 0


# -- D. 休眠恢复重钉 ----------------------------------------------------------
def test_reapply_after_resume_repins(tmp_path):
    p = tmp_path / "undervolt.json"
    win = _MinimalPinWindow(p)
    ctrl, ra = _make_controller(pinned_mv=-30, enabled=False)
    win._uv_controller = ctrl
    # 模拟「已激活并落盘」
    ctrl.config.save(p)
    ra.last_args = []  # 清空，确认恢复路径确实再次下发

    # 系统从休眠恢复
    win._reapply_after_resume()

    # SMU 已重置，必须再钉一次 -30
    assert _applied_mv(ra) == uv.coall_value(-30)
    assert ctrl.last_applied_mv == -30
    assert win._uv_last["state"] == "pinned"
    assert win._uv_last["mv"] == -30


# -- 互斥：开「自动降压」应清掉持久固定档 ------------------------------------
def test_enable_auto_clears_pinned(tmp_path):
    p = tmp_path / "undervolt.json"
    win = _MinimalPinWindow(p)
    ctrl, ra = _make_controller(pinned_mv=-30, enabled=False)
    win._uv_controller = ctrl

    # 用户在「降压 -30mV」激活状态下又开「自动降压」
    win.undervolt_set_flag("enabled", True)

    # 自动降压开启 -> 持久固定档被互斥清掉
    assert ctrl.config.pinned_mv == 0
    assert ctrl.config.enabled is True
    reloaded = uv.UndervoltConfig.load(p)
    assert reloaded.pinned_mv == 0


if __name__ == "__main__":
    import sys
    for name in dir(sys.modules[__name__]):
        if name.startswith("test_"):
            globals()[name]()
    print("OK - all undervolt pinned tests passed")
