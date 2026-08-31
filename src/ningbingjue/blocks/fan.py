"""fan block — EC-based fan control (MotionAssistant port).

移植 MotionAssistant ``Gyro.FanControl`` 的核心控制逻辑到凝冰结魄。
按机型路由 + 风扇曲线 + 1Hz tick + 3℃ 滞回 全部对齐 MA 实现。

设计要点：
  - **EC 写入**走 ``platform.ec_io``（自写 ctypes + WinRing0 + 跨进程命名互斥，
    与 ryzenadj 共用同一份 WinRing0x64.sys，对齐 MA 的 ``EcMutex`` 安全模型）。
  - **温度探针**走 ``core.thermal.ThermalProbe``（WMI MSAcpi_ThermalZoneTemperature，
    零原生依赖；用户选定方案，对掌机 ACPI 热区精度足够做风扇控制）。
  - **曲线调度**挂到全应用唯一的 ``MetricSampler``（后台线程，失焦也运行），
    与 power 块 GPU 浮动同款方案，避免「一打游戏 tick 就停」。
  - **降级**：EC 加载失败 / 温度探针未启动 → 卡片仍能显示，仅对应功能 no-op。
    UI 用状态文字明确告知用户原因（"WinRing0 不可用：请管理员运行 / 关 Core Isolation"）。

首版只支持 HX370 (Win Max 2 / Win Mini 370) EC 寄存器映射：
  - ec_enable_addr = 629 (0x275) — 使能手动模式
  - ec_pwm_addr    = 6153 (0x1809) — 写转速 0-255
8945HS / 其他机型留作 v2（board_quirk 字段已预留）。
"""
from __future__ import annotations
from ..ui.panels import _show_block_toast

import os
import sys
import threading
import time
from typing import Any, Optional

from ..core.color.api import get_button_state, get_slider_colors
from ..core.color.palette import COLOR_GREEN, COLOR_MAUVE, COLOR_RED, COLOR_YELLOW
from ..core.color.rgb import Color
from ..core.color.semantic import get_theme_accent
from ..core.config import FanConfig
from ..core.navigation import (
    FocusHolder,
    clear_block_focus,
    set_control_focus_visual,
)

from ..platform import ec_io
from ..core.thermal import ThermalProbe
from .base import BlockBlueprint


# Mode constants (string keys persisted to layout.json; rename = breaking change)
MODE_AUTO = "auto"        # EC 控制权交还 BIOS
MODE_MANUAL = "manual"    # 固定 manual_pct 转速
MODE_CURVE = "curve"      # 按曲线 1Hz tick + 滞回
MODE_SILENT = "silent"    # 风扇停转（写 0；危险但用户要的）
_VALID_MODES = (MODE_AUTO, MODE_MANUAL, MODE_CURVE, MODE_SILENT)

# 决策日志（诊断用：每次 fan tick 落一条，方便排查「风扇不动 / 没生效」）
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                              "..", "..", ".."))
_FAN_LOG = os.path.join(_PROJECT_ROOT, "logs", "fan.log")


def _log_fan(tag: str, **kwargs: Any) -> None:
    """追加一条风扇决策记录；写失败静默忽略。"""
    try:
        os.makedirs(os.path.dirname(_FAN_LOG), exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        parts = " | ".join(f"{k}={v}" for k, v in kwargs.items())
        with open(_FAN_LOG, "a", encoding="utf-8", errors="ignore") as fh:
            fh.write(f"[{ts}] {tag} | {parts}\n")
    except Exception:  # noqa: BLE001
        pass


class _Btn:
    """Lightweight button state placeholder (mirrors PowerBlock._Btn)."""
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

    def set_colors(self, *args: Any) -> None:
        self._colors = tuple(args)

    def set_focus_visual(self, focused: bool) -> None:
        self.focused = focused

    def invalidate(self) -> None:
        pass


class FanBlock:
    """风扇控制功能卡片（参考 PowerBlock 结构）。"""

    PWM_MIN = 0
    PWM_MAX = 255
    # silent 模式安全闸：温度超过此值自动放弃 silent，切回全速 255，
    # 避免用户开 silent 后烧机器。MA 也有同等的过热保护。
    SILENT_TEMP_CEILING_C = 85.0
    # 估算用：满 PWM(255) 对应的风扇标称最高转速（GPD Win Max 2 约 6000-7000
    # RPM）。仅当 EC 转速计读不到时用于「PWM → RPM」粗略换算显示。
    MAX_RPM = 6500

    def __init__(self, config: Any, ui: dict[str, Any] | None = None,
                 ec_writer: Any = None) -> None:
        self.config = config
        self.ui: dict[str, Any] = dict(ui or {})
        self.theme_color = get_theme_accent()
        # 默认注入 ec_io 模块（生产）；测试可注入 mock
        self._ec = ec_writer if ec_writer is not None else ec_io
        self._thermal = ThermalProbe.instance()

        fc = getattr(self.config, "fan_config", None) or FanConfig()
        self.mode = fc.mode if fc.mode in _VALID_MODES else MODE_AUTO
        self.manual_pct = max(self.PWM_MIN, min(self.PWM_MAX, fc.manual_pct))
        self.silent_pct = max(self.PWM_MIN, min(self.PWM_MAX, int(fc.silent_pct)))
        self.curve = list(fc.curve)
        self.hysteresis_c = int(fc.hysteresis_c)
        self.update_hz = int(fc.update_hz)
        self.ec_enable_addr = int(fc.ec_enable_addr)
        self.ec_pwm_addr = int(fc.ec_pwm_addr)
        self.board_quirk = str(fc.board_quirk)
        self.auto_detect = bool(getattr(fc, "auto_detect", True))
        self.detected_model = str(getattr(fc, "detected_model", "") or "")

        # 温度来源：acpi(默认, WMI 热区) / hwinfo(CPU Tctl/Tdie) / ec(EC 寄存器)
        # 见 _read_temp。acpi 在掌机常比 CPU 核心低 10~20℃，是「80℃ 风扇仍低转」
        # 的根因；hwinfo/ec 才能读到真实 CPU 结温。
        self.temp_source = str(getattr(fc, "temp_source", "acpi") or "acpi").lower()
        if self.temp_source not in ("acpi", "hwinfo", "ec"):
            self.temp_source = "acpi"
        self.ec_temp_addr = int(getattr(fc, "ec_temp_addr", 0) or 0)

        # RPM tachometer register. 0 = not set → fall back to pwm_addr+1
        # (the common ITE EC layout where the tach sits right after the PWM
        # register). detect_device() overrides this when a model is matched.
        rpm_cfg = int(getattr(fc, "ec_rpm_addr", 0) or 0)
        if not rpm_cfg:
            rpm_cfg = int(fc.ec_pwm_addr) + 1
        self.ec_rpm_addr = rpm_cfg

        # Auto-detect device (MA-style model → protocol + register mapping).
        if self.auto_detect:
            self.detect_device(silent=True)

        # Route ec_io to the device's SIO protocol (MA device mapping:
        # Win4 6800U uses 2E2F; everything else WM2/Win Mini/Duo uses 4E4F).
        self._apply_protocol()

        self.applied = False
        self.last_ec_error: str = ""   # 最近一次 EC 不可用原因（供 UI 展示根因）
        self.focus_idx = FocusHolder(0)
        self.in_item_mode = False
        self.slider_adjust = False
        self.adjust_target: str | None = None

        # 运行时读数
        self.last_temp_c: Optional[float] = None
        self.last_pwm: int = 0
        self._temp_src_used: str = self.temp_source
        self._last_decision_ts = 0.0
        # 温度源降级标记（grill 决策：回落必须明示，绝不静默）。
        # 仅当用户在编辑面板把 TempSource 显式设成 "hwinfo" / "ec"（非默认 acpi），
        # 而该来源实际读不到值、被迫回落到 ACPI 时才置 True。
        #
        # 为什么这条必须存在：ACPI 皮肤温度比 CPU 结温低 10~20℃，回落会让风扇
        # 转速被算低——正是「80℃ 风扇仍低转」的老坑。而 HWiNFO 免费版的共享内存
        # 有 12 小时自动断供限制，意味着这个回落会在长时间开机后**自动静默发生**。
        # 没有这个标记，用户只会觉得「风扇今天怎么不转了」。
        self.temp_degraded: bool = False

        # 滞回状态：last_band = 上次落档的曲线区间索引；只有当温度穿过
        # 区间边界 + hysteresis_c 才换档，避免风扇抽搐（MA TempDelay 同款）
        self._last_band_idx: int = -1

        # 线程安全：curve tick 由后台 MetricSampler 线程驱动，可能与 GUI 线程
        # 的 mode 切换 / 滑块调整并发。用 RLock 保护运行时状态。
        self._tick_lock = threading.RLock()

        # 退出兜底幂等标志：closeEvent / atexit 两条路径共用，
        # 保证整个进程生命周期内最多真正把 EC 交还 BIOS 一次。
        self._exit_restored = False

        self._connect_sampler()

        # 退出兜底（纵深防御）：即便 closeEvent 没触发（崩溃 / 被任务管理器强杀
        # / 解释器级退出），也在进程终止时尝试把 EC 控制权交还 BIOS。
        # _restore_auto_on_exit 内部幂等，与 closeEvent 中的 dispose 调用不冲突。
        # 仅 Windows 注册（EC 控制只在 Win32 真实存在，避免非 Windows 测试环境
        # 注册无意义的 atexit）。
        if sys.platform == "win32":
            try:
                import atexit
                atexit.register(self._restore_auto_on_exit)
            except Exception:  # noqa: BLE001
                pass

        # UI 按钮占位（与 PowerBlock._Btn 同款，用于 paint 时取色）
        self.auto_btn = _Btn(col=0, row=0)
        self.manual_btn = _Btn(col=1, row=0)
        self.curve_btn = _Btn(col=2, row=0)
        self.silent_btn = _Btn(col=3, row=0)
        self.pwm_slider = _Btn(col=0, row=1)
        self.apply_btn = _Btn(col=1, row=1)

    # -- items for matrix nav ---------------------------------------
    def _items(self) -> list[_Btn]:
        return [self.auto_btn, self.manual_btn, self.curve_btn,
                self.silent_btn, self.pwm_slider, self.apply_btn]

    # -- config persistence -----------------------------------------
    def save_config(self) -> FanConfig:
        fc = FanConfig(
            mode=self.mode,
            manual_pct=self.manual_pct,
            silent_pct=self.silent_pct,
            curve=list(self.curve),
            hysteresis_c=self.hysteresis_c,
            update_hz=self.update_hz,
            ec_enable_addr=self.ec_enable_addr,
            ec_pwm_addr=self.ec_pwm_addr,
            board_quirk=self.board_quirk,
            auto_detect=self.auto_detect,
            detected_model=self.detected_model,
            ec_rpm_addr=self.ec_rpm_addr,
            temp_source=self.temp_source,
            ec_temp_addr=self.ec_temp_addr,
        )
        self.config.fan_config = fc
        return fc

    def load_config(self) -> None:
        fc = getattr(self.config, "fan_config", None) or FanConfig()
        self.mode = fc.mode if fc.mode in _VALID_MODES else MODE_AUTO
        self.manual_pct = max(self.PWM_MIN, min(self.PWM_MAX, fc.manual_pct))
        self.silent_pct = max(self.PWM_MIN, min(self.PWM_MAX, int(fc.silent_pct)))
        self.curve = list(fc.curve)
        self.hysteresis_c = int(fc.hysteresis_c)
        self.update_hz = int(fc.update_hz)
        self.ec_enable_addr = int(fc.ec_enable_addr)
        self.ec_pwm_addr = int(fc.ec_pwm_addr)
        self.board_quirk = str(fc.board_quirk)
        self.auto_detect = bool(getattr(fc, "auto_detect", True))
        self.detected_model = str(getattr(fc, "detected_model", "") or "")
        self.temp_source = str(getattr(fc, "temp_source", "acpi") or "acpi").lower()
        if self.temp_source not in ("acpi", "hwinfo", "ec"):
            self.temp_source = "acpi"
        self.ec_temp_addr = int(getattr(fc, "ec_temp_addr", 0) or 0)
        rpm_cfg = int(getattr(fc, "ec_rpm_addr", 0) or 0)
        if not rpm_cfg:
            rpm_cfg = int(fc.ec_pwm_addr) + 1
        self.ec_rpm_addr = rpm_cfg
        self._apply_protocol()
        self.applied = False
        self._last_band_idx = -1
        self.last_temp_c = None
        self.last_pwm = 0
        self._render()

    # -- helpers ----------------------------------------------------
    def _clamp_pct(self, v: int) -> int:
        return max(self.PWM_MIN, min(self.PWM_MAX, v))

    def pwm_to_pct(self, pwm: int) -> int:
        """把内部 PWM 值（0-255）换算成「占最大转速的百分比」（0-100）。"""
        return max(0, min(100, round(int(pwm) / self.PWM_MAX * 100)))

    # -- device auto-detection ----------------------------------------
    def _apply_protocol(self) -> None:
        """Route ec_io to the SIO protocol implied by the current board_quirk.

        Win4 6800U uses the 2E2F SIO window (ports 0x2E/0x2F); everything else
        (WM2 / Win Mini / Duo) uses 4E4F (0x4E/0x4F).
        """
        proto = "2e2f" if self.board_quirk == "win4" else "4e4f"
        try:
            if hasattr(self._ec, "set_ec_protocol"):
                self._ec.set_ec_protocol(proto)
        except Exception:  # noqa: BLE001
            pass

    def set_detected_mapping(self, quirk: str, protocol: str,
                             enable_addr: int, pwm_addr: int, rpm_addr: int,
                             model_hint: str = "", persist: bool = True) -> None:
        """Lock in a device mapping the user has empirically confirmed working.

        Used by the brute-force fan test: after the user confirms which probe
        method actually spun the fan, we write that exact (quirk, protocol,
        register) mapping into the block, disable auto-detect so a flaky
        startup WMI read can't silently overwrite it with the wrong WM2
        fallback, and persist to layout.json. The card then drives the fan via
        the correct EC register on every subsequent run.

        ``protocol`` is accepted for forward-compatibility but the live routing
        is derived from ``board_quirk`` (win4 -> 2e2f, else 4e4f) so it stays
        consistent with ``_apply_protocol``.
        """
        self.board_quirk = str(quirk)
        self.ec_enable_addr = int(enable_addr)
        self.ec_pwm_addr = int(pwm_addr)
        self.ec_rpm_addr = int(rpm_addr) if int(rpm_addr) else int(pwm_addr) + 1
        self.auto_detect = False  # 锁定用户确认过的映射，避免启动检测覆写
        self._apply_protocol()
        if model_hint:
            self.detected_model = str(model_hint)
        self.applied = False
        self._last_band_idx = -1
        if persist:
            self.save_config()
        self._render()

    def detect_device(self, silent: bool = True) -> bool:
        """Auto-detect the device model and apply its EC register mapping.

        Returns True if a known mapping was applied.  On a match the mapping is
        always persisted back to config, so the later OnLoadConfig re-read keeps
        this device instead of reverting to the wm2 default.  ``silent=False``
        (user trigger from the edit panel) additionally re-renders + toasts.
        """
        try:
            if not hasattr(self._ec, "detect_device"):
                return False
            info = self._ec.detect_device()
        except Exception:  # noqa: BLE001
            return False
        if not info or not info.get("matched"):
            if not silent:
                self.show_toast("未识别到已知 GPD 机型，沿用当前映射", success=False)
            return False
        self.detected_model = str(info.get("model", "") or "")
        self.board_quirk = str(info.get("board_quirk", self.board_quirk))
        self.ec_enable_addr = int(info.get("enable_addr", self.ec_enable_addr))
        self.ec_pwm_addr = int(info.get("pwm_addr", self.ec_pwm_addr))
        # 转速计寄存器：matched 时用映射表里的值；若当前配置已手动指定则保留
        rpm = int(info.get("rpm_addr", 0) or 0)
        if rpm:
            self.ec_rpm_addr = rpm
        self._apply_protocol()
        self.applied = False
        self._last_band_idx = -1
        # 识别成功即写回 config（无论 silent），防止 boot() 后续 OnLoadConfig
        # 用旧 board_quirk 覆盖本次识别结果（否则启动识别白干、回退默认 wm2）。
        self.save_config()
        if not silent:
            self._render()
            self.show_toast(
                f"已识别：{self.detected_model}（{self.board_quirk}）",
                success=True)
        return True

    def _ec_health(self) -> bool:
        """EC 是否可用（WinRing0 加载 + 健康检查）。"""
        try:
            return bool(self._ec.health_check())
        except Exception:  # noqa: BLE001
            return False

    def _read_temp(self) -> Optional[float]:
        """读当前温度（°C），按 temp_source 路由 + 自动回落。

        三个来源（见 config.FanConfig.temp_source 注释）：
          - "acpi"  : WMI MSAcpi_ThermalZoneTemperature 最热热区（零依赖，
                      但掌机常比 CPU 核心低 10~20℃，是「80℃ 风扇仍低转」根因）。
          - "hwinfo": HWiNFO 共享内存 CPU(Tctl/Tdie) 温度（最准，需 HWiNFO 在跑
                      且开 Shared Memory；读不到/陈旧自动回落 acpi）。
          - "ec"    : 直接读 EC 温度寄存器 ec_temp_addr（GPD 原生、零外部依赖；
                      地址为 0 或读不到回落 acpi）。

        回落策略：选中的来源读不到合理值（None / <0 / >125）时，回退到 acpi，
        保证风扇控制永不因温度信号丢失而停摆。每次成功读数记录来源标签，
        供 UI 状态栏显示「读的是哪里的温度」。
        """
        src = self.temp_source
        val: Optional[float] = None
        # 本次是否被迫从用户选定的来源回落到 ACPI。默认 True（还没读到任何值），
        # 只有真从 hwinfo/ec 读到有效值时才置 False——这样短暂无值也会如实标记。
        degraded = src in ("hwinfo", "ec")
        try:
            if src == "hwinfo":
                val = self._read_temp_hwinfo()
            elif src == "ec":
                val = self._read_temp_ec()
            # acpi 或 其他来源失败 → 走 acpi 兜底
            if val is None or val < 0 or val > 125:
                val = self._read_temp_acpi()
                if val is not None:
                    self._temp_src_used = "acpi"
            else:
                self._temp_src_used = src
                degraded = False
        except Exception:  # noqa: BLE001
            val = self._read_temp_acpi()
            if val is not None:
                self._temp_src_used = "acpi"
        self.temp_degraded = degraded
        return val

    @property
    def temp_degraded_text(self) -> str:
        """降级时给 UI 的一行说明；未降级返回空串。

        调用方应把它拼在温度读数旁边（而不是替换掉读数），让用户既看到当前
        数字，也看到「这个数字不可信」。
        """
        if not self.temp_degraded:
            return ""
        want = {"hwinfo": "HWiNFO", "ec": "EC"}.get(self.temp_source, self.temp_source)
        return f"⚠ 已回落 ACPI（{want} 无数据·读数偏低）"

    def _read_temp_acpi(self) -> Optional[float]:
        """WMI ACPI 热区（默认来源，零依赖）。"""
        try:
            if not self._thermal.is_running:
                self._thermal.ensure_started()
            return self._thermal.read_temp_c()
        except Exception:  # noqa: BLE001
            return None

    def _read_temp_hwinfo(self) -> Optional[float]:
        """HWiNFO 共享内存 CPU(Tctl/Tdie) 温度；读不到/陈旧返回 None。"""
        try:
            from ..core.hwinfo_smi import read_telemetry
            tel = read_telemetry()
            if not tel.source_ok or not tel.fresh:
                return None
            t = tel.temp_cpu
            if t is None or t < 0 or t > 125:
                return None
            return float(t)
        except Exception:  # noqa: BLE001
            return None

    def _read_temp_ec(self) -> Optional[float]:
        """直接读 EC 温度寄存器 ec_temp_addr（摄氏度）。地址为 0 或读不到 → None。"""
        addr = int(self.ec_temp_addr or 0)
        if not addr:
            return None
        try:
            if not self._ec_health():
                return None
            raw = self._ec.read_ec(addr)
        except Exception:  # noqa: BLE001
            return None
        if raw is None:
            return None
        t = float(raw)
        if t < 0 or t > 125:
            return None
        return t

    def set_temp_source(self, source: str, ec_temp_addr: int | None = None) -> None:
        """切换温度来源（编辑面板调用）。source ∈ acpi/hwinfo/ec。"""
        s = str(source or "acpi").lower()
        if s not in ("acpi", "hwinfo", "ec"):
            s = "acpi"
        self.temp_source = s
        if ec_temp_addr is not None:
            self.ec_temp_addr = max(0, int(ec_temp_addr))
        self.applied = False
        self._render()

    def _write_fan_pwm(self, pwm: int, reason: str = "tick") -> bool:
        """写转速到 EC（先使能，再写转速寄存器）。

        返回 False 时调用方应 SKIP（不动 last_pwm，下个 tick 重试）。
        返回 True 表示已成功发出写入指令（不保证 EC 实际生效，那是固件事）。
        """
        if not self._ec_health():
            _log_fan("SKIP", reason="ec_unavailable", pwm=pwm)
            return False
        # 使能手动模式（每次写都重新使能，幂等，对齐 MA FanControl.cs:894）。
        # ec_enable_addr == 0 表示该机型（Win4 / Win Mini / Duo）无独立「使能
        # 手动」寄存器，直写转速寄存器即可（写 0 即交还 BIOS 自动控制）。
        if self.ec_enable_addr:
            ok_enable = self._ec.write_ec(self.ec_enable_addr, 1)
            if not ok_enable:
                _log_fan("SKIP", reason="enable_failed", pwm=pwm)
                return False
        ok_pwm = self._ec.write_ec(self.ec_pwm_addr, pwm & 0xFF)
        if not ok_pwm:
            _log_fan("SKIP", reason="pwm_write_busy",
                     pwm=pwm, addr=self.ec_pwm_addr)
            return False
        self.last_pwm = pwm
        _log_fan("WRITE", pwm=pwm, reason=reason, addr=self.ec_pwm_addr)
        return True

    def _restore_auto(self) -> bool:
        """交还 EC 控制权给 BIOS。

        两类机型路由不同（对齐 ``_write_fan_pwm`` 的注释约定）：
          * ``ec_enable_addr != 0``（WM2 等）：写 ``enable_addr=0`` 关闭手动使能；
          * ``ec_enable_addr == 0``（Win Mini / Win4 / Duo）：无独立使能寄存器，
            写 ``ec_pwm_addr=0`` 即交还 BIOS 自动控制。

        旧实现一律写 ``enable_addr``，对 Win Mini 即地址 0 → 等于没写，
        AUTO 模式形同虚设（风扇钉在上一次 manual 写的转速上）。
        """
        if not self._ec_health():
            return False
        if self.ec_enable_addr:
            target_addr = self.ec_enable_addr
            ok = self._ec.write_ec(self.ec_enable_addr, 0)
        else:
            target_addr = self.ec_pwm_addr
            ok = self._ec.write_ec(self.ec_pwm_addr, 0)
        if ok:
            _log_fan("RESTORE_AUTO", addr=target_addr)
        return ok

    # -- curve evaluation with hysteresis ---------------------------
    @staticmethod
    def _pwm_for_band(pts: list, band_idx: int) -> int:
        """Lookup PWM by band index, with bounds handling.

          - band_idx < 0          → lowest band's PWM (curve[0]) — actually
                                     we want *below* the curve = minimum PWM,
                                     so we return curve[0]["pwm"] (typically 0).
          - band_idx >= len(pts)  → highest band's PWM (curve[-1], typically 255).
          - otherwise              → curve[band_idx]["pwm"].
        """
        if not pts:
            return 255  # safety: no curve → full speed (avoid overheat)
        if band_idx < 0:
            return int(pts[0]["pwm"])
        if band_idx >= len(pts):
            return int(pts[-1]["pwm"])
        return int(pts[band_idx]["pwm"])

    def _eval_curve(self, temp_c: float) -> int:
        """按曲线点表查 PWM，带 hysteresis_c 滞回。

        算法（与 MA FanControl.cs:617 getTempFanSpeed 对齐）：
          1. 把曲线点按温度升序排
          2. 算升温路径候选档位 up_band = 最后一个 temp ≤ 当前温度 的点索引
          3. 若当前温度 < 上次档位温度 - hysteresis_c → 走降温路径，candidate = down_band
             若当前温度 > 上次档位温度 + hysteresis_c → 走升温路径，candidate = up_band
             否则 → 在滞回带内，candidate = _last_band_idx（保持当前档）
          4. 更新 _last_band_idx = candidate；target_pwm = _pwm_for_band(pts, candidate)

        滞回带内「保持当前档」是 MA TempDelay 帧缓降的核心：温度在阈值附近
        抖动时风扇不会反复切换档位，避免抽搐噪声 + 电机寿命损耗。
        """
        if not self.curve:
            return self.PWM_MAX
        pts = sorted(self.curve, key=lambda p: p["temp"])

        # 升温路径候选：最后一个 temp ≤ 当前温度 的点
        up_band = -1
        for i, pt in enumerate(pts):
            if temp_c >= pt["temp"]:
                up_band = i
            else:
                break

        if self._last_band_idx < 0:
            # 首次设定档位：直接用升温路径
            candidate = up_band
        else:
            last_idx = min(self._last_band_idx, len(pts) - 1)
            last_temp = pts[last_idx]["temp"]
            if temp_c < last_temp - self.hysteresis_c:
                # 降温路径：重新算 down_band（同样 = 最后一个 temp ≤ 当前温度 的点）
                down_band = -1
                for i, pt in enumerate(pts):
                    if temp_c >= pt["temp"]:
                        down_band = i
                    else:
                        break
                # 只在 down_band < 当前档 才降档（避免在升温时误降）
                candidate = down_band if down_band < self._last_band_idx else self._last_band_idx
            elif (self._last_band_idx < len(pts) - 1
                  and temp_c >= pts[self._last_band_idx]["temp"] + self.hysteresis_c):
                # 升温路径：只在 up_band > 当前档 才升档
                candidate = up_band if up_band > self._last_band_idx else self._last_band_idx
            else:
                # 在滞回带内 → 保持当前档
                candidate = self._last_band_idx

        self._last_band_idx = candidate
        target_pwm = self._pwm_for_band(pts, candidate)
        return self._clamp_pct(target_pwm)

    # -- apply & restore --------------------------------------------
    def apply(self, silent: bool = False) -> bool:
        """应用当前 mode 配置到 EC（用户点 Apply 按钮触发）。

        每种 mode 对应一次 EC 写入动作；返回 False 表示 EC 不可用或写入被跳过
        （一般是 ryzenadj 正持有 WinRing0 互斥）。silent=True 时只下发不弹
        toast（供 OnPowerResume 休眠恢复重下发使用）。
        """
        if not self._ec_health():
            err = ec_io.last_error() or "WinRing0 加载/初始化失败"
            if ec_io.is_admin():
                tip = err
            else:
                tip = (f"{err}；且程序未以管理员身份运行，"
                       "请右键「以管理员身份运行」后重试。")
            self.last_ec_error = tip
            if not silent:
                self.show_toast("✗ EC 不可用：" + tip, success=False)
            return False

        ok = False
        if self.mode == MODE_AUTO:
            ok = self._restore_auto()
            if not silent:
                self.show_toast("✓ 已切回 BIOS 自动控制", success=True)
        elif self.mode == MODE_MANUAL:
            ok = self._write_fan_pwm(self.manual_pct, reason="manual_apply")
            if ok and not silent:
                self.show_toast(
                    f"✓ 已应用：手动 {self.pwm_to_pct(self.manual_pct)}%", success=True)
        elif self.mode == MODE_CURVE:
            # 立即跑一次 tick，让用户看到效果
            ok = self._curve_tick_now()
            if ok and not silent:
                self.show_toast("✓ 曲线模式已启动（1Hz 调度）", success=True)
        elif self.mode == MODE_SILENT:
            temp = self._read_temp()
            if temp is not None and temp >= self.SILENT_TEMP_CEILING_C:
                if not silent:
                    self.show_toast(
                        f"✗ 当前温度 {temp:.0f}℃ ≥ {self.SILENT_TEMP_CEILING_C:.0f}℃，"
                        "拒绝开启 silent 模式（过热保护）", success=False)
                return False
            # silent=0 表示「交还 BIOS 自动控速」（而非 0 转速停转）；>0 时
            # 实际写入值下限钳到 15%，绝不写 0 停转。
            if self.silent_pct <= 0:
                ok = self._restore_auto()
                if ok and not silent:
                    self.show_toast(
                        "✓ 已交还 BIOS 自动控速（silent=0）", success=True)
            else:
                ok = self._write_fan_pwm(max(self.silent_pct, 15), reason="silent_apply")
                if ok and not silent:
                    self.show_toast(
                        f"✓ silent 模式：风扇 {self.pwm_to_pct(max(self.silent_pct, 15))}%"
                        f"（温度 ≥{self.SILENT_TEMP_CEILING_C:.0f}℃ 会拒绝/退出）",
                        success=True)

        if ok:
            self.applied = True
            self.save_config()
        elif not silent:
            self.show_toast("✗ 写入失败（WinRing0 互斥繁忙？请稍后再试）",
                            success=False)
        self.update_status()
        return ok

    def on_power_resume(self) -> bool | None:
        """OnPowerResume 钩子：休眠恢复后重新下发风扇配置。

        门控：仅当休眠前已应用过（self.applied）且当前模式 != AUTO 时才重下发。
        AUTO 模式是「交还 BIOS 控制」（写 ec_enable_addr=0），休眠后 BIOS 本就
        接管，无需重写；MANUAL/CURVE/SILENT 则是程序主动写 EC PWM，休眠后 EC
        寄存器可能被重置，需要重新写入。静默重下发（不弹 toast）。

        幂等：未应用过或 AUTO 模式则 no-op。
        """
        if not getattr(self, "applied", False):
            return None
        if self.mode == MODE_AUTO:
            return None
        try:
            return self.apply(silent=True)
        except Exception:
            return False

    def _curve_tick_now(self) -> bool:
        """立即跑一次 curve tick（Apply 时调用 + MetricSampler 回调里也调）。"""
        with self._tick_lock:
            return self._fan_tick_locked()

    def _fan_tick_locked(self) -> bool:
        """真正的 1Hz 决策逻辑。调用方需持 ``_tick_lock``。"""
        if self.mode != MODE_CURVE:
            return False
        temp = self._read_temp()
        if temp is None:
            # 温度信号暂不可用：不再「保持不写」（否则首次 apply 后风扇仍由
            # BIOS 控制，用户误以为曲线失效）。改为按曲线最低温档 curve[0].pwm
            # 断言控制权 —— 至少让风扇落入曲线设计的最低转速，而不是悬空。
            if self.curve:
                pts = sorted(self.curve, key=lambda p: p["temp"])
                floor_pwm = int(pts[0]["pwm"])
                _log_fan("NO_TEMP_FALLBACK", pwm=floor_pwm)
                return self._write_fan_pwm(floor_pwm, reason="curve_no_temp")
            return True
        self.last_temp_c = temp

        # silent 安全闸自动放弃（curve 模式下不会进入 silent，但保留防御）
        if temp >= self.SILENT_TEMP_CEILING_C:
            target = self.PWM_MAX
        else:
            target = self._eval_curve(temp)
        ok = self._write_fan_pwm(target, reason="curve_tick")
        if ok:
            self._last_decision_ts = time.monotonic()
        return ok

    # -- MetricSampler hookup ---------------------------------------
    def _connect_sampler(self) -> None:
        """挂到全应用唯一的性能采样器（与 PowerBlock GPU 浮动同款）。

        后台线程驱动 curve tick，不受窗口焦点影响——失焦（游戏前台）时
        HUD 的 _slow 刷新定时器被停掉，但此处仍照常运行。
        """
        try:
            from ..core.metrics import MetricSampler
            sampler = MetricSampler.instance()
            sampler.unregister_callback(self._on_sampler_sample)
            sampler.register_callback(self._on_sampler_sample)
            if sys.platform == "win32":
                sampler.start()
        except Exception:  # pragma: no cover
            pass

    def _on_sampler_sample(self, snap) -> None:
        """MetricSampler 回调：1Hz（采样器节奏 ~1-1.5s）跑一次 curve tick。

        在后台采样线程执行，全程持 ``_tick_lock``（RLock，允许与 GUI 线程
        的 mode 切换重入），保证运行时状态读写线程安全。
        """
        if self.mode != MODE_CURVE:
            return
        # 节流：update_hz=1 → 至少 1.0s 间隔，避免和 GPU 浮动 tick 撞车抢
        # WinRing0 互斥。MA 也是固定 1Hz。
        now = time.monotonic()
        min_interval = 1.0 / max(1, self.update_hz)
        if (now - self._last_decision_ts) < min_interval:
            return
        with self._tick_lock:
            if self.mode != MODE_CURVE:
                return
            self._fan_tick_locked()

    # -- 退出兜底：交还 EC 控制权给 BIOS -------------------------------
    def _restore_auto_on_exit(self) -> None:
        """退出兜底：无条件把 EC 控制权交还 BIOS（auto）。

        与 ``dispose`` / ``atexit`` 共用，幂等（``self._exit_restored`` 保证整个
        进程生命周期内最多真正写一次）。即便 ``closeEvent`` 没触发（崩溃 / 被
        任务管理器强杀 / 解释器级退出），也能在进程终止时尽量把风扇控制权归还
        BIOS，避免退出后风扇被钉死在手动转速。

        鲁棒性：EC 跨进程互斥可能被 ryzenadj 占住导致单次写入失败，退出时多试
        几次（每次间隔 50ms，最多 ~150ms），不长时间阻塞收尾。
        """
        if getattr(self, "_exit_restored", False):
            return
        self._exit_restored = True
        try:
            if not self._ec_health():
                return
            for _ in range(3):
                if self._restore_auto():
                    return
                time.sleep(0.05)
        except Exception:  # noqa: BLE001
            pass

    # -- 对称生命周期 ---------------------------------------------------
    def dispose(self) -> None:
        """注销 MetricSampler 回调 + 关 thermal probe + 交还 EC 控制权给 BIOS。

        由主窗口 closeEvent 调用，是「退出时交还风扇控制权」的主路径。"""
        try:
            from ..core.metrics import MetricSampler
            MetricSampler.instance().unregister_callback(self._on_sampler_sample)
        except Exception:  # noqa: BLE001
            pass
        # 退出前无条件把 EC 控制权交回 BIOS（auto）。
        # 不再按 mode 门控：即便用户把模式切到 AUTO 但没按 Apply、或上次退出前
        # 处于 manual/curve/silent，物理上 EC 仍被程序占用，退出时都必须归还；
        # 对已处于 AUTO 的机器，写 ec_enable=0 / pwm=0 是幂等 no-op。
        # 复用 _restore_auto_on_exit（含 EC 互斥重试 + 幂等保护），与 atexit
        # 兜底路径共用同一逻辑，保证任何退出方式都只真正交还一次。
        self._restore_auto_on_exit()
        try:
            self._thermal.dispose()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._ec.shutdown()
        except Exception:  # noqa: BLE001
            pass

    # -- mode toggles ---------------------------------------------------
    def set_mode(self, mode: str) -> None:
        """切换 mode（不立即写 EC；用户需按 Apply 应用）。"""
        if mode not in _VALID_MODES:
            return
        self.mode = mode
        self.applied = False
        self._last_band_idx = -1  # 重置滞回状态
        self._render()

    def set_manual_pct(self, pct: int) -> None:
        self.manual_pct = self._clamp_pct(pct)
        self.applied = False
        self._render()

    def set_silent_pct(self, pct: int) -> None:
        self.silent_pct = self._clamp_pct(pct)
        self.applied = False
        self._render()

    def set_hysteresis_c(self, c: int) -> None:
        self.hysteresis_c = max(0, min(10, int(c)))
        self._last_band_idx = -1
        self.applied = False
        self._render()

    # -- preset snapshot / restore -----------------------------------------
    def _preset_label(self) -> str:
        """风扇档位的预设命名片段：模式 + 数值（手动/静音带 %）。"""
        if self.mode == MODE_MANUAL:
            return f"手动{self.pwm_to_pct(self.manual_pct)}%"
        if self.mode == MODE_SILENT:
            return f"静音{self.pwm_to_pct(self.silent_pct)}%"
        if self.mode == MODE_CURVE:
            return "曲线"
        return "自动"

    def on_export_preset(self) -> dict[str, Any]:
        """导出当前风扇预设值：模式 + 当前模式对应的 PWM 数值 + 命名片段。"""
        pct = 0
        if self.mode == MODE_MANUAL:
            pct = self.manual_pct
        elif self.mode == MODE_SILENT:
            pct = self.silent_pct
        return {
            "mode": self.mode,
            "pct": int(pct),
            "label": self._preset_label(),
        }

    def on_apply_preset(self, data: dict[str, Any]) -> None:
        """从预设回填风扇：恢复模式与对应 PWM 数值，静默下发到 EC。"""
        if not isinstance(data, dict):
            return
        mode = data.get("mode", self.mode)
        if mode not in _VALID_MODES:
            mode = self.mode
        pct = int(data.get("pct", 0))
        if mode == MODE_MANUAL:
            self.set_manual_pct(pct)
        elif mode == MODE_SILENT:
            self.set_silent_pct(pct)
        self.set_mode(mode)
        self.apply(silent=True)

    def set_auto_detect(self, enabled: bool) -> None:
        self.auto_detect = bool(enabled)
        if self.auto_detect:
            self.detect_device(silent=True)
        self._render()

    def set_rpm_addr(self, addr: int) -> None:
        self.ec_rpm_addr = max(0, int(addr))
        self.applied = False
        self._render()

    # -- RPM (实际转速读取) ---------------------------------------------
    def read_fan_rpm(self) -> Optional[int]:
        """读取当前风扇实际转速（RPM）。

        通过 EC 转速计寄存器（16-bit 小端，地址 ec_rpm_addr / +1）读取。
        EC 不可用 / 地址为 0 / 读数不合理（≤0 或 >30000）时返回 None。
        永不抛异常。
        """
        addr = self.ec_rpm_addr
        if not addr:
            return None
        try:
            if not self._ec_health():
                return None
            lo = self._ec.read_ec(addr)
            hi = self._ec.read_ec(addr + 1)
        except Exception:  # noqa: BLE001
            return None
        if lo is None or hi is None:
            return None
        rpm = (hi << 8) | lo
        if rpm <= 0 or rpm > 30000:
            return None
        return rpm

    def estimate_rpm(self, pwm: int) -> int:
        """PWM → RPM 粗略换算（仅在读不到真实转速计时使用）。"""
        return int(self._clamp_pct(pwm) / self.PWM_MAX * self.MAX_RPM)

    def fan_rpm_display(self) -> str:
        """卡片/编辑项展示用的转速文本：优先真实转速，否则 PWM 估算。

        全部以「占最大转速的百分比」为单位，不出现 PWM/255 字样。
        """
        rpm = self.read_fan_rpm()
        if rpm is not None:
            pct = max(0, min(100, round(rpm / self.MAX_RPM * 100)))
            return f"≈{pct}%（≈{rpm} RPM）"
        if self.mode == MODE_CURVE:
            pwm = self.last_pwm
        elif self.mode == MODE_MANUAL:
            pwm = self.manual_pct
        elif self.mode == MODE_SILENT:
            pwm = self.silent_pct
        else:
            pwm = 0
        est = self.estimate_rpm(pwm)
        pct = self.pwm_to_pct(pwm)
        return f"≈{pct}%（≈{est} RPM）"

    # -- curve editing (供编辑面板调用) --------------------------------
    def set_curve(self, points: list) -> None:
        """Replace the whole curve with a list of {temp:int, pwm:int}."""
        self.curve = [{"temp": int(p["temp"]),
                       "pwm": self._clamp_pct(int(p["pwm"]))} for p in points]
        self._last_band_idx = -1
        self.applied = False
        self._render()

    def set_curve_point(self, idx: int, temp: int, pwm: int) -> None:
        if 0 <= idx < len(self.curve):
            self.curve[idx] = {"temp": int(temp),
                               "pwm": self._clamp_pct(int(pwm))}
            self._last_band_idx = -1
            self.applied = False
            self._render()

    def add_curve_point(self, temp: int, pwm: int) -> None:
        self.curve.append({"temp": int(temp),
                           "pwm": self._clamp_pct(int(pwm))})
        self._last_band_idx = -1
        self.applied = False
        self._render()

    def remove_curve_point(self, idx: int) -> None:
        # 至少保留 2 个拐点（否则曲线退化成常量，失去温度响应能力）
        if 0 <= idx < len(self.curve) and len(self.curve) > 2:
            del self.curve[idx]
            self._last_band_idx = -1
            self.applied = False
            self._render()

    def sort_curve(self) -> None:
        """按温度升序原地排序曲线点（稳定排序，相等温度保持原相对顺序）。

        仅排序并重置档位缓存，不触发 _render / 写盘 —— 由编辑面板决定何时
        刷新 UI（键盘输入期间不应重排，否则输入框焦点丢失、无法连续输入）。
        save_config 导出前确保有序，下次打开即按温度排列。
        """
        self.curve.sort(key=lambda p: p["temp"])
        self._last_band_idx = -1
        self.applied = False

    def _step_slider(self, direction: int) -> None:
        if self.adjust_target == "pwm":
            self.manual_pct = self._clamp_pct(
                self.manual_pct + (5 if direction > 0 else -5))
        self.applied = False
        self.update_slider_ui()
        self.update_status()

    # -- UI rendering --------------------------------------------------
    def update_slider_ui(self) -> None:
        plabel = self.ui.get("pwm_value")
        if plabel is not None:
            plabel.text = f"{self.pwm_to_pct(self.manual_pct)}%"

    def update_status(self) -> None:
        lbl = self.ui.get("status_label")
        if lbl is None:
            return
        if self.applied:
            if self.mode == MODE_AUTO:
                lbl.text = "✓ BIOS 自动"
                lbl.fore_color = COLOR_GREEN
            elif self.mode == MODE_MANUAL:
                lbl.text = (f"✓ 手动 {self.pwm_to_pct(self.manual_pct)}%"
                            f" · {self.fan_rpm_display()}")
                lbl.fore_color = COLOR_GREEN
            elif self.mode == MODE_CURVE:
                t = self.last_temp_c
                if t is not None:
                    src_tag = {"acpi": "ACPI", "hwinfo": "HWiNFO", "ec": "EC"}.get(
                        self._temp_src_used, "ACPI")
                    # 降级时必须把警示拼进同一行：ACPI 皮肤温度比 CPU 结温低
                    # 10~20℃，风扇转速会被算低。静默回落 = 「80℃ 风扇仍低转」
                    # 老坑复现，用户却只看到一行正常的绿色「✓ 曲线」。
                    warn = self.temp_degraded_text
                    if warn:
                        lbl.text = (f"⚠ 曲线 · {src_tag} {t:.0f}℃"
                                    f" · {self.fan_rpm_display()} · {warn}")
                        # 仍是「已应用」，但读数不可信 → 用警示色而非绿色。
                        lbl.fore_color = COLOR_RED
                    else:
                        lbl.text = (f"✓ 曲线 · {src_tag} {t:.0f}℃"
                                    f" · {self.fan_rpm_display()}")
                        lbl.fore_color = COLOR_GREEN
                else:
                    lbl.text = "✓ 曲线 · 温度读取中"
                    lbl.fore_color = COLOR_GREEN
            elif self.mode == MODE_SILENT:
                if self.silent_pct <= 0:
                    lbl.text = "⚠ 风扇停转"
                else:
                    lbl.text = (f"⚠ 静音 {self.pwm_to_pct(self.silent_pct)}%"
                                f" · {self.fan_rpm_display()}")
                lbl.fore_color = COLOR_RED
        else:
            if self.last_ec_error:
                lbl.text = "✗ " + self.last_ec_error
                lbl.fore_color = COLOR_RED
            else:
                lbl.text = "待应用"
                lbl.fore_color = COLOR_YELLOW

    def update_live_readout(self) -> None:
        """被 UI 慢定时器调用：刷新实时温度/转速读数（不写 EC，只读显示）。"""
        temp = self._read_temp()
        if temp is not None:
            self.last_temp_c = temp
        self.update_status()

    def _render(self) -> None:
        self.pwm_slider.value = self.manual_pct
        # Mode 按钮：当前 mode 用 GREEN solid，其他用 RED frosted（与 GPU 锁同款视觉）
        for btn, m in ((self.auto_btn, MODE_AUTO), (self.manual_btn, MODE_MANUAL),
                       (self.curve_btn, MODE_CURVE), (self.silent_btn, MODE_SILENT)):
            st = get_button_state(COLOR_GREEN if self.mode == m else COLOR_RED,
                                  "solid" if self.mode == m else "frosted")
            btn.set_colors(st["Back"], st["Hover"], st["Border"], st["Fore"])
        apply_st = get_button_state(self.theme_color,
                                     "solid" if self.applied else "frosted")
        self.apply_btn.set_colors(apply_st["Back"], apply_st["Hover"],
                                  apply_st["Border"], apply_st["Fore"])
        self.update_slider_ui()
        self.update_status()
        self._update_slider_visual()

    def _update_slider_visual(self) -> None:
        s = self.pwm_slider
        if self.in_item_mode and self.slider_adjust and self.adjust_target == "pwm":
            s.border_color = COLOR_YELLOW
            s.border_thickness = 3
        else:
            s.border_color = None
            s.border_thickness = None
        if hasattr(s, "invalidate"):
            s.invalidate()

    # -- theme -------------------------------------------------------
    def _set_title_color(self, color: Color) -> None:
        title = self.ui.get("title")
        if title is not None:
            title.fore_color = color

    def on_theme_change(self, accent: dict[str, Any]) -> None:
        self.theme_color = get_theme_accent()
        self._set_title_color(self.theme_color)
        sl = get_slider_colors(self.theme_color)
        t = self.ui.get("pwm_track")
        if t is not None:
            t.back_color = sl["Track"]
        f = self.ui.get("pwm_fill")
        if f is not None:
            f.back_color = sl["Fill"]
        th = self.ui.get("pwm_thumb")
        if th is not None and hasattr(th, "set_colors"):
            th.set_colors(sl["ThumbBack"], sl["ThumbHover"],
                          sl["ThumbBorder"], sl["ThumbFore"])
        self._render()

    # -- navigation ---------------------------------------------------
    def on_activate_item(self) -> None:
        self.in_item_mode = True
        self.focus_idx.value = 0
        self.slider_adjust = False
        self.adjust_target = None
        set_control_focus_visual(self.pwm_slider, True)

    def on_deactivate_item(self) -> None:
        self.in_item_mode = False
        self.slider_adjust = False
        self.adjust_target = None
        clear_block_focus([i for i in self._items() if i is not None])
        self._render()

    # -- toast -------------------------------------------------------
    def show_toast(self, text: str, success: bool = True) -> None:
        _show_block_toast(self.ui, text, success)

    # -- registration --------------------------------------------------
    def register(self, registry: Any) -> None:
        bp = BlockBlueprint(
            block_type="fan",
            name="Fan Control",
            color=COLOR_MAUVE,
            card=self.ui.get("card"),
            hooks={
                "GetCardHeight": lambda: 200,
                "OnRefresh": self.update_live_readout,
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
        """Short live readout for the smoke report."""
        parts = [f"Mode {self.mode}"]
        if self.mode == MODE_MANUAL:
            parts.append(f"{self.pwm_to_pct(self.manual_pct)}%")
        elif self.mode == MODE_CURVE and self.last_temp_c is not None:
            parts.append(f"CPU {self.last_temp_c:.0f}℃")
            parts.append(f"{self.pwm_to_pct(self.last_pwm)}%")
        if self.applied:
            parts.append("已应用")
        else:
            parts.append("待应用")
        return " · ".join(parts)