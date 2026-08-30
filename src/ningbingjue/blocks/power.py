"""power block — Ryzen APU TDP + GPU clock control via ryzenadj.

Faithful logic port of ``blocks/power.ps1``.  Model: a TDP(W) slider +
a GPU(MHz) slider, each with an independent lock button, plus a GPU
float mode and an Apply button.  Navigation is a 2×3 matrix
(每行 = 滑块 + 锁钮; 末行 = GPU浮动 + Apply).  All ryzenadj
invocation goes through the ``RyzenAdj`` adapter, whose ``invoke``
preserves the original's quirk of treating exit code ``-1073741819``
as success.  GPU-utilization reads (for float) go through
``read_gpu_utilization`` (mockable).
"""

from __future__ import annotations
from ..ui.panels import _show_block_toast

import math
import os
import sys
import threading
import time
from typing import Any

from ..core.color.api import (
    get_button_state,
    get_slider_colors,
)
from ..core.color.palette import COLOR_GREEN, COLOR_MAUVE, COLOR_RED, COLOR_YELLOW
from ..core.color.rgb import Color
from ..core.color.semantic import get_theme_accent
from ..core.config import (
    PowerConfig,
)
from ..core.gui import _gui_method
from ..core.float_params import (
    EMA_ALPHA,
    METRIC_STALE_SECS,
    is_gpu_counter_corrupted,
)
from ..core.metrics import MetricSampler
from ..core.navigation import (
    FocusHolder,
    clear_block_focus,
    navigate_matrix,
    set_control_focus_visual,
)

from ..platform.ryzenadj import RyzenAdj, _GpuUtilMonitor, MockRyzenAdj
from .base import BlockBlueprint

# --- GPU 自动调频决策日志（诊断用）-----------------------------------------
# 记录每次浮动决策的上下文：GPU 占用率、当前频率、天花板、决策动作、原因。
# 与 ryzenadj_invoke.log 配合看：ryzenadj_invoke.log 记录"发了什么"，
# gpu_auto.log 记录"为什么发"。
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_GPU_AUTO_LOG = os.path.join(_PROJECT_ROOT, "logs", "gpu_auto.log")
# P3-14：决策日志从多个线程（浮动 worker / reaper 回调 / GUI toggle）追加同一文件，
# 模块级写锁保证行间不 interleave（Windows O_APPEND 只保证原子 append，不保证行）。
_FLOAT_LOG_LOCK = threading.Lock()


def _log_float(tag: str, **kwargs: Any) -> None:
    """追加一条 GPU 浮动决策记录；写失败静默忽略，不影响主逻辑。"""
    try:
        os.makedirs(os.path.dirname(_GPU_AUTO_LOG), exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        parts = " | ".join(f"{k}={v}" for k, v in kwargs.items())
        with _FLOAT_LOG_LOCK:
            with open(_GPU_AUTO_LOG, "a", encoding="utf-8", errors="ignore") as fh:
                fh.write(f"[{ts}] {tag} | {parts}\n")
    except Exception:  # noqa: BLE001
        pass


def _float_guarded(fn):
    """Wrap a method so its whole body runs under ``self._float_lock`` (RLock).

    Lets the GPU-float logic be driven concurrently by the background
    MetricSampler thread (auto-lock check) and the GUI thread (toggle/load)
    without data races.  RLock → safe to nest (e.g. ``_on_sampler_sample``
    already holds the lock when it calls the decorated ``gpu_float_tick``).
    """
    import functools

    @functools.wraps(fn)
    def _wrapper(self, *args, **kwargs):
        with self._float_lock:
            return fn(self, *args, **kwargs)
    return _wrapper


class _Btn:
    def __init__(self, col: int = 0, row: int = 0) -> None:
        self.text = ""
        self._colors: tuple | None = None
        self.focused = False
        self.fore_color: Color | None = None
        self.border_color: Color | None = None
        self.border_thickness: int | None = None
        self.ctrl_col = col
        self.ctrl_row = row
        self.value = 0
        # navigate_matrix 用 tag.col / tag.row 做 2D 定位（见 core.navigation.
        # _extract_position）。这里把 tag 指向自身并暴露 col/row 属性，
        # 使 PowerBlock 的矩阵导航（上下左右）能正确工作。
        self.tag = self

    @property
    def col(self) -> int:
        return self.ctrl_col

    @property
    def row(self) -> int:
        return self.ctrl_row

    def set_colors(self, *args: Any) -> None:
        self._colors = tuple(args)

    def set_focus_visual(self, focused: bool) -> None:
        self.focused = focused

    def invalidate(self) -> None:
        pass


class PowerBlock:
    TDP_MIN = 6
    TDP_MAX = 30
    GPU_MIN = 400
    GPU_MAX = 1400
    GPU_FLOOR = 400
    # 抬离地板(400MHz)参数：卡在地板且负载确实高时，一次性抬到「预热地板」并稳住，
    # 解决旧逻辑要等 util≥92–98% 才肯抬、导致「卡在 0.4ghz 不动」的问题。
    # 施密特迟滞：地板脱离用 >=85%、分级降频用 <85%，方向互斥，稳定负载只落一侧，
    # 杜绝 400↔600 时钟抖动；85~92% 为缓冲带（不升不降），进一步防边界抖。
    FLOAT_FLOOR_RAISE_THRESHOLD = 85.0   # 地板脱离阈值（≥此利用率才肯离开 400）
    FLOAT_ACTIVE_FLOOR = 600             # 有负载时的「预热地板」：锁地板+高负载立即抬到此处并稳住
    FLOAT_MIN_UP_STEP = 50               # 升频最小步长，防低频步长被 50 对齐吞掉
    FLOAT_DOWN_DEBOUNCE_TICKS = 3        # 降频防抖：util_peak 需连续 N 拍落在降频区间才降一档；每降一档后重新计 N 拍（逐档确认），防单拍毛刺/负载抖动误降
    FLOAT_UP_DEBOUNCE_TICKS = 2          # 升频防抖：离开地板/饱和抬频需连续 N 拍高负载确认，防单拍尖峰误升与 400↔600 抖动
    FLOAT_COOLDOWN_S = 1.0
    FLOAT_RESEND_S = 5.0
    # GPU 浮动决策 worker 的轮询周期（秒）：独立后台线程固定周期消费最新采样快照，
    # 不再占用 MetricSampler 采样回调线程（P1-②，2026-08-20 审查修复）。
    FLOAT_WORKER_INTERVAL_S = 1.5
    # 跨进程互斥持续被占（另一凝冰结魄实例/别的工具在驱动 WinRing0）检测：
    # 连续 busy 达到阈值即判为「长期占用」——停止每拍锤锁（每拍空等 1.5s 互斥
    # 超时，采样线程被拖慢、且无限重试无意义），退避后低频重试，并挂起告警让
    # UI 提示用户关闭重复实例。这是「GPU 浮动卡死锁频上限」的隐蔽根因之一
    # （日志铁证：freq 恒 907 + applied 707↔907 振荡 + circuit=False）。
    GPU_BUSY_STREAK_WARN = 5         # 连续 busy/busy-cross-process 达到此数
    GPU_BUSY_BACKOFF_S = 5.0         # 触发后的退避时长（秒）：期间不再尝试下发
    GPU_BUSY_WARN_COOLDOWN_S = 60.0  # 同一告警 toast 的最小间隔（防刷屏）
    # EMA 平滑系数：与 rtss 自动锁频共用同一 α（core.float_params.EMA_ALPHA），
    # 保证两块对 GPU 抖动的处理一致，调参只改一处。
    FLOAT_EMA_ALPHA = EMA_ALPHA
    FLOAT_TARGET_PCT = 90.0
    FLOAT_UP_THRESHOLD = 92.0
    FLOAT_URGENT_THRESHOLD = 98.0
    # GPU 利用率读取新鲜度窗口（秒）。超过此窗口没有任何有效采样 => 视为
    # 信号不可用，tick 保守保持当前频率，避免基于冻结/损坏数据误决策。
    # 与 rtss 自动锁帧共用同一窗口（core.float_params.METRIC_STALE_SECS）。
    GPU_UTIL_STALE_SECS = METRIC_STALE_SECS
    # P2-7（2026-08-20 审查）：GPU 利用率信号持续不可用达到此秒数 → 自动禁用浮动
    # 并写回上限，避免「信号坏 → 永远 hold 在当前频率」的静默停摆。短时不可用
    # （采样器 EOF 3s 重启等）不触发；计数器损坏的 lodctr 引导仍由
    # _maybe_warn_gpu_counter_corrupted 负责。
    GPU_UNKNOWN_HOLD_MAX_S = 60.0

    def __init__(self, config: Any, ryzenadj: RyzenAdj, ui: dict[str, Any] | None = None) -> None:
        self.config = config
        self.ryzenadj = ryzenadj
        self.ui: dict[str, Any] = dict(ui or {})
        self.theme_color = get_theme_accent()

        # 注册 silent（GPU 浮动）下发结果回调：reaper 判定成功/失败后，用「硬件已
        # 确认频率」校正 gpu_current_freq（显示 + 算法 current），消除乐观目标值
        # 与实际硬件的发散（详见 _on_gpu_sent）。仅对真实 RyzenAdj / Mock 都安全
        # （回调只在 reaper 路径触发，Mock 无 reaper 故不会被调用）。
        try:
            self.ryzenadj.on_silent_sent = self._on_gpu_sent
        except Exception:  # noqa: BLE001
            pass

        # 启动即拉起 GPU 利用率直连监控，并触发一次读取以写 gpu_util.log 头部。
        # 目的：让 gpu_util.log 在「新代码 + 应用启动」时就非空；若日志仍空，
        # 可一眼判定为旧 ryzenadj.py / 陈旧 __pycache__ 在生效（与浮动开关无关）。
        # 仅对真实 RyzenAdj 生效（Mock 在测试里不启动监控、不写日志）。
        if sys.platform == "win32" and not isinstance(ryzenadj, MockRyzenAdj):
            try:
                _GpuUtilMonitor.instance().ensure_started()
                ryzenadj.read_gpu_utilization()
            except Exception:  # noqa: BLE001
                pass

        pc = getattr(self.config, "power_config", None) or PowerConfig()
        self.tdp = pc.tdp
        self.gpu = pc.gpu
        self.tdp_lock = pc.tdp_lock
        self.gpu_lock = pc.gpu_lock
        self.gpu_float_enabled = pc.gpu_float_enabled

        self.applied = False
        self.focus_idx = FocusHolder(0)
        self.in_item_mode = False
        self.slider_adjust = False
        self.adjust_target: str | None = None
        self.confirm_mode = False
        self.confirm_idx = 0

        self.gpu_current_freq = self.gpu
        self.gpu_float_down_hold = 0
        self.gpu_float_up_hold = 0
        self.gpu_last_send_ts = 0.0
        # 跨进程互斥长期占用检测：连续 busy 计数 / 退避截止 / 告警防刷屏 / 待弹告警
        # （PowerPanel.sync 轮询 _gpu_busy_warn_pending 在主线程弹 toast）。
        self.gpu_busy_streak = 0
        self._gpu_busy_backoff_until = 0.0
        self._gpu_busy_warned_at = 0.0
        self._gpu_busy_warn_pending = False

        # GPU PDH 计数器损坏检测的计时状态（见 _maybe_warn_gpu_counter_corrupted）。
        # 同一会话内只弹一次 lodctr 引导，避免刷屏。
        self._gpu_corrupt_since = 0.0
        self._gpu_corrupt_warned_at = 0.0
        # P2-7：GPU 利用率信号不可用的持续时长起点（monotonic），超时自动禁用浮动。
        self._gpu_unknown_hold_since = 0.0
        # Sophisticated float state (from PS power.ps1 GPUauto)
        self.gpu_util_buf: list[float] = []
        self.gpu_util_ema = -1.0
        self.gpu_last_executed_dir = ""

        # 线程安全：GPU 浮动逻辑由独立 worker 线程（_float_worker_loop）驱动，
        # 可能与 GUI 线程的 toggle/load 并发。用 RLock 保护浮动状态。
        self._float_lock = threading.RLock()
        # GPU 监测（2 分钟诊断录制）状态：后台线程采样写日志，主线程读状态更新 UI。
        self._gpu_monitor_active = False
        self._gpu_monitor_stop = threading.Event()
        self._gpu_monitor_path = ""
        self._gpu_last_tick_mono = 0.0
        # 活跃地板：卡在 400MHz 且有负载时抬离到的下限频率，防止抬上去又被降频
        # 拖回地板造成时钟抖动（详见 gpu_float_tick ③）。
        self.gpu_floor_active = self.GPU_FLOOR
        # GPU 浮动决策 worker（P1-②，2026-08-20）：由独立 daemon 线程固定周期
        # 驱动，不再占用 MetricSampler 采样回调线程（慢消费者阻塞整条采样链的根因）。
        self._float_worker_stop = threading.Event()
        self._float_worker: "threading.Thread | None" = None

        # 挂载到全局性能检查脚本（MetricSampler）：每次采样就跑一次自动
        # 锁频决策 + 5s 保活重发。该线程不受窗口焦点影响，解决失焦即停。
        self._connect_sampler()

        self.tdp_slider = _Btn(col=0, row=0)
        self.tdp_lock_btn = _Btn(col=1, row=0)
        self.gpu_slider = _Btn(col=0, row=1)
        self.gpu_lock_btn = _Btn(col=1, row=1)
        self.gpu_float_btn = _Btn(col=0, row=2)
        self.apply_btn = _Btn(col=1, row=2)

    # -- items for matrix nav ---------------------------------------
    def _items(self) -> list[_Btn]:
        return [self.tdp_slider, self.tdp_lock_btn, self.gpu_slider,
                self.gpu_lock_btn, self.gpu_float_btn, self.apply_btn]

    # -- config persistence -----------------------------------------
    def save_config(self) -> PowerConfig:
        pc = PowerConfig(
            gpu=self.gpu, tdp=self.tdp, tdp_lock=self.tdp_lock,
            gpu_lock=self.gpu_lock, gpu_float_enabled=self.gpu_float_enabled,
        )
        self.config.power_config = pc
        return pc

    def load_config(self) -> None:
        pc = getattr(self.config, "power_config", None) or PowerConfig()
        self.tdp = max(self.TDP_MIN, min(self.TDP_MAX, pc.tdp))
        self.gpu = max(self.GPU_MIN, min(self.GPU_MAX, pc.gpu))
        self.tdp_lock = pc.tdp_lock
        self.gpu_lock = pc.gpu_lock
        self.applied = False
        # Reset sophisticated float state on load (under float lock)
        with self._float_lock:
            self.gpu_current_freq = self.gpu
            self.gpu_float_enabled = pc.gpu_float_enabled
            self.gpu_util_buf.clear()
            self.gpu_util_ema = -1.0
            self.gpu_last_executed_dir = ""
            self.gpu_float_down_hold = 0
            self.gpu_float_up_hold = 0
            self.gpu_floor_active = self.GPU_FLOOR
            self.gpu_busy_streak = 0
            self._gpu_busy_backoff_until = 0.0
            self._gpu_busy_warn_pending = False
        self._render()

    # -- clamp helpers ----------------------------------------------
    def _clamp_tdp(self, v: int) -> int:
        return max(self.TDP_MIN, min(self.TDP_MAX, v))

    def _clamp_gpu(self, v: int) -> int:
        return max(self.GPU_MIN, min(self.GPU_MAX, v))

    # -- ryzenadj apply -------------------------------------------
    def build_power_config(self) -> PowerConfig:
        # ⚠️ 语义说明（P3-11）：float-on 时 gpu 字段返回的是「当前浮动频率」而非
        # ceiling，仅供 apply_power 内部的 float 分支消费（该分支随后用 self.gpu
        # 覆盖并下发）；**不要**把 pc 用于持久化——持久化走 save_config() 的
        # self.gpu（ceiling），避免把 400 之类浮动值写进配置当上限。
        pc = PowerConfig(
            gpu=self.gpu_current_freq if self.gpu_float_enabled else self.gpu,
            tdp=self.tdp, tdp_lock=self.tdp_lock, gpu_lock=self.gpu_lock,
            gpu_float_enabled=self.gpu_float_enabled,
        )
        return pc

    def apply_power(self, silent: bool = False) -> bool:
        pc = self.build_power_config()
        # 统一派发器：把「GPU 锁频 / TDP 锁」写进 desired，再合并下发一次 ryzenadj。
        if pc.gpu_float_enabled:
            # 浮动开启时，手动滑块值 = 频率上限 + 立即生效：把手动值作为当前频率下发，
            # 浮动从该值继续自动调节。旧代码此处 gfx 被整段跳过（pass），导致用户改
            # 滑块+Apply 完全无效——频率被冻结在旧值，表现为“开浮动后无法手动设频率”。
            self.gpu_current_freq = self.gpu
            self.ryzenadj.set_desired_gpu_clk(self.gpu)
        elif pc.gpu_lock:
            self.ryzenadj.set_desired_gpu_clk(pc.gpu)
        else:
            self.ryzenadj.set_desired_gpu_clk(None)
        if pc.tdp_lock:
            self.ryzenadj.set_desired_tdp_mw(pc.tdp * 1000)
        else:
            self.ryzenadj.set_desired_tdp_mw(None)
        args = self.ryzenadj.build_desired_args()
        exe = self.ryzenadj._resolve_exe()
        # 把实际下发的指令打到 toast 上，方便跨设备排查：
        # 若看到的还是朴素「✓ 已应用电源配置」而无 cmd=，说明跑的是旧代码（日志/修复未生效）。
        cmd_str = " ".join([os.path.basename(exe)] + args) if args else "（无参数）"
        ok = self.ryzenadj.maybe_apply(silent=silent)
        if ok:
            self.applied = True
            self.confirm_mode = False
            self.save_config()
            if not silent:
                self.show_toast("✓ 已应用电源配置  cmd: " + cmd_str, success=True)
            self.update_status()
        else:
            if not silent:
                # 精确诊断：HVCI 开启时驱动被内核拦截，管理员也无效；不再误导成
                # 「请以管理员身份运行」（用户已确认是管理员仍失败的根因）。
                tip = self.ryzenadj.diagnose() or (
                    "：请以管理员身份运行本程序" if not self.ryzenadj._is_admin()
                    else "（详见 logs/ryzenadj_invoke.log）")
                self.show_toast("✗ ryzenadj 调用失败" + tip, success=False)
        return ok

    def on_power_resume(self) -> bool | None:
        """OnPowerResume 钩子：系统从休眠/睡眠恢复后，若休眠前已应用过电源配置
        （self.applied），重新下发一次 ryzenadj——SMU 在休眠后会重置，TDP/GPU
        锁等寄存器会丢失。静默重下发（不弹 toast，由调用方汇总反馈）。

        幂等：未应用过（applied=False）则 no-op，绝不擅自下发用户未启用的配置。
        """
        if not getattr(self, "applied", False):
            return None
        try:
            return self.apply_power(silent=True)
        except Exception:
            return False

    # -- 配置预设钩子（移植自 YeManCC 档位概念，重做为自由命名预设系统）------
    def on_export_preset(self) -> dict[str, Any]:
        """OnExportPreset 契约：返回电源卡片当前相关值。

        仅导出预设关心的字段：tdp / GPU 上限 / GPU 浮动开关 / TDP·GPU 锁。
        GPU 上限始终导出（供硬锁场景恢复），但列表显示仅在 gpu_float 开时显值。
        """
        return {
            "tdp": int(self.tdp),
            "gpu": int(self.gpu),
            "gpu_float": bool(self.gpu_float_enabled),
            "tdp_lock": bool(self.tdp_lock),
            "gpu_lock": bool(self.gpu_lock),
        }

    def on_apply_preset(self, data: dict[str, Any]) -> None:
        """OnApplyPreset 契约：把预设的电源值写回并下发（静默）。

        直接置布尔/数值后 apply_power，不走 toggle_gpu_float（避免其健康
        自检 / 熔断 / 弹 toast 等重副作用）。GPU 上限始终恢复（硬锁场景也用）。
        """
        if not isinstance(data, dict):
            return
        if "tdp" in data:
            self.tdp = self._clamp_tdp(int(data.get("tdp", self.tdp)))
        if "gpu" in data:
            self.gpu = self._clamp_gpu(int(data.get("gpu", self.gpu)))
        if "gpu_float" in data:
            self.gpu_float_enabled = bool(data["gpu_float"])
        if "tdp_lock" in data:
            self.tdp_lock = bool(data["tdp_lock"])
        if "gpu_lock" in data:
            self.gpu_lock = bool(data["gpu_lock"])
        self.apply_power(silent=True)

    @staticmethod
    def _fmt_ryzenadj_err(out: str) -> str:
        """从 ryzenadj 输出里抽关键错误行，截断给 toast 看。"""
        if not out:
            return ""
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if not lines:
            return ""
        tail = lines[-3:]
        joined = " | ".join(tail)
        if len(joined) > 220:
            joined = joined[-220:]
        return "（ryzenadj: " + joined + "）"

    def show_confirm(self) -> None:
        self.confirm_mode = True
        self.confirm_idx = 0
        self._update_confirm_visual()

    def dismiss_confirm(self) -> None:
        self.confirm_mode = False
        self._update_confirm_visual()

    # -- 挂载到全局性能检查脚本（MetricSampler）------------------------
    def _connect_sampler(self) -> None:
        """启动 GPU 浮动决策 worker + 确保共享采样器已启动。

        （2026-08-20 P1-② 修复）旧实现把浮动决策注册到 MetricSampler 的采样
        回调里同步执行——而 SMT 图 / 温度 / 自动锁帧与浮动共用同一条采样链，
        浮动一发卡（ryzenadj 下发 / 跨进程互斥等待最长 1.5s）就会阻塞整个采样
        宿主，其它消费者全部饿死。现改为**独立 worker 线程**固定周期轮询最新
        快照：慢只慢浮动自己，采样链不再被阻塞；窗口失焦（游戏前台）时 worker
        不受焦点影响、照常运行（等价于旧「挂采样器」的失焦免疫）。

        同时保证采样器已启动（即使负载门控降压未启用，浮动也照常工作）。
        """
        try:
            from ..core.metrics import MetricSampler
            sampler = MetricSampler.instance()
            if sys.platform == "win32":
                sampler.start()
        except Exception:  # pragma: no cover
            pass
        self._start_float_worker()

    def _start_float_worker(self) -> None:
        """启动 GPU 浮动决策 worker（幂等）。daemon 线程，随进程退出。"""
        if self._float_worker is not None and self._float_worker.is_alive():
            return
        self._float_worker_stop.clear()
        self._float_worker = threading.Thread(
            target=self._float_worker_loop, daemon=True, name="gpu-float-worker")
        self._float_worker.start()

    def _float_worker_loop(self) -> None:
        """worker 主循环：固定周期轮询最新采样快照跑一次浮动决策。"""
        while not self._float_worker_stop.wait(self.FLOAT_WORKER_INTERVAL_S):
            self._float_worker_once()

    def _float_worker_once(self) -> None:
        """单步：浮动开启则跑一次决策（worker 线程与单测共用）。

        全程持 ``_float_lock``（RLock，允许与 GUI 线程的 toggle/load 重入），
        保证浮动状态读写线程安全。保活重发已移除：ryzenadj/SMU 自身会保留
        设定值；频率仅在「目标值真正变化」时经统一派发器下发一次
        （见 ``_send_gpu_clock`` → ``maybe_apply`` 脏检查）。
        """
        try:
            if not self.gpu_float_enabled:
                return
            with self._float_lock:
                if not self.gpu_float_enabled:
                    return
                self.gpu_float_tick()
        except Exception:  # noqa: BLE001
            pass

    def _on_sampler_sample(self, snap) -> None:
        """（已由 ``_float_worker`` 取代，2026-08-20 起不再注册到采样器。

        保留本方法仅为兼容外部引用；实际驱动走独立 worker 线程，避免占用
        采样回调线程阻塞整条采样链（详见 ``_connect_sampler`` 注释）。）
        """
        self._float_worker_once()

    # -- 对称生命周期 -----------------------------------------------------
    def dispose(self) -> None:
        """停止 GPU 浮动决策 worker 与诊断录制线程（与 :meth:`_connect_sampler`
        配对）。

        由主窗口 ``closeEvent`` 在 app 退出时调用；区块因配置重载被重建时，
        旧 worker 随之停止，杜绝线程残留持有已废弃对象（2026-08-20 P1-②
        改造后不再注册 MetricSampler 回调，故也无需 unregister）。
        """
        try:
            self._float_worker_stop.set()
            if self._float_worker is not None:
                self._float_worker.join(timeout=2.0)
                self._float_worker = None
        except Exception:  # noqa: BLE001
            pass
        try:
            self._gpu_monitor_stop.set()
        except Exception:  # noqa: BLE001
            pass

    # -- GPU 利用率读取（修复 GPU 浮动失效的根因）-------------------------
    def _read_gpu_util(self, snap: "Any | None" = None) -> tuple[float, bool]:
        """Return ``(gpu_util_pct, known)``.

        主信号 = 共享 ``MetricSampler`` 快照的 ``gpu_pct`` —— 浮动决策由独立
        worker 线程（``_float_worker_loop``）固定周期消费该快照（也支持直接传入
        ``snap``，等价于快照本身）。这正是 rtss 自动锁帧所用的同一路信号：只要
        共享采样器的 GPU reader 活着，浮动就能工作。

        为什么不能直接用 ``read_gpu_utilization()``（直连 ``_GpuUtilMonitor``）：
        那套监控与 MetricSampler 2026-08-19 起**订阅同一个合并采样宿主**
        （``core.perf_probe.SharedMonitor``，1 个 PowerShell 同时读 GPU/每核/温度）。
        掌机上 ``\\GPU Engine(*)`` 这个 PDH 计数器一旦损坏（事件日志/日志里表现
        为 ``LOOP_ERR: 性能计数器示例中的数据无效``），宿主脚本的 GPU 段会持续
        报错不产出 G 行（CORE/TEMP 段仍正常，互不拖累），于是共享采样器与直连
        监控**同源同坏**（合并前两者是独立进程，一个坏另一个可能幸存——合并后
        失去这层冗余，属已知取舍）。旧代码只信直连监控 => GPU 浮动永远看到
        util=0、一路被压到地板 / 或完全不动 —— 即用户报告的「GPU 浮动没有
        任何效果」。

        回退：仅当 MetricSampler 也没有 GPU 读数时，才退到直连监控
        ``read_gpu_utilization()``，且**只有它真正 warmed（拿到过有效读数）才信任**
        ；否则直连监控此刻也是坏的，返回 ``known=False`` 让 tick 保守「保持当前
        频率」，而不是把一个假的「空闲 0%」当成真空闲去把频率拖到地板。

        测试里用的是 ``MockRyzenAdj``（非真实适配器），直接信任其返回值，保证
        单测不受影响。
        """
        # ① 优先用共享采样器的 gpu_pct（回调传入的 snap，或实时快照）
        if snap is not None and snap.gpu_pct is not None and snap.ok \
                and (time.monotonic() - snap.ts) <= self.GPU_UTIL_STALE_SECS:
            return (float(snap.gpu_pct), True)
        try:
            from ..core.metrics import MetricSampler
            s2 = MetricSampler.instance().snapshot
            if s2.gpu_pct is not None and s2.ok \
                    and (time.monotonic() - s2.ts) <= self.GPU_UTIL_STALE_SECS:
                return (float(s2.gpu_pct), True)
        except Exception:  # noqa: BLE001
            pass
        # ② 回退到直连监控，但仅当它已 warmed；测试（MockRyzenAdj）直接信任
        try:
            if isinstance(self.ryzenadj, MockRyzenAdj):
                return (float(self.ryzenadj.read_gpu_utilization()), True)
            if _GpuUtilMonitor.instance().warmed:
                return (float(self.ryzenadj.read_gpu_utilization()), True)
        except Exception:  # noqa: BLE001
            pass
        return (0.0, False)

    def _gpu_applied(self) -> "int | None":
        """ryzenadj 已确认写入的 gfx_clk（硬件侧），None=未知/无。诊断用。"""
        try:
            v = getattr(self.ryzenadj, "_applied", {}).get("gfx_clk")
            return None if v is None else int(v)
        except Exception:  # noqa: BLE001
            return None

    def _gpu_circuit_open(self) -> bool:
        """ryzenadj 熔断是否开启（开启时静默下发会被 SKIP）。诊断用。"""
        try:
            return bool(getattr(self.ryzenadj, "circuit_open", False))
        except Exception:  # noqa: BLE001
            return False

    def _maybe_warn_gpu_counter_corrupted(self, now: float | None = None,
                                           force: bool = False) -> None:
        """确认 GPU ``\\GPU Engine(*)`` PDH 计数器损坏时，提示用户用管理员
        PowerShell 跑 ``lodctr /r`` 重建计数器。

        触发条件（见 ``core.float_params.is_gpu_counter_corrupted``）：采样器在
        运行、核心/进程级信号健康，**唯独** ``gpu_pct`` 永远读不到。这与「开机
        预热 / 采样器未启动」区分开，避免误报。

        节流：损坏签名需持续 ≥20s 才首次弹（躲开开机前几秒的读数空窗）；同一
        会话 10min 内最多弹一次。``force=True``（用户主动点开 GPU 浮动开关时）
        跳过 20s 等待立即提示——用户此刻正在排查，值得立刻给引导。
        """
        now = time.monotonic() if now is None else now
        try:
            if is_gpu_counter_corrupted(MetricSampler.instance()):
                if self._gpu_corrupt_since <= 0.0:
                    self._gpu_corrupt_since = now
                sustained = now - self._gpu_corrupt_since
                if (force or sustained >= 20.0) and \
                        (now - self._gpu_corrupt_warned_at) >= 600.0:
                    self._gpu_corrupt_warned_at = now
                    self.show_toast(
                        "⚠ GPU 性能计数器已损坏：请以管理员身份运行 PowerShell 执行 "
                        "`lodctr /r` 重建计数器，再重启本程序（GPU 浮动/自动锁帧暂不可用）",
                        success=False,
                    )
            else:
                self._gpu_corrupt_since = 0.0
        except Exception:  # noqa: BLE001
            pass

    # -- GPU float: sophisticated 90%-target model (PS power.ps1 GPUauto) ---
    @staticmethod
    def _down_step_for(util_peak: float) -> int:
        """分级降频步长（用户 2026-08-19 指定阶梯）。

        依据 3 帧峰值 GPU 占用返回本拍降频幅度；返回 0 表示不降（≥85%）。
        """
        if util_peak < 10.0:
            return 200
        if util_peak < 30.0:
            return 100
        if util_peak < 60.0:
            return 100
        if util_peak < 70.0:
            return 50
        if util_peak < 85.0:
            return 25
        return 0

    @_float_guarded
    def gpu_float_tick(self, now: float | None = None,
                       snap: "Any | None" = None) -> int:
        """Gentle GPU float — tiered down when load drops, lift only when saturated.

        Decision model (per MetricSampler tick, ~1.5s), hit-and-return in order:
        * 3-frame rolling peak (utilPeak) drives decisions; EMA (utilFall) kept for
          observability only.
        * Floor-escape: locked at 400 MHz with utilPeak >= 85% → one-shot lift to
          FLOAT_ACTIVE_FLOOR (600 MHz) and hold.
        * Tiered down (user-specified 2026-08-19): any utilPeak < 85% drops by a
          load-dependent step — <10%: -200, <30%: -100, <60%: -100, <70%: -50,
          <85%: -25. Replaces the old slow "true idle (EMA<10% for 15 ticks) +
          early-down (<50% for 4 ticks)" paths that left a 4%-load GPU stuck high.
        * Saturated lift: utilPeak >= 92% and below ceiling → graded additive step
          (+50 / +100 / +150 by load).
        * Buffer band: util in [85%, 92%) → hold steady (Schmitt hysteresis, no flap).
        """
        now = time.time() if now is None else now
        self._gpu_last_tick_mono = time.monotonic()
        util, util_known = self._read_gpu_util(snap)
        ceiling = self.gpu
        current = self.gpu_current_freq

        # Safety: GPU 利用率信号不可用（共享采样器没跑 + 直连监控也坏了，典型是
        # 掌机 ``\\GPU Engine(*)`` PDH 计数器损坏）。此时保守「保持当前频率」，
        # 既不升压也不回落，避免把一个假的「空闲 0%」当成真空闲把频率拖到地板。
        if not util_known:
            self.gpu_util_buf.clear()
            self.gpu_util_ema = -1.0
            # P2-7：持续不可用超阈值 → 自动禁用浮动并写回上限，避免静默停摆。
            if self._gpu_unknown_hold_since <= 0.0:
                self._gpu_unknown_hold_since = now
            elif (now - self._gpu_unknown_hold_since) >= self.GPU_UNKNOWN_HOLD_MAX_S:
                self._gpu_unknown_hold_since = 0.0
                self._disable_gpu_float(
                    reason="gpu_unknown_hold_timeout",
                    toast=f"GPU 利用率信号持续不可用（{int(self.GPU_UNKNOWN_HOLD_MAX_S)}s），"
                          "已自动关闭浮动。请以管理员身份运行 PowerShell 执行 `lodctr /r` "
                          "重建计数器后再重新开启。")
                return 0
            _log_float("NO_DECISION", reason="gpu_unknown_hold", current=current,
                       note="GPU 利用率读取不可用（计数器损坏/采样器未运行），保持当前频率",
                       hold_since=round(now - self._gpu_unknown_hold_since, 1)
                       if self._gpu_unknown_hold_since > 0 else 0.0,
                       applied=self._gpu_applied(), circuit=self._gpu_circuit_open())
            # 若确属 GPU PDH 计数器损坏，提示用户跑 lodctr /r 重建（节流内）。
            self._maybe_warn_gpu_counter_corrupted()
            return 0
        # Safety: NaN（理论上不会发生，但防御性兜底）
        if isinstance(util, float) and math.isnan(util):
            self.gpu_util_buf.clear()
            self.gpu_util_ema = -1.0
            return 0
        # 信号恢复正常 → 重置不可用计时
        self._gpu_unknown_hold_since = 0.0

        # ── ① 3-frame rolling peak for up decisions ──
        self.gpu_util_buf.append(util)
        if len(self.gpu_util_buf) > 3:
            self.gpu_util_buf = self.gpu_util_buf[-3:]
        util_peak = max(self.gpu_util_buf) if self.gpu_util_buf else util

        # ── ② EMA（α=0.40，保留用于日志观察；降频决策改用 util_peak）──
        if self.gpu_util_ema < 0.0:
            self.gpu_util_ema = util
        else:
            self.gpu_util_ema = (self.FLOAT_EMA_ALPHA * util
                                 + (1.0 - self.FLOAT_EMA_ALPHA) * self.gpu_util_ema)
        util_fall = self.gpu_util_ema

        _log_float("TICK", util=round(util, 2), peak=round(util_peak, 2),
                   fall=round(util_fall, 2), current=current, ceiling=ceiling,
                   floor=self.GPU_FLOOR, last_dir=self.gpu_last_executed_dir,
                   applied=self._gpu_applied(), circuit=self._gpu_circuit_open())

        # ── ③ 地板抬离：锁在 400MHz 且负载确实高(util_peak≥85%) → 一次性抬到「预热地板」并稳住 ──
        # 修复原「永远卡 0.4ghz」根因：旧乘法规步长在 400 处被 50 对齐吞掉，
        # 低负载永远抬不出去。现改为：util_peak≥85% 一次性抬到 FLOAT_ACTIVE_FLOOR(600)
        # 即停手稳住——既果断脱离地板又不过冲，也不做 600/700/800 分级。
        # <85% 保持地板（分级降频阶梯本就要降到地板，已在地板降无可降）。
        if current <= self.GPU_FLOOR:
            if util_peak < self.FLOAT_FLOOR_RAISE_THRESHOLD:
                _log_float("NO_DECISION", reason="at_floor_idle", current=current,
                           util_peak=round(util_peak, 2))
                self.gpu_float_up_hold = 0
                return 0
            # 升频防抖：离开地板也需连续 N 拍高负载确认，防单拍尖峰误抬 / 400↔600 抖动。
            self.gpu_float_up_hold += 1
            if self.gpu_float_up_hold < self.FLOAT_UP_DEBOUNCE_TICKS:
                _log_float("NO_DECISION", reason="floor_raise_debounce",
                           current=current, util_peak=round(util_peak, 2),
                           up_hold=self.gpu_float_up_hold,
                           need=self.FLOAT_UP_DEBOUNCE_TICKS)
                return 0
            new_freq = min(ceiling, self.FLOAT_ACTIVE_FLOOR)
            if new_freq > current:
                _log_float("DECISION", action="up", reason="floor_raise",
                           current=current, new_freq=new_freq,
                           util_peak=round(util_peak, 2))
                self.gpu_last_executed_dir = "up"
                self.gpu_float_up_hold = 0
                # P2-4：地板抬离为一次性动作（每次仅当 400→600），force=True 无冷却
                # 由 up_hold 2 拍防抖节流（~4-6s/次），勿改小防抖否则驱动 churn 上升。
                self._send_gpu_clock(new_freq, force=True, reason="floor_raise")
                return new_freq - current
            _log_float("NO_DECISION", reason="floor_at_ceiling", current=current,
                       util_peak=round(util_peak, 2), ceiling=ceiling)
            self.gpu_float_up_hold = 0
            return 0

        # ── ④ 分级降频（用户 2026-08-19 指定阶梯）：只要 util_peak < 85% 就降频，
        # 幅度按档位递减——负载越接近需求，降得越慢：
        #   <10% → -200；<30% → -100；<60% → -100；<70% → -50；<85% → -25。
        # 用 3 帧峰值 util_peak 抗单帧毛刺，并加「连续 N 拍确认」防抖
        # （FLOAT_DOWN_DEBOUNCE_TICKS）：util_peak 需连续 N 拍落在降频区间才真正降，
        # 防单拍毛刺/负载短暂波动引起的误降与来回抖；确认后只要仍在 <85% 就每拍
        # 按当前档位连续降。与 ③ 地板脱离(≥85%)、⑥ 饱和抬频(≥92%) 构成施密特迟滞。
        down_step = self._down_step_for(util_peak)
        if down_step > 0 and current > self.GPU_FLOOR:
            self.gpu_float_down_hold += 1
            if self.gpu_float_down_hold < self.FLOAT_DOWN_DEBOUNCE_TICKS:
                _log_float("NO_DECISION", reason="down_debounce",
                           current=current, util_peak=round(util_peak, 2),
                           hold=self.gpu_float_down_hold,
                           need=self.FLOAT_DOWN_DEBOUNCE_TICKS)
                # 低负载（降频区间）下不应保留升频确认计数，避免残留高负载计数
                # 在负载跳变到抬频阈值时误触发一次抬升（防抖）。
                self.gpu_float_up_hold = 0
                return 0
            new_freq = max(self.GPU_FLOOR, current - down_step)
            # 注（P3-12）：降频结果不强制 50 对齐（ceiling 非 50 倍数时下发
            # 655/455 等值）。ryzenadj 接受任意值、SMU 按步进取整，显示与硬件
            # 可能差几十 MHz；对齐会改变既有行为且收益极低，故保持现状。
            if new_freq != current:
                _log_float("DECISION", action="down", reason="tiered_down",
                           current=current, new_freq=new_freq,
                           util_peak=round(util_peak, 2), step=down_step,
                           hold=self.gpu_float_down_hold)
                self._send_gpu_clock(new_freq, reason="tiered_down")
                self.gpu_last_executed_dir = "down"
                # 加强防抖：每降一档后清零，下一档仍需连续 N 拍低负载重新确认，
                # 防负载抖动导致的来回跳档（逐档确认而非一次确认后连续猛降）。
                self.gpu_float_down_hold = 0
                self.gpu_float_up_hold = 0
                return new_freq - current
            # 已到地板：重置，下次离开地板再重新计
            self.gpu_float_down_hold = 0
        else:
            # 退出降频区间（util_peak ≥85% 或已在地板）→ 重置降频防抖计数。
            # 注意：此处【不】清零 up_hold——高负载(≥85%)正是升频区间，up_hold
            # 需在 ③/⑥ 升频分支里持续累积到 FLOAT_UP_DEBOUNCE_TICKS 才能抬频；
            # 在此清零会导致升频防抖永远凑不满、频率抬不上去。
            self.gpu_float_down_hold = 0

        # ── ⑥ 饱和抬频：GPU 占用冲高(≥92%)且未达上限 → 分级加法步长微抬 ──
        # 作用：把被频率卡住的 GPU「喂饱」，使其占用维持高位（更多帧/更高真实占用），
        # 占用一旦回落到中段即停手稳住，不做无谓抖动。步长分级：越满越跟手。
        if util_peak >= self.FLOAT_UP_THRESHOLD and current < ceiling:
            # 升频防抖：饱和抬频也需连续 N 拍高负载确认，防单拍尖峰误升（否则
            # 一次瞬时 92% 就把频率顶上去，负载回落又降、来回抖）。
            self.gpu_float_up_hold += 1
            if self.gpu_float_up_hold < self.FLOAT_UP_DEBOUNCE_TICKS:
                _log_float("NO_DECISION", reason="up_debounce",
                           current=current, util_peak=round(util_peak, 2),
                           up_hold=self.gpu_float_up_hold,
                           need=self.FLOAT_UP_DEBOUNCE_TICKS)
                return 0
            if util_peak >= 98.0:
                step = 150
            elif util_peak >= 95.0:
                step = 100
            else:
                step = 50
            new_freq = min(ceiling, current + step)
            new_freq = int(round(new_freq / 50.0) * 50)
            new_freq = min(ceiling, new_freq)
            if new_freq != current:
                _log_float("DECISION", action="up", reason="saturated",
                           current=current, new_freq=new_freq,
                           util_peak=round(util_peak, 2), step=step)
                self.gpu_last_executed_dir = "up"
                self.gpu_float_up_hold = 0
                # P2-4：饱和抬升走 force=True 无独立冷却，靠 up_hold 2 拍防抖节流
                # （实测 ~4-6s/次 spawn，低于 07-26 修复前的 1Hz 保活）；勿调小
                # 防抖否则 ryzenadj 驱动 churn 上升（WinRing0 反复加载的失稳源）。
                self._send_gpu_clock(new_freq, force=True, reason="saturated")
                return new_freq - current
            _log_float("NO_DECISION", reason="up_at_ceiling", current=current,
                       util_peak=round(util_peak, 2), ceiling=ceiling)
            self.gpu_float_up_hold = 0
            return 0

        # ── ⑤ 缓冲带：占用落在 [85%, 92%) → 稳住不动 ──
        # 高于分级降频上界(85%)、低于饱和抬频阈值(92%)，不升不降，作为施密特
        # 缓冲防止边界来回抖（84%↔86% / 91%↔93% 不再反复横跳）。
        _log_float("NO_DECISION", reason="in_deadband_hold",
                   current=current, util_peak=round(util_peak, 2),
                   util_fall=round(util_fall, 2),
                   up_thr=self.FLOAT_UP_THRESHOLD)
        # 缓冲带内既不升也不降：清零升频确认计数，避免之前累积的高负载计数
        # 在负载跳变到抬频阈值时误触发一次抬升（防抖）。
        self.gpu_float_up_hold = 0
        return 0

    @_float_guarded
    def _send_gpu_clock(self, freq: int, force: bool = False,
                        reason: str = "adjust") -> None:
        """把目标 GPU 频率写进统一派发器，仅在值变化时合并下发一次 ryzenadj。

        不再自行 invoke：经 ``RyzenAdj.maybe_apply`` 的脏检查，只有当前 desired
        与上次 applied 不同才会真正调用 ryzenadj（合并 GPU/TDP/CO 成一次进程）。
        这样既消除高频 churn，又避免与降压 tick 并发抢 ring0 驱动。
        保留 1s 冷却作为二级节流（浮动决策本身只在频率真正变化时调用本函数）。

        关键修复（GPU 浮动「有时控不住」+「显示频率与实际不符」根因）：
        ``gpu_current_freq``（显示 + 算法 current）曾在这里被**无条件**乐观推进到
        ``freq``——但 silent 路径是 fire-and-forget，真实成败由后台 reaper 判定。
        一旦下发被 SKIP（busy / 熔断）或 reaper 判定失败，硬件其实没收到，而
        ``gpu_current_freq`` 仍停在乐观目标值 → ① 显示/状态频率 ≠ 实际硬件频率；
        ② 算法误以为已生效、以该目标值为 ``current`` 继续决策，可能不再重发 →
        硬件卡在旧值（表现即「GPU 浮动有时控不住」）。
        现改为：依据 ``maybe_apply`` 的受理结果决定 ``gpu_current_freq``——
        受理成功先乐观推进（reaper 失败会经 ``on_silent_sent`` 回调回滚到确认值）；
        被 SKIP（busy/熔断）则回退到「硬件已确认频率」，使显示与算法始终贴合真实
        硬件，并在下一拍重新尝试下发。
        """
        now = time.time()
        if not force and (now - self.gpu_last_send_ts) < self.FLOAT_COOLDOWN_S:
            _log_float("SEND_COOLDOWN", requested=freq, current=self.gpu_current_freq,
                       reason=reason, cooldown_left=round(self.FLOAT_COOLDOWN_S - (now - self.gpu_last_send_ts), 2))
            return
        # 退避期：跨进程互斥被另一实例长期占用（busy 连击已触发退避）时停止尝试下发，
        # 避免每拍空等 1.5s 互斥超时拖慢采样线程、且无限重试毫无意义；退避到期后再试。
        if now < self._gpu_busy_backoff_until:
            _log_float("SEND_BACKOFF", freq=freq, reason=reason, force=force,
                       resume_in=round(self._gpu_busy_backoff_until - now, 1))
            return
        self.ryzenadj.set_desired_gpu_clk(freq)
        # maybe_apply 返回「本次下发是否被真正受理」：
        #   - silent 路径：True = 已 fire-and-forget 受理；False = 被 busy/熔断 SKIP
        #     （硬件没收到）。据此决定 gpu_current_freq 是否乐观推进到目标。
        accepted = self.ryzenadj.maybe_apply(force=True)
        if accepted:
            self.gpu_current_freq = freq
            self.gpu_busy_streak = 0  # 真下发受理 → 互斥已恢复，清连击
        elif self.ryzenadj.circuit_open:
            # 熔断：静默下发被 SKIP，硬件确实没收到新频率，回退到已确认值贴真。
            confirmed = self.ryzenadj._applied.get("gfx_clk")
            if confirmed is not None:
                self.gpu_current_freq = int(confirmed)
            self.gpu_busy_streak = 0  # 熔断是另一类问题（真失败），不累计 busy 连击
        else:
            # busy（瞬态：另一 ryzenadj 子进程正在运行 / 跨进程互斥被占）：本次
            # 下发被 SKIP，**硬件根本没收到 freq**。回退到硬件已确认频率，使显示/
            # 算法 current 始终贴合真实硬件；desired 仍=freq，下一拍自动重试下发。
            confirmed = self.ryzenadj._applied.get("gfx_clk")
            if confirmed is not None:
                self.gpu_current_freq = int(confirmed)
            # 连击检测（2026-08-20）：busy 连续 ≥ 阈值 → 极可能是另一实例长期占用
            # 跨进程互斥（run.py 原无单实例守卫，双实例并存即互相饿死）。此时频率
            # 永远降不下来（日志铁证 freq 恒 907 + applied 707↔907 振荡 + circuit
            # 恒 False——busy 不算失败所以不熔断、也没人告诉用户）。触发退避停止
            # 锤锁 + 挂起告警，让面板弹 toast 提示关闭重复实例。
            self.gpu_busy_streak += 1
            if self.gpu_busy_streak >= self.GPU_BUSY_STREAK_WARN:
                self._gpu_busy_backoff_until = now + self.GPU_BUSY_BACKOFF_S
                if now - self._gpu_busy_warned_at >= self.GPU_BUSY_WARN_COOLDOWN_S:
                    self._gpu_busy_warned_at = now
                    self._gpu_busy_warn_pending = True  # PowerPanel.sync 主线程弹 toast
                # P2-5（2026-08-20）：busy 告警按来源细分——刚发生过 reaper 超时
                # （自身子进程挂起，互斥被自己的 reaper 占住）≠ 跨进程互斥被另一
                # 实例占用 ≠ 同进程瞬态繁忙，避免误导排查方向。
                skip = getattr(self.ryzenadj, "_last_skip_reason", "") or ""
                reap_to = getattr(self.ryzenadj, "_last_reap_timeout_mono", 0.0) or 0.0
                if reap_to and (time.monotonic() - reap_to) <= 12.0:
                    note = ("ryzenadj 调用超时/挂起（子进程 10s 未退出，可能被杀软"
                            "拦截或驱动加载卡死），详见 logs/ryzenadj_invoke.log")
                elif skip == "busy-cross-process":
                    note = ("跨进程互斥被另一实例长期占用：很可能有另一个凝冰结魄"
                            "实例在运行，GPU 浮动无法下发，请关闭重复实例")
                elif skip == "circuit-open":
                    note = "熔断已开启（连续失败，自动下发暂停），详见日志"
                else:
                    note = "ryzenadj 调用繁忙（同进程并发/互斥争用），详见 logs/ryzenadj_invoke.log"
                _log_float("BUSY_STALLED", freq=freq, reason=reason, force=force,
                           streak=self.gpu_busy_streak, skip=skip,
                           rolled_back_to=confirmed, applied=self._gpu_applied(),
                           note=note + "（已退避 5s 停止锤锁）")
            else:
                _log_float("SEND_BUSY", freq=freq, reason=reason, force=force,
                           streak=self.gpu_busy_streak,
                           rolled_back_to=confirmed, applied=self._gpu_applied())
        _log_float("SEND", freq=freq, reason=reason, force=force, accepted=accepted)
        self.gpu_last_send_ts = now

    def _on_gpu_sent(self, gfx: "int | None") -> None:
        """ryzenadj silent（GPU 浮动）下发结果回调：把 ``gpu_current_freq`` 校正到
        硬件实际已确认频率。

        背景：GPU 浮动走 silent fire-and-forget，``_send_gpu_clock`` 立即返回、真实
        成败由后台 reaper 线程判定。若下发被判定失败（busy / 驱动瞬态 / 熔断），
        reaper 会把 ``applied`` 回滚，但本块 ``gpu_current_freq`` 若仍停在乐观目标
        值，就会出现「凝冰结魄显示 600MHz、实际硬件还是 400MHz」的发散，且算法会
        误以为已生效而停止重试（表现即用户报告的「GPU 浮动有时控不住频率」）。

        此回调用 reaper 最终确认的 ``gfx_clk`` 覆盖 ``gpu_current_freq``，使显示与
        算法 current 始终贴合真实硬件状态。在后台线程执行，仅做一个 int 赋值，
        无 Qt 调用、不持锁，安全。
        """
        if not self.gpu_float_enabled or gfx is None:
            return
        try:
            self.gpu_current_freq = int(gfx)
        except Exception:  # noqa: BLE001
            pass

    # -- GPU 监测（2 分钟诊断录制）-----------------------------------------
    GPU_MONITOR_DURATION_S = 120
    GPU_MONITOR_INTERVAL_S = 1.0

    def start_gpu_monitor(self) -> "str | None":
        """启动 2 分钟 GPU 占用 + 浮动锁频诊断录制，日志写到安装目录 logs/。

        独立后台线程每 ~1s 采样一次，记录：GPU 占用原始值 + 是否可用（known）、
        浮动是否开启、浮动目标频率 gpu_current_freq、天花板 ceiling、ryzenadj
        已确认频率 _applied.gfx_clk、熔断状态、共享采样器健康度（ok/ts 新鲜度），
        以及浮动 tick 是否仍在被驱动（last_tick_age）。这能一次性区分三类根因：
        ① 信号读不到（util_known=False 但采样器 ok）；② tick 停摆（tick_age 越涨
        越大）；③ 下发被熔断/回滚（applied_gfx 不动或 circuit=True）。

        返回日志路径；已在录制则返回 None（防重入）。
        """
        if self._gpu_monitor_active:
            return None
        try:
            from ..core.metrics import MetricSampler
            _sampler = MetricSampler.instance()
        except Exception:  # noqa: BLE001
            _sampler = None
        log_dir = os.path.dirname(_GPU_AUTO_LOG)
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:  # noqa: BLE001
            log_dir = "."
        path = os.path.join(log_dir, "gpu_monitor_%s.log" % time.strftime("%Y%m%d_%H%M%S"))
        self._gpu_monitor_active = True
        self._gpu_monitor_path = path
        self._gpu_monitor_stop.clear()
        threading.Thread(target=self._gpu_monitor_loop, args=(path, _sampler),
                         daemon=True, name="gpu-monitor").start()
        return path

    def _gpu_monitor_loop(self, path: str, _sampler: "Any | None") -> None:
        duration = self.GPU_MONITOR_DURATION_S
        interval = self.GPU_MONITOR_INTERVAL_S
        try:
            with open(path, "a", encoding="utf-8", errors="ignore") as fh:
                fh.write("# 凝冰结魄 GPU 浮动 2 分钟诊断录制\n")
                fh.write("# 开始: %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                fh.write("# 列: n | util | known | float | freq | ceiling | applied_gfx"
                         " | circuit | sampler | snap_ok | snap_gpu | ts_age | tick_age\n")
                n = 0
                while n * interval < duration and not self._gpu_monitor_stop.is_set():
                    try:
                        line = self._gpu_monitor_sample(n, _sampler)
                    except Exception as e:  # noqa: BLE001
                        line = f"{n:3d} | error={e!r}"
                    fh.write(line + "\n")
                    fh.flush()
                    n += 1
                    self._gpu_monitor_stop.wait(interval)
                fh.write("# 结束: %s  样本数=%d\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), n))
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._gpu_monitor_active = False

    def _gpu_monitor_sample(self, n: int, _sampler: "Any | None") -> str:
        mono = time.monotonic()
        util, util_known = self._read_gpu_util()
        util_disp = round(util, 2) if util_known else "—"
        snap_gpu = None
        snap_ok = None
        ts_age = None
        sampler_running = False
        if _sampler is not None:
            try:
                sampler_running = bool(_sampler.is_running)
                s = _sampler.snapshot
                snap_gpu = s.gpu_pct
                snap_ok = s.ok
                if s.ts:
                    ts_age = round(mono - s.ts, 2)
            except Exception:  # noqa: BLE001
                pass
        tick_age = None
        if self._gpu_last_tick_mono:
            tick_age = round(mono - self._gpu_last_tick_mono, 2)

        def _f(v):
            return "—" if v is None else v

        return (f"{n:3d} | util={util_disp} | known={util_known} | "
                f"float={self.gpu_float_enabled} | freq={self.gpu_current_freq} | "
                f"ceiling={self.gpu} | applied_gfx={_f(self._gpu_applied())} | "
                f"circuit={self._gpu_circuit_open()} | sampler={sampler_running} | "
                f"snap_ok={_f(snap_ok)} | snap_gpu={_f(snap_gpu)} | "
                f"ts_age={_f(ts_age)} | tick_age={_f(tick_age)}")

    def _disable_gpu_float(self, reason: str,
                           toast: "str | None" = None) -> bool:
        """关闭 GPU 浮动并写回用户设定上限（P1-①/P2-7 共用）。

        关闭浮动 = 显式把频率写回 ceiling：SMU 会保留最后下发的 ``--gfx-clk``
        直到下次写入，若只置标志不写回，硬件会停留在最后浮动值（如 400）而
        UI 显示 ceiling → 显示/硬件发散且无恢复出口。写回后显示/硬件/算法三者
        一致。注：ryzenadj 的 ``--gfx-clk`` 只能设值、没有「解锁回 SMU 自动」
        flag；完全放开 GPU（gpu_lock=False + Apply）后硬件停留在本次写回的上限
        （=滑块值），属产品语义边界。返回写回是否被受理。

        ``reason`` 进决策日志（manual / gpu_unknown_hold_timeout 等）；
        ``toast`` 为成功文案，失败时自动拼接「写回上限未受理」。不持久化 config
        （manual 由调用方 toggle 的 save_config 保存；超时禁用仅内存态，重启后
        由用户决定是否重开）。
        """
        with self._float_lock:
            if not self.gpu_float_enabled:
                return False
            self.gpu_float_enabled = False
            _log_float("AUTO_DISABLE", reason=reason, final_freq=self.gpu_current_freq)
            self.gpu_current_freq = self.gpu
            self.ryzenadj.set_desired_gpu_clk(self.gpu)
            ok = self.ryzenadj.maybe_apply(silent=False)
            self.gpu_last_send_ts = time.time()
            self.gpu_busy_streak = 0
            self._gpu_busy_backoff_until = 0.0
            self._gpu_busy_warn_pending = False
        if toast is not None:
            self.show_toast(
                toast if ok else toast + "（写回上限未受理，详见日志）", success=ok)
        return ok

    def toggle_gpu_float(self) -> None:
        if not self.gpu_float_enabled:
            if not self.ryzenadj.health_check():
                # 精确诊断：HVCI 开启时驱动被内核拦截，管理员也无效。
                tip = self.ryzenadj.diagnose() or (
                    "：请以管理员身份运行本程序" if not self.ryzenadj._is_admin()
                    else "（详见 logs/ryzenadj_invoke.log）")
                self.show_toast("✗ ryzenadj 健康自检失败" + tip, success=False)
                return
            with self._float_lock:
                self.gpu_float_enabled = True
                self.gpu_current_freq = self.gpu
                # 用户主动重开开关 = 再给一次机会：清除上次的熔断/失败计数，
                # 否则熔断开启后浮动锁频会永远静默不下发（详见 ryzenadj.py 熔断逻辑）。
                self.ryzenadj.reset_circuit()
                _log_float("AUTO_ENABLE",
                           ceiling=self.gpu, start_freq=self.gpu,
                           up_thr=self.FLOAT_UP_THRESHOLD,
                           target_pct=self.FLOAT_TARGET_PCT)
                # Reset all sophisticated float state on enable
                self.gpu_util_buf.clear()
                self.gpu_util_ema = -1.0
                self.gpu_last_executed_dir = ""
            self.gpu_float_down_hold = 0
            self.gpu_float_up_hold = 0
            self.gpu_floor_active = self.GPU_FLOOR
            self._gpu_unknown_hold_since = 0.0
            # 重新开启浮动 = 给一次干净的机会：清 busy 连击/退避/告警（与熔断同理念）
            self.gpu_busy_streak = 0
            self._gpu_busy_backoff_until = 0.0
            self._gpu_busy_warn_pending = False
            # 开启即下发起始频率，经统一派发器（非 silent）捕获真实输出做诊断
            # （silent 路径 fire-and-forget 会永远返回成功，吞掉失败）。
            self.ryzenadj.set_desired_gpu_clk(self.gpu)
            args = self.ryzenadj.build_desired_args()
            exe = self.ryzenadj._resolve_exe()
            cmd_str = " ".join([os.path.basename(exe)] + args) if args else "（无参数）"
            ok = self.ryzenadj.maybe_apply(silent=False)
            self.gpu_last_send_ts = time.time()
            # 立即读一次 GPU 利用率，让用户在弹出瞬间就能确认「到底读没读到」
            # （空闲=0% 是正常的；若一直 0% 且非空闲，看 logs/gpu_util.log 判别源）。
            # 走统一的 _read_gpu_util：优先共享采样器，避免直连监控损坏时显示误导性的 0%。
            util_now, util_known = self._read_gpu_util()
            if util_known:
                util_hint = f" | GPU利用率≈{util_now:.1f}%"
            else:
                util_hint = " | GPU利用率=不可用(计数器损坏/采样器未运行)"
                # 用户此刻正在排查：若确属计数器损坏，立即给 lodctr 引导。
                self._maybe_warn_gpu_counter_corrupted(force=True)
            if ok:
                self.show_toast("✓ GPU 浮动已开启  cmd: " + cmd_str + util_hint, success=True)
            else:
                self.show_toast("⚠ GPU 锁频可能未生效 cmd: " + cmd_str, success=False)
        else:
            # P1-①/P2-7：关闭浮动 = 写回用户设定上限（显示/硬件/算法三者一致）
            self._disable_gpu_float(
                reason="manual",
                toast=f"GPU 浮动已关闭，频率恢复 {self.gpu} MHz")
        self.save_config()
        self._render()

    # -- toggles ----------------------------------------------------
    def toggle_tdp_lock(self) -> None:
        self.tdp_lock = not self.tdp_lock
        self._render()

    def toggle_gpu_lock(self) -> None:
        self.gpu_lock = not self.gpu_lock
        self._render()

    def _step_slider(self, direction: int) -> None:
        if self.adjust_target == "tdp":
            self.tdp = self._clamp_tdp(self.tdp + (1 if direction > 0 else -1))
        elif self.adjust_target == "gpu":
            self.gpu = self._clamp_gpu(self.gpu + (50 if direction > 0 else -50))
        self.applied = False
        self.update_slider_ui()
        self.update_status()

    # -- UI rendering ----------------------------------------------
    def update_slider_ui(self) -> None:
        # value labels
        tlabel = self.ui.get("tdp_value")
        if tlabel is not None:
            tlabel.text = f"{self.tdp} W"
        glabel = self.ui.get("gpu_value")
        if glabel is not None:
            glabel.text = f"{self.gpu} MHz"

    def update_status(self) -> None:
        lbl = self.ui.get("status_label")
        if lbl is None:
            return
        if self.applied:
            lbl.text = "✓ 已应用"
            lbl.fore_color = COLOR_GREEN
        elif self.tdp_lock or self.gpu_lock:
            lbl.text = "待应用"
            lbl.fore_color = COLOR_YELLOW
        else:
            lbl.text = "未锁定"

    def _render(self) -> None:
        self.tdp_slider.value = self.tdp
        self.gpu_slider.value = self.gpu
        tlock = get_button_state(COLOR_GREEN if self.tdp_lock else COLOR_RED, "solid")
        glock = get_button_state(COLOR_GREEN if self.gpu_lock else COLOR_RED, "solid")
        self.tdp_lock_btn.set_colors(tlock["Back"], tlock["Hover"], tlock["Border"], tlock["Fore"])
        self.gpu_lock_btn.set_colors(glock["Back"], glock["Hover"], glock["Border"], glock["Fore"])
        float_st = get_button_state(
            COLOR_GREEN if self.gpu_float_enabled else COLOR_RED, "solid")
        self.gpu_float_btn.set_colors(float_st["Back"], float_st["Hover"], float_st["Border"], float_st["Fore"])
        apply_st = get_button_state(self.theme_color, "solid" if self.applied else "frosted")
        self.apply_btn.set_colors(apply_st["Back"], apply_st["Hover"], apply_st["Border"], apply_st["Fore"])
        self.update_slider_ui()
        self.update_status()
        self._update_slider_visual()
        self._update_confirm_visual()

    def _update_slider_visual(self) -> None:
        for slider, target in ((self.tdp_slider, "tdp"), (self.gpu_slider, "gpu")):
            if self.in_item_mode and self.slider_adjust and self.adjust_target == target:
                slider.border_color = COLOR_YELLOW
                slider.border_thickness = 3
            else:
                slider.border_color = None
                slider.border_thickness = None
            if hasattr(slider, "invalidate"):
                slider.invalidate()

    def _update_confirm_visual(self) -> None:
        bar = self.ui.get("confirm_panel")
        if bar is not None:
            bar.visible = self.confirm_mode
        ok = self.ui.get("confirm_ok")
        cancel = self.ui.get("confirm_cancel")
        if self.confirm_mode:
            if ok is not None:
                ok.fore_color = COLOR_GREEN if self.confirm_idx == 0 else None
            if cancel is not None:
                cancel.fore_color = COLOR_RED if self.confirm_idx == 1 else None

    # -- theme -------------------------------------------------------
    def _set_title_color(self, color: Color) -> None:
        title = self.ui.get("title")
        if title is not None:
            title.fore_color = color

    def on_theme_change(self, accent: dict[str, Any]) -> None:
        self.theme_color = get_theme_accent()
        self._set_title_color(self.theme_color)
        sl = get_slider_colors(self.theme_color)
        for key in ("tdp_track", "gpu_track"):
            t = self.ui.get(key)
            if t is not None:
                t.back_color = sl["Track"]
        for key in ("tdp_fill", "gpu_fill"):
            f = self.ui.get(key)
            if f is not None:
                f.back_color = sl["Fill"]
        for key in ("tdp_thumb", "gpu_thumb"):
            th = self.ui.get(key)
            if th is not None and hasattr(th, "set_colors"):
                th.set_colors(sl["ThumbBack"], sl["ThumbHover"], sl["ThumbBorder"], sl["ThumbFore"])
        self._render()

    # -- navigation ---------------------------------------------------
    def on_activate_item(self) -> None:
        self.in_item_mode = True
        self.focus_idx.value = 0
        self.slider_adjust = False
        self.adjust_target = None
        set_control_focus_visual(self.tdp_slider, True)

    def on_deactivate_item(self) -> None:
        self.in_item_mode = False
        self.slider_adjust = False
        self.adjust_target = None
        clear_block_focus([i for i in self._items() if i is not None])
        self._render()

    def on_navigate(self, direction: int) -> None:
        """矩阵导航：上下(±1)/左右(±10)在 2×3 控件网格内移动焦点。

        缺失此方法会导致电源卡片在真实 app 中导航时抛 AttributeError（其它块
        ls/procmon/rtss/tile 均有实现）。焦点视觉由 navigate_matrix 内部经
        set_control_focus_visual 自动刷新。
        """
        navigate_matrix(self._items(), direction, self.focus_idx)

    # -- toast -------------------------------------------------------
    @_gui_method
    def show_toast(self, text: str, success: bool = True) -> None:
        _show_block_toast(self.ui, text, success)

    # -- registration --------------------------------------------------
    def register(self, registry: Any) -> None:
        bp = BlockBlueprint(
            block_type="power",
            name="Power Settings",
            color=COLOR_MAUVE,
            card=self.ui.get("card"),
            hooks={
                "GetCardHeight": lambda: 190,
                "OnRefresh": self.update_status,
                "OnLayout": lambda: self._render(),
                "OnSyncTitleColor": self._set_title_color,
                "OnThemeChange": self.on_theme_change,
                "OnSaveConfig": self.save_config,
                "OnLoadConfig": self.load_config,
                "OnActivateItem": self.on_activate_item,
                "OnDeactivateItem": self.on_deactivate_item,
                "OnPowerResume": self.on_power_resume,
                "OnExportPreset": self.on_export_preset,
                "OnApplyPreset": self.on_apply_preset,
            },
        )
        registry.register(bp.to_block())

    def status(self) -> str:
        """Short live readout for the smoke report / GUI panel."""
        parts = [f"TDP {self.tdp}W", f"GPU {self.gpu}MHz"]
        if self.gpu_float_enabled:
            parts.append(f"浮动 {self.gpu_current_freq}MHz")
        if self.applied:
            parts.append("已应用")
        elif self.tdp_lock or self.gpu_lock:
            parts.append("待应用")
        return " · ".join(parts)