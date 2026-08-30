"""Undervolt controller tests (headless, MockRyzenAdj).

Locks the state machine + the user's final thresholds:
    cpu_heavy = max_core >= 50 OR mean_core >= 25  -> HEAVY (-50)
    hold HEAVY while gpu >= 55
    light_streak >= 3s (no cpu_heavy, gpu low)      -> LIGHT (-20)
    not in game (sticky 30s)                        -> IDLE (0)

Note: the helper builds controllers with heavy_confirm_s=0 and
heavy_validated=True so the *confirm / validation* gates don't mask the
core state-machine assertions. The confirm timing and the validated gate
are exercised by dedicated tests below.
"""
from __future__ import annotations

import time
from typing import Any

import ningbingjue.platform.undervolt as uv
from ningbingjue.core.metrics import MetricSnapshot
from ningbingjue.platform.ryzenadj import MockRyzenAdj


class _FakeSampler:
    """Stand-in for MetricSampler: caller sets .snap fields directly."""
    def __init__(self) -> None:
        self.snap = MetricSnapshot()
    @property
    def snapshot(self) -> MetricSnapshot:
        return self.snap


def _controller(enabled=True, heavy_enabled=True, game=True,
                maxc=0.0, meanc=0.0, gpu=0.0):
    ra = MockRyzenAdj()
    backend = uv.RyzenAdjBackend(ra)
    cfg = uv.UndervoltConfig(
        enabled=enabled, heavy_enabled=heavy_enabled,
        offsets={"idle": 0, "light": -20, "heavy": -50},
        hmax=50.0, hmean=25.0, hgpu_hold=55.0, t_light_s=3.0, sticky_s=30.0,
        heavy_confirm_s=0.0,   # 测试聚焦状态机，关闭确认时长
        heavy_validated=True,  # 测试聚焦 -50 行为，默认视为已验证
    )
    sampler = _FakeSampler()
    sampler.snap = MetricSnapshot(max_core=maxc, mean_core=meanc,
                                  gpu_pct=gpu, ok=True)
    ctrl = uv.UndervoltController(backend, sampler, cfg)
    ctrl._sampler = sampler

    def _fg(wl):
        return (1234, "game.exe") if game else None
    uv.find_foreground_game = _fg  # type: ignore[assignment]
    return ctrl, ra, sampler


def _set(sampler, maxc=None, meanc=None, gpu=None):
    if maxc is not None:
        sampler.snap.max_core = maxc
    if meanc is not None:
        sampler.snap.mean_core = meanc
    if gpu is not None:
        sampler.snap.gpu_pct = gpu


def _applied_mv(ra: MockRyzenAdj) -> int | None:
    if not ra.last_args:
        return None
    return int(ra.last_args[1])


def test_coall_encoding():
    assert uv.coall_value(-50) == 1048526
    assert uv.coall_value(-20) == 1048556
    assert uv.coall_value(0) == 1048576


def test_idle_when_not_in_game():
    ctrl, ra, _ = _controller(game=False, maxc=90.0)
    st = ctrl.tick(1.0)
    assert st == uv.IDLE
    assert _applied_mv(ra) == 1048576  # CO=0


def test_heavy_on_max_core():
    ctrl, ra, _ = _controller(game=True, maxc=70.0, gpu=10.0)
    st = ctrl.tick(1.0)
    assert st == uv.HEAVY
    assert _applied_mv(ra) == uv.coall_value(-50)


def test_heavy_on_mean_core_balanced():
    # 均衡型（原神）：单核不高但均值触线 -> HEAVY
    ctrl, ra, _ = _controller(game=True, maxc=30.0, meanc=28.0, gpu=10.0)
    st = ctrl.tick(1.0)
    assert st == uv.HEAVY
    assert _applied_mv(ra) == uv.coall_value(-50)


def test_hold_heavy_while_gpu_high():
    # 先升到 HEAVY，再让 CPU 掉但 GPU 仍高 -> 应保持 HEAVY（防抖）
    ctrl, ra, sampler = _controller(game=True, maxc=70.0, gpu=80.0)
    assert ctrl.tick(1.0) == uv.HEAVY
    _set(sampler, maxc=5.0, meanc=5.0, gpu=80.0)
    st = ctrl.tick(1.0)
    assert st == uv.HEAVY
    # 仍 -50，没有降到 -20
    assert _applied_mv(ra) == uv.coall_value(-50)


def test_downgrade_to_light_after_debounce():
    ctrl, ra, sampler = _controller(game=True, maxc=70.0, gpu=80.0)
    assert ctrl.tick(1.0) == uv.HEAVY
    # CPU 掉、GPU 也掉 -> 累计 light_streak，满 3s 才落 LIGHT
    _set(sampler, maxc=5.0, meanc=5.0, gpu=5.0)
    assert ctrl.tick(1.0) == uv.HEAVY      # 1s
    assert ctrl.tick(1.0) == uv.HEAVY      # 2s
    st = ctrl.tick(1.0)                     # 3s -> 落 LIGHT
    assert st == uv.LIGHT
    assert _applied_mv(ra) == uv.coall_value(-20)


def test_sticky_keeps_game_state_30s():
    ctrl, ra, sampler = _controller(game=True, maxc=70.0, gpu=80.0)
    assert ctrl.tick(1.0) == uv.HEAVY
    # 游戏窗口丢失，但 30s 内仍认领
    uv.find_foreground_game = lambda wl: None  # type: ignore[assignment]
    _set(sampler, maxc=70.0, gpu=80.0)
    # 把 last_game_ts 推到 10s 前
    ctrl.last_game_ts = time.monotonic() - 10.0
    assert ctrl.tick(1.0) == uv.HEAVY
    assert _applied_mv(ra) == uv.coall_value(-50)


def test_game_lost_after_sticky_returns_idle():
    ctrl, ra, sampler = _controller(game=True, maxc=70.0, gpu=80.0)
    assert ctrl.tick(1.0) == uv.HEAVY
    uv.find_foreground_game = lambda wl: None  # type: ignore[assignment]
    ctrl.last_game_ts = time.monotonic() - 40.0  # 超过 sticky
    _set(sampler, maxc=70.0, gpu=80.0)
    st = ctrl.tick(1.0)
    assert st == uv.IDLE
    assert _applied_mv(ra) == uv.coall_value(0)


def test_heavy_disabled_falls_back_to_light():
    # heavy_enabled=False：即使判 HEAVY 也只给 LIGHT
    ctrl, ra, _ = _controller(enabled=True, heavy_enabled=False,
                              game=True, maxc=70.0, gpu=10.0)
    st = ctrl.tick(1.0)
    assert st == uv.HEAVY  # 状态仍是 heavy 判定
    assert _applied_mv(ra) == uv.coall_value(-20)  # 但只落 -20


def test_disabled_does_not_write_ryzenadj():
    ctrl, ra, _ = _controller(enabled=False, heavy_enabled=True,
                             game=True, maxc=70.0, gpu=10.0)
    st = ctrl.tick(1.0)
    assert st == uv.HEAVY
    assert ra.last_args == []  # 没写 ryzenadj


def test_metrics_import_is_pyqt_free():
    # metrics 模块不得依赖 PyQt（独立脚本/headless 可 import）
    import ningbingjue.core.metrics as m
    assert hasattr(m, "MetricSampler")
    assert hasattr(m, "MetricSnapshot")


def test_dry_run_logs_and_never_writes(tmp_path):
    # dry_run 模式：逐 tick 记录决策，但绝不写 ryzenadj；
    # 且 heavy_enabled=False 时把 HEAVY 门控成 LIGHT 也如实记进日志。
    ra = MockRyzenAdj()
    backend = uv.RyzenAdjBackend(ra, dry_run=True)
    cfg = uv.UndervoltConfig(
        enabled=False, heavy_enabled=False, dry_run=True,
        offsets={"idle": 0, "light": -20, "heavy": -50},
        hmax=50.0, hmean=25.0, hgpu_hold=55.0, t_light_s=3.0, sticky_s=30.0,
        heavy_confirm_s=0.0,   # 关闭确认时长，聚焦门控/防抖日志
        heavy_validated=True,
    )
    sampler = _FakeSampler()
    log_path = tmp_path / "dryrun.log"
    ctrl = uv.UndervoltController(backend, sampler, cfg, dry_log_path=log_path)
    uv.find_foreground_game = lambda wl: (1234, "game.exe")  # type: ignore[assignment]

    # 1) 重载：单核拉满 -> HEAVY（heavy 未放行，日志记 eff_mv=-20 + 门控提示）
    sampler.snap = MetricSnapshot(max_core=80.0, mean_core=40.0, gpu_pct=70.0, ok=True)
    assert ctrl.tick(1.0) == uv.HEAVY

    # 2) 开菜单：CPU 掉、GPU 仍高 -> 保持 HEAVY（gpu_hold 防抖）
    sampler.snap = MetricSnapshot(max_core=5.0, mean_core=5.0, gpu_pct=70.0, ok=True)
    assert ctrl.tick(1.0) == uv.HEAVY

    # 3) 切回轻载：CPU/GPU 都低 -> 累计满 3s 落 LIGHT
    sampler.snap = MetricSnapshot(max_core=10.0, mean_core=10.0, gpu_pct=10.0, ok=True)
    assert ctrl.tick(1.0) == uv.HEAVY   # 1s
    assert ctrl.tick(1.0) == uv.HEAVY   # 2s
    assert ctrl.tick(1.0) == uv.LIGHT   # 3s -> LIGHT

    # 4) 失焦超 sticky -> IDLE（即便指标全红）
    uv.find_foreground_game = lambda wl: None  # type: ignore[assignment]
    ctrl.last_game_ts = time.monotonic() - 40.0
    sampler.snap = MetricSnapshot(max_core=90.0, mean_core=90.0, gpu_pct=90.0, ok=True)
    assert ctrl.tick(1.0) == uv.IDLE

    ctrl.close()

    # 绝不写 ryzenadj
    assert ra.last_args == []
    # 日志含全部关键转换与门控/防抖提示
    text = log_path.read_text(encoding="utf-8")
    assert "-> HEAVY" in text
    assert "-> LIGHT" in text
    assert "-> IDLE" in text
    assert "HEAVY 未放行 -> 用 LIGHT" in text
    assert "gpu_hold" in text


def test_heavy_requires_confirm_duration():
    # heavy_confirm_s=2.0：单核瞬尖峰(1 tick)不应触发 HEAVY；持续 >=2s 才升。
    ra = MockRyzenAdj()
    backend = uv.RyzenAdjBackend(ra)
    cfg = uv.UndervoltConfig(
        enabled=True, heavy_enabled=True, heavy_validated=True,
        offsets={"idle": 0, "light": -20, "heavy": -50},
        hmax=50.0, hmean=25.0, hgpu_hold=55.0, t_light_s=3.0, sticky_s=30.0,
        heavy_confirm_s=2.0,
    )
    sampler = _FakeSampler()
    sampler.snap = MetricSnapshot(max_core=80.0, mean_core=40.0, gpu_pct=10.0, ok=True)
    ctrl = uv.UndervoltController(backend, sampler, cfg)
    uv.find_foreground_game = lambda wl: (1234, "game.exe")  # type: ignore[assignment]

    # 1) 单 tick 尖峰：尚未确认 -> 维持 IDLE（绝不写 -50）
    st1 = ctrl.tick(1.0)
    assert st1 == uv.IDLE
    assert _applied_mv(ra) is None or _applied_mv(ra) == uv.coall_value(0)

    # 2) 持续重载第 2 个 tick：heavy_streak=2.0>=2.0 -> HEAVY(-50)
    st2 = ctrl.tick(1.0)
    assert st2 == uv.HEAVY
    assert _applied_mv(ra) == uv.coall_value(-50)

    # 3) 尖峰只持续 1 tick 后回落：heavy_streak 清零，不进 HEAVY
    ctrl2, ra2, s2 = _controller(enabled=True, heavy_enabled=True,
                                 game=True, maxc=0.0, meanc=0.0, gpu=0.0)
    # 手动覆盖确认时长
    ctrl2.config.heavy_confirm_s = 2.0
    _set(s2, maxc=80.0, meanc=40.0, gpu=10.0)
    assert ctrl2.tick(1.0) == uv.IDLE            # 1 tick 尖峰，不确认
    _set(s2, maxc=5.0, meanc=5.0, gpu=5.0)       # 立即回落
    assert ctrl2.tick(1.0) == uv.IDLE            # 仍不 HEAVY


def test_heavy_blocked_until_validated():
    # heavy_validated=False：即便 heavy_enabled=True 且判 HEAVY，也只给 -20 并告警
    ra = MockRyzenAdj()
    backend = uv.RyzenAdjBackend(ra)
    cfg = uv.UndervoltConfig(
        enabled=True, heavy_enabled=True, heavy_validated=False,
        offsets={"idle": 0, "light": -20, "heavy": -50},
        hmax=50.0, hmean=25.0, hgpu_hold=55.0, t_light_s=3.0, sticky_s=30.0,
        heavy_confirm_s=0.0,   # 关确认，聚焦验证门控
    )
    sampler = _FakeSampler()
    sampler.snap = MetricSnapshot(max_core=80.0, mean_core=40.0, gpu_pct=10.0, ok=True)
    ctrl = uv.UndervoltController(backend, sampler, cfg)
    uv.find_foreground_game = lambda wl: (1234, "game.exe")  # type: ignore[assignment]

    st = ctrl.tick(1.0)
    assert st == uv.HEAVY                       # 状态判定仍是 HEAVY
    assert _applied_mv(ra) == uv.coall_value(-20)  # 但只落 -20（未验证门控）


def test_reapply_after_validation_change():
    """heavy_validated 改变后 reapply 立即按新门控重应用当前档位。

    验证修复：此前 heavy_validated 无 UI 入口，用户开了「允许 -50mV 重载档」
    仍只给 -20 且无从知晓原因。现在 UI 可改 heavy_validated，改后 reapply 应
    立即下发 -50（开验证）/ 退回 -20（关验证）。
    """
    ra = MockRyzenAdj()
    backend = uv.RyzenAdjBackend(ra)
    cfg = uv.UndervoltConfig(
        enabled=True, heavy_enabled=True, heavy_validated=False,
        offsets={"idle": 0, "light": -20, "heavy": -50},
        hmax=50.0, hmean=25.0, hgpu_hold=55.0, t_light_s=3.0, sticky_s=30.0,
        heavy_confirm_s=0.0,
    )
    sampler = _FakeSampler()
    sampler.snap = MetricSnapshot(max_core=80.0, mean_core=40.0, gpu_pct=10.0, ok=True)
    ctrl = uv.UndervoltController(backend, sampler, cfg)
    uv.find_foreground_game = lambda wl: (1234, "game.exe")  # type: ignore[assignment]

    # 1) heavy_validated=False：判 HEAVY 但只给 -20
    assert ctrl.tick(1.0) == uv.HEAVY
    assert _applied_mv(ra) == uv.coall_value(-20)

    # 2) 用户在 UI 开「-50 验证」-> heavy_validated=True，reapply 立即下发 -50
    cfg.heavy_validated = True
    ctrl.reapply()
    assert _applied_mv(ra) == uv.coall_value(-50)

    # 3) 再关回去 -> reapply 立即退回 -20（安全门控可逆）
    cfg.heavy_validated = False
    ctrl.reapply()
    assert _applied_mv(ra) == uv.coall_value(-20)


