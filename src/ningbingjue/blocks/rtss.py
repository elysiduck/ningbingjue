"""rtss block — frame-limit control via RTSS CLI.

Faithful logic port of ``blocks/rtss.ps1``.  All behavior lives in ``RtssBlock``;
the optional ``ui`` dict holds widget-like objects that receive visual updates
(so the *logic* is fully testable without a GUI).  ``register`` exposes the block
through the standard Hooks contract.
"""

from __future__ import annotations

import functools
import json
import os
import threading
import time
from typing import Any, Callable

from ..core.color.api import (
    get_button_state,
    get_slider_colors,
)
from ..core.color.palette import COLOR_BLUE, COLOR_GREEN, COLOR_RED, COLOR_YELLOW
from ..core.color.rgb import Color
from ..core.color.semantic import get_theme_accent, is_light_theme
from ..core.game_detect import find_foreground_game
from ..core.float_params import EMA_ALPHA, METRIC_STALE_SECS, METRIC_STALE_UI_SECS
from ..core.metrics import MetricSampler
from ..core.navigation import (
    FocusHolder,
    clear_block_focus,
    set_control_focus_visual,
)

from ..platform.rtss_cli import RtssCli
from ..platform import rtss_deploy
from ..platform.rtss_guard import (
    DEFAULT_INTERVAL,
    DEFAULT_MAX_RETRIES,
    RtssGuard,
)
from .base import BlockBlueprint


def _auto_guarded(fn):
    """Run a method under the block's RLock so the MetricSampler background
    thread (which drives auto-lock) and the UI thread never race on the
    shared fps / auto-lock state."""
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        with self._lock:
            return fn(self, *a, **k)
    return wrapper


# -- 跨线程 UI 马歇尔 ----------
# auto_lock / GPU 浮动 由 MetricSampler 后台线程驱动，状态翻转时会调用
# show_toast()/apply_*() 进而设置控件。Qt 规定 GUI 操作只能跑在主线程，后台
# 线程直接写控件会损坏 Qt 内部状态 → 间歇性原生崩溃（无 Python 栈）。
# 马歇尔实现已抽到 core.gui（与 power 块共用），启动时需在主线程调用一次
# ensure_gui_bridge()（见 ui/app_window.run_app）。
from ..core.gui import _gui_method


# -- RTSS 失败日志 --------------------------------------------------------
# RTSS 链路的失败绝大多数是静默的：配置写不进、profile 写被拒、下发返回非 0、
# RTSS 进程起不来……在掌机上无法区分「没装」「装了但没起来」「下发了没生效」。
# 只在失败时落一行（成功不写，避免刷盘），沿用 core/profile.py 的 logs/ 惯例。
_RTSS_LOG_PATH: str | None = None


def _rtss_log(msg: str) -> None:
    global _RTSS_LOG_PATH
    try:
        if _RTSS_LOG_PATH is None:
            root = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..", ".."))
            d = os.path.join(root, "logs")
            os.makedirs(d, exist_ok=True)
            _RTSS_LOG_PATH = os.path.join(d, "nbj_rtss.log")
        with open(_RTSS_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


class RtssBlock:
    FPS_MIN = 20
    FPS_MAX = 60

    # -- auto-lock (自动锁帧：低负载游戏场景自动顶到上限帧率) ----------------
    # 触发条件（三者同时满足才升压到 AUTO_TARGET=FPS_MAX）：
    #   0) 前台是「游戏进程」(core.game_detect.find_foreground_game 判定)
    #   1) 游戏进程 CPU 占用 < AUTO_CPU_MAX_PCT
    #      —— 优先用共享指标器 snapshot.prog_cpu（前台进程级，避免后台进程占用
    #         单核峰值造成的误挡）；该值暂不可用时回退 snapshot.max_core（系统级
    #         单核峰值）。
    #      ⚠️ 实测语义（16 份掌机游戏录制，原神/绝区零）：
    #         游戏进程 CPU 在 light 与 heavy 相位下几乎一致（中位 14%/13%，
    #         max 仅 28%），系统单核 max_core 也恒定 ~61%——两个 CPU 信号都
    #         **无法**区分菜单(轻载)与战斗(重载)；真正区分轻重的是 GPU 负载
    #         （light 中位 79% vs heavy 中位 89%）。因此本 CPU 条件不是“轻载
    #         判别器”，而是“CPU 余量护栏”：升压会额外增加渲染线程的 CPU 负载，
    #         仅当游戏自身 CPU 仍有足够余量时才允许升压，避免与升帧抢资源导致
    #         卡顿。阈值取实测最大值 28% 之上留 ~12pp 余量 = 40，远超正常
    #         菜单/游戏的 14% 占用，绝不误挡；仅当游戏 CPU 异常飙高（着色器编译、
    #         物理尖峰等）时抑制升压。
    #   2) GPU 自动锁频已落到地板 (经 gpu_freq_provider 注入的布尔)
    #         OR 系统 GPU 负载 < AUTO_GPU_LOAD_PCT
    # 信号 1/2 的负载全部来自全应用唯一的共享指标器 MetricSampler，不另起采样；
    # GPU 是否到地板由 power 块经 gpu_freq_provider 注入布尔（地板 = power.GPU_MIN，
    # 不在本块硬编码，改 GPU_MIN 时自动跟随）。数据新鲜度由 AUTO_STALE_SECS 守护：
    # 采样停摆时 tick 保守 no-op，避免基于冻结数据误升压/误回落。连续满足
    # AUTO_RISE_TICKS 拍(~1s/拍)才升压，退出条件保持 AUTO_FALL_TICKS 拍即回落到基线。
    AUTO_CPU_MAX_PCT = 40.0     # 游戏进程 CPU 余量护栏（实测 max 28% + ~12pp 余量）
    AUTO_GPU_LOAD_PCT = 85      # 系统 GPU 负载阈值（%）默认值：<85% 视为低负载触发升压
                               # （UI 可配 10-90）。实测掌机游戏菜单 GPU 中位 ~79%、战斗 ~89%，
                               # 取 85 可区分轻重载；阈值越高越易触发（接近"有游戏就顶上限"）。
    # 顶到的 FPS 上限默认值 = 本块支持的帧率上限（将来放宽 FPS_MAX 时自动跟随；UI 可配）。
    AUTO_TARGET = FPS_MAX
    # 自动锁帧「基线」与「升压目标」的最小有效差距。基线 >= 目标时，升压与回落
    # 写入 RTSS 的 Limit 是同一个值，状态机照常翻转、日志照常打印，但用户侧
    # 帧率毫无变化——即 8-18 报告根因③的「空转」。这是最容易误判为「RTSS 坏了
    # / 锁帧没生效」的失效模式，故在开启路径上做防呆（见 toggle_auto_lock）。
    AUTO_MIN_SPREAD = 30
    AUTO_RISE_TICKS = 3         # 连续满足 ~3s 才升压（防抖）
    AUTO_FALL_TICKS = 2         # 退出条件 ~2s 即回落
    # 数据新鲜度守护：共享指标器后台采样间隔 GPU 1.5s / 核心 1.0s / 进程 0.4s。
    # 超过 STRICT 秒无任何更新 => 采样停摆，tick 保守 no-op（不升压也不回落）；
    # 超过 UI 秒 => 自动锁帧判定为不可用（按钮置灰），避免“永久崩溃却显示监控中”。
    # 两块共用同一组窗口（core.float_params），保证 GPU 浮动的保守策略与自动锁帧一致。
    AUTO_STALE_SECS = METRIC_STALE_SECS
    AUTO_STALE_UI_SECS = METRIC_STALE_UI_SECS

    def __init__(self, rtss_cli: RtssCli, config_dir: str,
                 ui: dict[str, Any] | None = None,
                 gpu_freq_provider: Callable[[], bool] | None = None,
                 game_whitelist_provider: Callable[[], list[str]] | None = None) -> None:
        self.rtss_cli = rtss_cli
        self.config_file = os.path.join(config_dir, "rtss_config.json")
        self.ui: dict[str, Any] = dict(ui or {})
        # GPU 是否已到自动锁频地板：由 power 块注入布尔 (None => 此路失效，
        # 自动锁帧退化为仅看系统 GPU 负载)。地板值取自 power.GPU_MIN，本块不硬编码。
        self.gpu_freq_provider = gpu_freq_provider
        # 游戏白名单：复用冻结卡片「标为游戏」维护的 undervolt 白名单，使 ZZZ
        # 等未命中关键字表的游戏也能被自动锁帧识别为前台游戏。None => 仅关键字。
        self.game_whitelist_provider = game_whitelist_provider

        # 打包默认值（出厂值）：锁帧默认关闭；锁帧 FPS 默认 30。
        # make_installer 默认不打包任何本机配置，安装后的程序即以这些代码
        # 默认值运行（rtss_config.json 缺失时 load_config 直接返回，保留此处默认值）。
        self.lock_enabled = False
        self.lock_applied = False
        self.fps = 30
        self.slider_drag = False
        self.focus_idx = FocusHolder(0)
        self.slider_adjust = False
        self.in_item_mode = False

        # auto-lock runtime state
        self.auto_lock_enabled = False
        self.auto_active = False          # currently boosted to AUTO_TARGET
        self.auto_baseline: int | None = None  # FPS to restore when leaving 2D
        self.auto_rise_count = 0          # consecutive low-load polls
        self.auto_fall_count = 0          # consecutive high-load/foreground-lost
        # GPU 负载的 EMA 平滑值（与 power GPU 浮动共用同一 α=EMA_ALPHA），
        # 抑制 PDH 计数器单点抖动导致的阈值抖动。-1.0 表示尚未初始化。
        self.gpu_load_ema = -1.0

        # 自动锁帧「升压目标」与「GPU 负载阈值」均可配置（UI 滑块调节 + 持久化），
        # 默认值取自下方类常量；rtss_config.json 命中则覆盖。
        self.auto_target = int(self.AUTO_TARGET)                  # 升压顶到的帧率上限（FPS）
        self.auto_gpu_load_pct = int(self.AUTO_GPU_LOAD_PCT)      # 升压触发阈值：系统 GPU 负载 < 该值%

        self.toast_text = ""
        self.toast_success = False

        # -- RTSS 部署与守护（grill 决策：独立低频线程，与采样链解耦）--------
        # self.guard 在 ensure_rtss_async() 里懒创建并启动；None = 未启用/非
        # Windows。self._guard_state 喂给 UI 角标显示第四态「已部署但拉起失败」。
        self.guard: "RtssGuard | None" = None
        self._guard_state: "tuple[str, str]" = ("idle", "")
        # 守护的可选覆盖（rtss_config.json 手动改；缺失取代码默认值，零 UI 改动）。
        self._guard_interval = float(DEFAULT_INTERVAL)
        self._guard_retries = int(DEFAULT_MAX_RETRIES)
        self._rtss_path_override: "str | None" = None

        # 自动锁帧状态机的线程安全（在 MetricSampler 后台线程上被驱动）。
        self._lock = threading.RLock()
        # 把自动锁帧挂到全局「性能检查脚本」MetricSampler：后台线程、不受窗口
        # 焦点影响 —— 打游戏 HUD 失焦时仍持续运行（与 power 块 GPU 浮动同方案）。
        try:
            MetricSampler.instance().unregister_callback(self._on_sampler_sample)  # 幂等：避免重建时重复注册
            MetricSampler.instance().register_callback(self._on_sampler_sample)
        except Exception:
            pass

    # -- registration ------------------------------------------------------
    def register(self, registry: Any) -> None:
        bp = BlockBlueprint(
            block_type="rtss",
            name="► 锁帧控制",
            color=COLOR_BLUE,
            card=self.ui.get("card"),
            hooks=self._build_hooks(),
        )
        registry.register(bp.to_block())

    def status(self) -> str:
        """Short live readout for the smoke report / GUI panel."""
        if not self.auto_lock_available():
            lock = self._lock_text()
            return f"{lock} · 自动锁帧不可用(指标器未运行)"
        if self.auto_lock_enabled:
            if self.auto_active:
                return (f"{self.fps} FPS · 自动锁帧已顶{self.auto_target} "
                        f"(基线{self.auto_baseline})")
            return (f"{self.fps} FPS · 自动锁帧监控中 "
                    f"(基线{self.auto_baseline})")
        return self._lock_text()

    def auto_lock_available(self) -> bool:
        """自动锁帧依赖共享指标器 MetricSampler（CPU / GPU 指标）。

        仅当它在运行（真实 Windows 硬件 + 已启动）且最近数据未过期时才可用；
        否则按钮置灰、tick 直接 no-op，避免误升压或“采样已停摆却显示监控中”。"""
        try:
            sampler = MetricSampler.instance()
            if not sampler.is_running:
                return False
            snap = sampler.snapshot
            if not snap.ok:
                return False
            return (time.monotonic() - snap.ts) <= self.AUTO_STALE_UI_SECS
        except Exception:
            return False

    def _lock_text(self) -> str:
        if self.lock_enabled and self.lock_applied:
            return f"{self.fps} FPS · 已激活"
        if self.lock_enabled:
            return f"{self.fps} FPS · 待应用"
        return "锁帧未启用"

    def _build_hooks(self) -> dict[str, Any]:
        return {
            "GetCardHeight": lambda: 100,
            "OnRefresh": self.set_lock,
            "OnSyncTitleColor": lambda color: self._set_title_color(color),
            "OnThemeChange": self.on_theme_change,
            "OnSaveConfig": self.save_config,
            "OnLoadConfig": self.load_config,
            "OnApplyLock": self.apply_lock,
            "OnToggleLock": self.toggle_lock,
            "OnToggleAutoLock": self.toggle_auto_lock,
            "OnExportPreset": self.on_export_preset,
            "OnApplyPreset": self.on_apply_preset,
                "OnActivateItem": self.on_activate_item,
                "OnDeactivateItem": self.on_deactivate_item,
                "OnPowerResume": self.on_power_resume,
        }

    # -- navigation item list ---------------------------------------------
    def _nav_items(self) -> list[Any]:
        return [self.ui.get("btn_lock_status"), self.ui.get("btn_slider_thumb")]

    # -- slider UI ---------------------------------------------------------
    @_auto_guarded
    def update_slider_ui(self, value: int) -> None:
        if value < self.FPS_MIN:
            value = self.FPS_MIN
        if value > self.FPS_MAX:
            value = self.FPS_MAX
        self.fps = value

        # 手动改帧率时，若自动锁帧正处于升压态，视为用户接管：退出自动升压
        # 并以新值作为基线，避免之后回落时被旧基线覆盖掉用户的手动设置。
        if self.auto_active:
            self.auto_active = False
            self.auto_baseline = value
            self.auto_rise_count = 0
            self.auto_fall_count = 0

        track = self.ui.get("pnl_slider_track")
        thumb = self.ui.get("btn_slider_thumb")
        fill = self.ui.get("pnl_slider_fill")
        if track is not None and thumb is not None:
            loc = getattr(track, "location", None)
            track_x = loc.x if loc is not None else 0
            track_w = int(getattr(track, "width", 0) or getattr(track, "Width", 0) or 240)
            if track_w <= 0:
                track_w = 240
            thumb_x = track_x + int((value - 20) / 40.0 * track_w) - 12
            thumb_x = max(track_x - 12, min(thumb_x, track_x + track_w - 12))
            if hasattr(thumb, "location"):
                try:
                    thumb.location = type(loc)(thumb_x, getattr(loc, "y", 55))
                except Exception:
                    pass
            if fill is not None and hasattr(fill, "width"):
                try:
                    fill.width = max(8, int((value - 20) / 40.0 * track_w))
                except Exception:
                    pass

        lbl = self.ui.get("lbl_fps_value")
        if lbl is not None:
            lbl.text = f"{value} FPS"
        btn = self.ui.get("btn_lock_status")
        if btn is not None:
            btn.text = str(value)

        if self.lock_enabled:
            self.lock_applied = False
            self.set_lock()

    # -- core lock logic ---------------------------------------------------
    @_gui_method
    def set_lock(self) -> None:
        btn = self.ui.get("btn_lock_status")
        if btn is not None:
            btn.text = str(self.fps)
        is_light = is_light_theme()
        rtss_color = get_theme_accent()

        if is_light:
            lock_on = get_button_state(COLOR_GREEN, "frosted", light=True)
            lock_off = get_button_state(COLOR_RED, "frosted", light=True)
            state = lock_on if self.lock_enabled else lock_off
        else:
            if self.lock_enabled:
                state = get_button_state(rtss_color, "solid")
            else:
                state = get_button_state(style="muted")

        if btn is not None and not getattr(btn, "custom_styled", False):
            if hasattr(btn, "set_colors"):
                btn.set_colors(state["Back"], state["Hover"], state["Border"], state["Fore"])

        lbl = self.ui.get("lbl_lock_status_right")
        if lbl is not None:
            if self.lock_enabled and self.lock_applied:
                lbl.text = f"{self.fps} FPS · 已激活"
                lbl.fore_color = rtss_color
            elif self.lock_enabled:
                lbl.text = f"{self.fps} FPS · 待应用"
                lbl.fore_color = COLOR_YELLOW
            else:
                lbl.text = "锁帧未启用"

    def rtss_status(self) -> "tuple[str, str]":
        """委托给 rtss_cli 探测 RTSS 是否安装/运行，供 UI 状态显示。

        第四态 ``deployed_failed``：守护线程连续拉起 RTSS 失败（已部署但起不来）
        时由本块从 ``self._guard_state`` 暴露，UI 角标据此显示橙色「未就绪」。
        """
        if self.rtss_cli is None:
            return ("missing", "RTSS 未配置")
        try:
            state, text = self.rtss_cli.rtss_status()
        except Exception:
            state, text = ("unknown", "RTSS 状态未知")
        # 守护报「已部署但拉起失败」优先于普通 installed（installed 表示已装
        # 但未跑，而 deployed_failed 表示我们尝试拉起却失败——更严重的异常）。
        if self._guard_state[0] == "deployed_failed":
            return ("deployed_failed", self._guard_state[1])
        return (state, text)

    # -- RTSS 部署与守护（首次运行自部署 + 运行状态维护）-------------------
    def ensure_rtss_async(self) -> None:
        """启动期调用：后台线程跑一次自部署，成功后再启动守护。

        放后台是为了不阻塞 UI 启动（释放文件 + ``RTSS.exe /I`` 自注册可能耗时
        数秒）。非 Windows 直接 no-op。
        """
        import sys
        if sys.platform != "win32":
            return
        try:
            threading.Thread(
                target=self._ensure_rtss_worker, name="RtssDeploy", daemon=True
            ).start()
        except Exception:
            pass

    def _ensure_rtss_worker(self) -> None:
        try:
            state, text = rtss_deploy.ensure_rtss()
            _rtss_log(f"自部署结果 state={state} text={text}")
        except Exception as e:
            _rtss_log(f"自部署异常: {e!r}")
        # 无论部署结果如何，只要装上了就启动守护兜底。
        try:
            self._start_guard()
        except Exception as e:
            _rtss_log(f"启动守护异常: {e!r}")

    def _guard_context(self) -> dict:
        """喂给 RtssGuard 的实时上下文。"""
        return {
            "enabled": bool(self.lock_enabled or self.auto_lock_enabled),
            "limit": int(self.fps) if self.lock_enabled else 0,
            "deploy_dir": (
                self.rtss_cli._resolve_rtss_dir()
                if hasattr(self.rtss_cli, "_resolve_rtss_dir") else None
            ),
        }

    @_gui_method
    def _on_guard_state(self, state: str, text: str) -> None:
        self._guard_state = (state, text)
        # 触发 UI 同步（角标刷新）。面板自身每秒也会刷，这里主动推一次更及时。
        try:
            ui = self.ui.get("card")
            if ui is not None and hasattr(ui, "sync"):
                ui.sync()
        except Exception:
            pass

    def _start_guard(self) -> None:
        if self.guard is not None:
            return
        if not rtss_deploy.system_rtss_dir() and not rtss_deploy.bundle_rtss_dir():
            # 既无系统也无内置，守护无事可做；UI 会引导下载。
            return
        if not (self.rtss_cli.is_rtss_installed()
                if hasattr(self.rtss_cli, "is_rtss_installed") else True):
            return
        try:
            self.guard = RtssGuard(
                self.rtss_cli,
                context=self._guard_context,
                on_state=self._on_guard_state,
                interval=self._guard_interval,
                max_retries=self._guard_retries,
            )
            self.guard.start()
        except Exception as e:
            _rtss_log(f"RtssGuard 创建失败: {e!r}")

    def deploy_and_repair(self) -> "tuple[str, str]":
        """手动修复入口（UI「修复 RTSS」按钮）。

        跑完整链路：自部署（释放+注册）→ 拉起 RTSS → 补下发 profile → 重启守护。
        返回 ``(state, text)`` 供 toast 显示。
        """
        try:
            state, text = rtss_deploy.ensure_rtss(force=True)
            _rtss_log(f"手动修复：自部署 state={state} text={text}")
        except Exception as e:
            _rtss_log(f"手动修复异常: {e!r}")
            return ("missing", "修复失败")
        # 拉起 RTSS（deploy 不负责拉起；修复要立刻让用户看到效果）。
        try:
            d = self.rtss_cli._resolve_rtss_dir() if hasattr(
                self.rtss_cli, "_resolve_rtss_dir") else None
            if d and os.path.exists(os.path.join(d, "RTSS.exe")):
                import subprocess
                subprocess.Popen(
                    [os.path.join(d, "RTSS.exe")],
                    creationflags=0x08000000,
                )
        except Exception as e:
            _rtss_log(f"手动修复拉起异常: {e!r}")
        # 重下发当前 profile。
        try:
            self.apply_lock(silent=True)
        except Exception:
            pass
        # 重启守护（清掉旧失败计数 / 退避）。
        if self.guard is not None:
            try:
                self.guard.stop()
            except Exception:
                pass
            self.guard = None
        try:
            self._start_guard()
        except Exception:
            pass
        return (state, text)

    @_auto_guarded
    def apply_lock(self, silent: bool = False) -> None:
        # 先确认 RTSS 是否安装：未安装时写什么都是空操作，直接报错避免「假成功」。
        state, _ = self.rtss_status()
        if state == "missing":
            self.lock_applied = False
            self.set_lock()
            self.save_config()
            if not silent:
                self.show_toast("✗ RTSS 未安装，无法锁定", success=False)
            return
        fps = self.fps if self.lock_enabled else 0
        self.rtss_cli.set_limiter(self.lock_enabled)
        ok = self.rtss_cli.set_limit(fps)
        self.lock_applied = bool(ok)
        self.set_lock()
        self.save_config()
        if not silent:
            if ok:
                self.show_toast(
                    f"✓ 已锁定 {fps} FPS" if fps > 0 else "✓ 已解除锁定",
                    success=True,
                )
            else:
                # RTSS 已安装但当前未运行（或其 profile 写入被拒）：锁定未生效。
                self.show_toast("✗ RTSS 未运行，锁定失败", success=False)

    def on_power_resume(self) -> bool | None:
        """OnPowerResume 钩子：休眠恢复后重新下发 RTSS 锁帧配置。

        门控：仅当休眠前已应用过（self.lock_applied）才重下发。RTSS 是用户态
        进程，休眠本身不会让它退出，但锁帧状态可能因 RTSS 自身或显卡驱动重置
        而丢失，重新下发保险。静默重下发（不弹 toast）。

        幂等：未应用过则 no-op。
        """
        if not getattr(self, "lock_applied", False):
            return None
        try:
            self.apply_lock(silent=True)
            return True
        except Exception:
            return False

    @_auto_guarded
    def toggle_lock(self) -> None:
        self.lock_enabled = not self.lock_enabled
        # The main lock and auto-lock are mutually exclusive at the *off* edge:
        # turning the lock off means "no frame limiting at all", so auto-lock
        # (which always keeps the limiter on at a baseline) must come off too.
        if not self.lock_enabled:
            self.auto_lock_enabled = False
            self.auto_active = False
            self.auto_rise_count = 0
            self.auto_fall_count = 0
        self.lock_applied = False
        self.set_lock()
        self.save_config()
        self.apply_lock()

    # -- auto-lock: boost to 60 FPS when a game is in a low-load 2D menu -----
    @_auto_guarded
    def toggle_auto_lock(self) -> None:
        if self.auto_lock_enabled:
            self.auto_lock_enabled = False
            self.auto_active = False
            self.auto_rise_count = 0
            self.auto_fall_count = 0
            self.gpu_load_ema = -1.0
            # Restore the baseline so the user isn't stuck at 60 on exit.
            if self.auto_baseline is not None and self.lock_enabled:
                self.fps = self.auto_baseline
                self.apply_lock()
            self.save_config()
            self.show_toast("✓ 自动锁帧已关闭", success=True)
            return

        # Enabling: auto-lock always keeps the limiter ON at a baseline.
        self.auto_lock_enabled = True
        if not self.lock_enabled:
            # 用户此前没开过手动锁：开启自动锁帧顺带打开 limiter，但基线沿用
            # 用户当前档位（默认 60），不擅自压到 30。想“省电基线”的用户应先
            # 手动设一个低于上限的档位，再开自动锁帧，此时菜单顶上限、游戏保基线。
            self.lock_enabled = True
        # 基线 = 用户当前档位，尊重既有设定，不擅自改。
        self.auto_baseline = max(self.FPS_MIN, min(self.FPS_MAX, int(self.fps)))
        # 防呆：基线 >= 升压目标时自动锁帧是纯空转（升压写 60、回落还是 60，
        # 状态机与日志一切正常，但帧率纹丝不动）。此时既不静默放行（用户会以为
        # 是 RTSS 坏了），也不大幅篡改用户档位——只把基线压到目标下方一个档，
        # 并在 toast 里说清楚，用户随后可自行调节。
        coerced = False
        if self.auto_baseline >= self.auto_target:
            lowered = max(self.FPS_MIN, self.auto_target - self.AUTO_MIN_SPREAD)
            if lowered < self.auto_baseline:
                _rtss_log(
                    f"自动锁帧防呆：基线 {self.auto_baseline} >= 目标 "
                    f"{self.auto_target}，已下调至 {lowered}"
                )
                self.auto_baseline = lowered
                coerced = True
        self.auto_active = False
        self.auto_rise_count = 0
        self.auto_fall_count = 0
        self.gpu_load_ema = -1.0
        # Apply the baseline immediately so the limiter is live at once.
        self.fps = self.auto_baseline
        self.apply_lock()
        self.save_config()
        if coerced:
            self.show_toast(
                f"✓ 自动锁帧已开启 · 原档位与上限相同，"
                f"基线已下调至 {self.auto_baseline} → 目标 {self.auto_target} FPS",
                success=True,
            )
        else:
            self.show_toast(
                f"✓ 自动锁帧已开启（基线 {self.auto_baseline} FPS）", success=True
            )

    # -- preset snapshot / restore -----------------------------------------
    def on_export_preset(self) -> dict[str, Any]:
        """导出当前 RTSS 预设值：仅捕获「自动锁帧（AT）」开关与基线帧数。

        手动锁帧 fps 不在预设命名里，故不纳入快照；自动锁帧是唯一被预设托管的
        RTSS 维度。"""
        return {
            "at_enabled": bool(self.auto_lock_enabled),
            "at_fps": int(self.auto_baseline) if self.auto_baseline is not None
            else int(self.FPS_MIN),
        }

    def on_apply_preset(self, data: dict[str, Any]) -> None:
        """从预设回填 RTSS 值：恢复自动锁帧开关与基线帧数。

        与 toggle_auto_lock 的开启路径保持一致——自动锁帧开启时 limiter 也须开启
        并落到基线；关闭时退出升压态（auto_active 归零，但保留 lock_enabled/fps 由
        上层电源/手动设定决定，这里只负责 AT 维度）。静默重下发避免弹 toast。"""
        if not isinstance(data, dict):
            return
        if "at_enabled" in data:
            self.auto_lock_enabled = bool(data["at_enabled"])
        if "at_fps" in data:
            try:
                self.auto_baseline = max(
                    self.FPS_MIN, min(self.FPS_MAX, int(data["at_fps"]))
                )
            except (TypeError, ValueError):
                pass
        if self.auto_lock_enabled:
            self.lock_enabled = True
            # auto_baseline 可能仍为 None（预设只给了 at_enabled 而没给有效 at_fps）。
            # 直接把它赋给 self.fps 会让后续 set_limit 收到 None 并在 int() 处炸。
            if self.auto_baseline is None:
                self.auto_baseline = self.FPS_MIN
            # 与 toggle_auto_lock 同一条防呆：预设里存的基线同样可能 >= 目标。
            if self.auto_baseline >= self.auto_target:
                self.auto_baseline = max(
                    self.FPS_MIN, self.auto_target - self.AUTO_MIN_SPREAD
                )
            self.fps = self.auto_baseline
        else:
            self.auto_active = False
        self.apply_lock(silent=True)

    def _foreground_is_game(self) -> bool:
        """True 当且仅当前台窗口是一个「游戏进程」。

        使用共享的 core.game_detect.find_foreground_game：命中关键字表**或**白名
        单即视为游戏。白名单来自 game_whitelist_provider（复用冻结卡片「标为游戏」
        维护的 undervolt 白名单），覆盖 ZenlessZoneZero.exe 等漏网游戏。
        桌面 / 浏览器 / HUD 自身（凝冰结魄）一律返回 False，绝不触发自动锁帧。
        导入在函数内做防御性包裹，非 Windows / 查找失败则视为无游戏（保守不升压）。"""
        whitelist = None
        if self.game_whitelist_provider is not None:
            try:
                wl = self.game_whitelist_provider()
                if wl:
                    whitelist = list(wl)
            except Exception:
                whitelist = None
        try:
            return find_foreground_game(whitelist=whitelist) is not None
        except Exception:
            return False

    # -- 挂载点：被全局性能检查脚本 MetricSampler 的采样回调驱动 ----------------
    def _on_sampler_sample(self, snap: "Any") -> None:
        """MetricSampler 每次采样后回调（后台线程），直接驱动自动锁帧状态机一拍。

        内部 ``auto_lock_tick`` 已 guard（未开启/数据过期则 no-op）；失焦（游戏
        前台）也照常触发，因为本回调走后台线程，不经过 UI 定时器。"""
        self.auto_lock_tick()

    # -- 对称生命周期 -----------------------------------------------------
    def dispose(self) -> None:
        """注销挂到共享 MetricSampler 的自动锁帧回调（与 ``__init__`` 里的
        ``register_callback`` 配对）。

        由主窗口 ``closeEvent`` 在 app 退出时调用，确保回调列表不残留本块引用
        （区块若因配置重载被重建，旧回调会持续堆积并持有已废弃对象）。"""
        try:
            from ..core.metrics import MetricSampler
            MetricSampler.instance().unregister_callback(self._on_sampler_sample)
        except Exception:  # noqa: BLE001
            pass

    @_auto_guarded
    def auto_lock_tick(self) -> None:
        """自动锁帧状态机的一拍。由全局 MetricSampler 的采样回调（后台线程，失焦也运行）驱动；
        仅在 MetricSampler 未运行时才由面板 ``heavy_refresh``（~1 s）回退驱动。

        读取全应用唯一的共享指标器 ``MetricSampler``（游戏进程 CPU / 系统 GPU 负载），
        外加 power 块经 ``gpu_freq_provider`` 注入的「GPU 是否已到自动锁频地板」布尔，
        判定是否处于低负载游戏场景：
          游戏进程 且 进程CPU < 40% (CPU余量护栏) 且 (GPU到地板 或 负载 < 50%)
        → 连续满足 AUTO_RISE_TICKS 拍后顶到 AUTO_TARGET；退出条件保持
        AUTO_FALL_TICKS 拍即回落到基线。"""
        if not self.auto_lock_enabled:
            return
        # 指标器运行 + 采样新鲜度守卫（严格窗口）。采样停摆时保守 no-op，
        # 既不升压也不回落，避免基于冻结数据误决策。
        try:
            sampler = MetricSampler.instance()
            if not sampler.is_running:
                return
            snap = sampler.snapshot
        except Exception:
            return
        if not snap.ok or (time.monotonic() - snap.ts) > self.AUTO_STALE_SECS:
            return

        # 信号 0：前台是游戏进程
        game_ok = self._foreground_is_game()

        # 信号 1：游戏进程 CPU 占用。优先用前台进程级 prog_cpu（避免后台进程
        # 占用单核峰值造成的误挡）；暂不可用时回退系统级单核峰值 max_core。
        if snap.prog_cpu is not None:
            cpu_ok = snap.prog_cpu < self.AUTO_CPU_MAX_PCT
        else:
            cpu_ok = snap.max_core is not None and snap.max_core < self.AUTO_CPU_MAX_PCT

        # 信号 2a：系统 GPU 负载（来自共享指标器；系统级，已知局限，靠 OR 容错）。
        # 用 EMA（与 power GPU 浮动同一 α）平滑，抑制 PDH 计数器单点抖动造成的
        # 阈值反复横跳；gpu_pct 为 None（计数器损坏/暂缺）这一拍直接判为「非低负载」，
        # 不沿用上一拍的 EMA，避免损坏时把陈旧低值当成真低负载去误升压。
        gpu_load_low = False
        if snap.gpu_pct is not None:
            if self.gpu_load_ema < 0.0:
                self.gpu_load_ema = snap.gpu_pct
            else:
                self.gpu_load_ema = (EMA_ALPHA * snap.gpu_pct
                                     + (1.0 - EMA_ALPHA) * self.gpu_load_ema)
            gpu_load_low = self.gpu_load_ema < self.auto_gpu_load_pct
        # 信号 2b：GPU 自动锁频已落到地板（power 块注入的布尔；调用需防异常）
        gpu_at_floor = False
        if self.gpu_freq_provider is not None:
            try:
                gpu_at_floor = bool(self.gpu_freq_provider())
            except Exception:
                gpu_at_floor = False

        condition = game_ok and cpu_ok and (gpu_at_floor or gpu_load_low)

        if condition:
            self.auto_fall_count = 0
            if self.auto_active:
                self.auto_rise_count = 0
            else:
                self.auto_rise_count += 1
                if self.auto_rise_count >= self.AUTO_RISE_TICKS:
                    self.auto_active = True
                    self.fps = self.auto_target
                    self.apply_lock()
                    self.show_toast(
                        f"自动锁帧：低负载场景，已顶到 {self.auto_target} FPS", success=True
                    )
        else:
            self.auto_rise_count = 0
            if self.auto_active:
                self.auto_fall_count += 1
                if self.auto_fall_count >= self.AUTO_FALL_TICKS:
                    self.auto_active = False
                    if self.auto_baseline is not None:
                        self.fps = self.auto_baseline
                    self.apply_lock()
                    self.show_toast(
                        f"自动锁帧：已回落到 {self.fps} FPS", success=True
                    )
            else:
                self.auto_fall_count = 0

    # -- runtime config setters（UI 滑块调节，仅持久化，不写 RTSS）-----------
    @_auto_guarded
    def set_auto_target(self, v: int) -> None:
        v = max(self.FPS_MIN, min(self.FPS_MAX, int(v)))
        if v == self.auto_target:
            return
        self.auto_target = v
        self.save_config()

    @_auto_guarded
    def set_auto_gpu_load_pct(self, v: int) -> None:
        v = max(10, min(90, int(v)))
        if v == self.auto_gpu_load_pct:
            return
        self.auto_gpu_load_pct = v
        self.save_config()

    # -- config persistence ----------------------------------------------
    @_auto_guarded
    def save_config(self) -> None:
        # When auto-lock is currently boosted, persist the *baseline* (not 60)
        # so a restart begins at the 3D cap, not the transient 2D boost.
        fps_to_save = self.fps
        if self.auto_active and self.auto_baseline is not None:
            fps_to_save = self.auto_baseline
        data = {
            "LockEnabled": self.lock_enabled,
            "FPS": fps_to_save,
            "AutoLockEnabled": self.auto_lock_enabled,
            "AutoBaseline": self.auto_baseline if self.auto_baseline is not None else 30,
            "AutoTarget": self.auto_target,
            "AutoGpuLoadPct": self.auto_gpu_load_pct,
        }
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.config_file)), exist_ok=True)
            with open(self.config_file, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except Exception as e:
            # 写入失败绝不能静默：配置落不了盘意味着用户调的档位、开关的自动锁帧
            # 重启后全部回到代码默认值，而界面依然显示「✓ 已锁定」——与 8-18 报告
            # 里那类「假成功」同源，只是更难发现。落一行日志以便排查。
            _rtss_log(f"save_config 失败 file={self.config_file} err={e!r}")

    @_auto_guarded
    def load_config(self) -> None:
        if not os.path.exists(self.config_file):
            return
        try:
            with open(self.config_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if data:
                if "LockEnabled" in data:
                    self.lock_enabled = bool(data["LockEnabled"])
                if "FPS" in data:
                    self.fps = max(self.FPS_MIN, min(self.FPS_MAX, int(data["FPS"])))
                    self.update_slider_ui(self.fps)
                if "AutoLockEnabled" in data:
                    self.auto_lock_enabled = bool(data["AutoLockEnabled"])
                if "AutoBaseline" in data:
                    self.auto_baseline = max(
                        self.FPS_MIN, min(self.FPS_MAX, int(data["AutoBaseline"]))
                    )
                if "AutoTarget" in data:
                    self.auto_target = max(
                        self.FPS_MIN, min(self.FPS_MAX, int(data["AutoTarget"]))
                    )
                if "AutoGpuLoadPct" in data:
                    self.auto_gpu_load_pct = max(
                        10, min(90, int(data["AutoGpuLoadPct"]))
                    )
                # 守护可选覆盖（仅手动编辑 JSON；缺失则保留代码默认值）。
                if "GuardIntervalSec" in data:
                    try:
                        v = float(data["GuardIntervalSec"])
                        if v >= 1.0:
                            self._guard_interval = v
                    except (TypeError, ValueError):
                        pass
                if "GuardMaxRetries" in data:
                    try:
                        v = int(data["GuardMaxRetries"])
                        if v >= 1:
                            self._guard_retries = v
                    except (TypeError, ValueError):
                        pass
                if "RtssPath" in data:
                    p = data["RtssPath"]
                    if isinstance(p, str) and os.path.isdir(p):
                        self._rtss_path_override = p
                        # 让 rtss_cli 优先用该路径（影响 available/探活/守卫）。
                        if self.rtss_cli is not None:
                            self.rtss_cli.rtss_dir = p
                # Auto-lock implies the limiter is ON at the baseline.
                if self.auto_lock_enabled:
                    self.lock_enabled = True
                    self.auto_active = False
                    self.auto_rise_count = 0
                    self.auto_fall_count = 0
                self.lock_applied = False
        except Exception:
            pass

    # -- theme -------------------------------------------------------------
    def _set_title_color(self, color: Color) -> None:
        title = self.ui.get("lbl_rtss_title")
        if title is not None:
            title.fore_color = color

    def on_theme_change(self, accent: dict[str, Any]) -> None:
        rtss_c = get_theme_accent()
        title = self.ui.get("lbl_rtss_title")
        if title is not None:
            title.fore_color = rtss_c
        slider = get_slider_colors(rtss_c)
        track = self.ui.get("pnl_slider_track")
        if track is not None:
            track.back_color = slider["Track"]
        fill = self.ui.get("pnl_slider_fill")
        if fill is not None:
            fill.back_color = slider["Fill"]
        thumb = self.ui.get("btn_slider_thumb")
        if thumb is not None and hasattr(thumb, "set_colors"):
            thumb.set_colors(slider["ThumbBack"], slider["ThumbHover"],
                             slider["ThumbBorder"], slider["ThumbFore"])
        fps_label = self.ui.get("lbl_fps_value")
        if fps_label is not None:
            fps_label.fore_color = rtss_c
        self.set_lock()

    # -- navigation --------------------------------------------------------
    def _update_slider_visual(self) -> None:
        thumb = self.ui.get("btn_slider_thumb")
        if thumb is None:
            return
        if self.in_item_mode and self.focus_idx.value == 1:
            if self.slider_adjust:
                thumb.border_color = Color.from_argb(255, 80, 220, 100)
                thumb.border_thickness = 4
            else:
                thumb.border_color = COLOR_YELLOW
                thumb.border_thickness = 3
            if hasattr(thumb, "invalidate"):
                thumb.invalidate()

    def on_activate_item(self) -> None:
        self.in_item_mode = True
        self.focus_idx.value = 0
        self.slider_adjust = False
        self._update_slider_visual()
        items = self._nav_items()
        if items and items[0] is not None:
            set_control_focus_visual(items[0], True)

    def on_deactivate_item(self) -> None:
        self.in_item_mode = False
        items = self._nav_items()
        clear_block_focus([i for i in items if i is not None])
        self.slider_adjust = False
        sc = get_slider_colors(COLOR_BLUE)
        thumb = self.ui.get("btn_slider_thumb")
        if thumb is not None:
            thumb.border_color = sc["ThumbBorder"]
            thumb.border_thickness = 3
            if hasattr(thumb, "invalidate"):
                thumb.invalidate()
        self.save_config()

    # -- toast -------------------------------------------------------------
    @_gui_method
    def show_toast(self, text: str, success: bool) -> None:
        self.toast_text = text
        self.toast_success = success
        panel = self.ui.get("toast_panel")
        label = self.ui.get("toast_label")
        if panel is not None and hasattr(panel, "set_tone"):
            panel.set_tone(success)
            panel.visible = True
        if label is not None:
            label.text = text
            label.fore_color = (
                Color.from_rgb(0, 230, 120) if success else Color.from_rgb(255, 80, 80)
            )