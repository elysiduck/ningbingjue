"""Configuration management — ``layout.json`` load/save + dataclasses.

Mirrors the config dict the original script keeps in memory and persists to
``layout.json``.  All fields are optional with sensible defaults so partial
configs still load.
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from .color.palette import BG_CARD
from .color.rgb import Color


# ---------------------------------------------------------------------------
# Robust scalers — these helpers are the *single source of truth* for parsing
# user-facing config fields that previously crashed on bad input.
#
# History (2026-08-12 P0 修复):
#   ``from_dict`` used to call ``int(d["FocusOpacity"])`` directly — if the
#   user hand-edited ``layout.json`` and wrote ``"abc"`` / ``""`` / ``true``,
#   the whole ``from_dict`` raised ``ValueError`` and ``ConfigManager.load``
#   silently fell back to ``AppConfig()``, losing EVERY field (cards, power,
#   tiles, presets…) not just the bad one.  Likewise ``build_focus_style``
#   called ``int(hexv[0:2], 16)`` after a length-only check, so a 6-char-but-
#   non-hex string like ``"gghhii"`` crashed the *entire app startup*.
#
#   These helpers move the editor's defensive parsing (``_coerce_focus_opacity``
#   originally lived in ``ui/button_edit.py``) down into ``core`` so the load
#   path and the editor path share the same truth.  ``ui.button_edit`` re-
#   exports them for back-compat with existing tests.
# ---------------------------------------------------------------------------

def _safe_hex(raw: object, default: str = "e6ff1e") -> str:
    """Return a 6-char lowercase hex string suitable for ``#rrggbb``.

    Accepts anything (str / None / int / …), strips whitespace, truncates to
    6 chars, and verifies every char is a valid hex digit.  On any failure
    (wrong length, non-hex char, non-string) returns ``default``.
    """
    if raw is None:
        return default
    try:
        s = str(raw).strip().lower()
    except Exception:
        return default
    if len(s) != 6:
        return default
    for c in s:
        if c not in "0123456789abcdef":
            return default
    return s


def _safe_align(raw: object, default: str = "right") -> str:
    """Coerce a window-alignment value to ``"left"`` or ``"right"``.

    Anything that is not exactly ``"left"`` (case-insensitive) falls back to
    ``"right"`` — the original right-rail behaviour and the safe default.
    """
    try:
        s = str(raw).strip().lower()
    except Exception:
        return default
    return "left" if s == "left" else "right"


#: 合法页面转场模式。实现见 ui/page_transition.py。
TRANSITION_MODES = ("slide", "fade", "none")


def _normalize_transition_mode(raw: object, default: str = "slide") -> str:
    """Coerce a page-transition mode to one of :data:`TRANSITION_MODES`.

    Anything unknown (including a missing / empty value in an older
    ``layout.json``) falls back to ``"slide"``.  ``"none"`` is the escape hatch
    that restores the original hard-cut behaviour with zero animation code on
    the page-switch path.

    只接受 str：``None`` 若走 ``str(None)`` 会变成 ``"None"`` → ``"none"``，
    等于把「字段缺失」误判成「用户主动关闭动画」。
    """
    if not isinstance(raw, str):
        return default
    s = raw.strip().lower()
    return s if s in TRANSITION_MODES else default


def _coerce_focus_opacity(raw: object, default: int = 25) -> int:
    """Coerce a saved focus-opacity value to int 0..100.

    The *legitimate* value 0 must be preserved — it is falsy, so a naive
    ``raw or default`` would wrongly replace it with ``default`` (the bug that
    made the editor slider snap to 25 whenever the saved opacity was 0).
    Only a genuinely missing / ``None`` / empty / non-numeric value falls back
    to ``default``.  Out-of-range values are clamped to [0, 100].
    """
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, min(100, val))


def _coerce_int(raw: object, default: int) -> int:
    """Like ``_coerce_focus_opacity`` but without the [0,100] clamp and with a
    mandatory ``default`` (used for tray_opacity / tray_blur / page_breaks
    where the valid range is field-specific).  ``None`` / ``""`` / non-numeric
    → ``default``."""
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _coerce_float(raw: object, default: float) -> float:
    """Float counterpart of ``_coerce_int`` (used for breath_max_brightness)."""
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _rgb_csv_to_color(csv: str | None, default: Color) -> Color:
    if not csv:
        return default
    parts = [p.strip() for p in csv.split(",")]
    if len(parts) == 3:
        try:
            return Color.from_rgb(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            return default
    return default


@dataclass
class Tile:
    launch_type: str = ""
    launch_path: str = ""
    width: int = 1
    tile_name_color: str = "255,255,255"
    icon_path: str = ""
    row: int = 0
    name: str = "新磁贴"
    height: int = 1
    block_name: str = ""
    show_block_title: bool = True
    col: int = 0
    show_tile_name: bool = True
    border_color: str = ""
    bg_color: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Tile":
        return cls(
            launch_type=d.get("LaunchType", ""),
            launch_path=d.get("LaunchPath", ""),
            width=_coerce_int(d.get("Width", 1), 1),
            tile_name_color=d.get("TileNameColor", "255,255,255"),
            icon_path=d.get("IconPath", ""),
            row=_coerce_int(d.get("Row", 0), 0),
            name=d.get("Name", "新磁贴"),
            height=_coerce_int(d.get("Height", 1), 1),
            block_name=d.get("BlockName", ""),
            show_block_title=bool(d.get("ShowBlockTitle", True)),
            col=_coerce_int(d.get("Col", 0), 0),
            show_tile_name=bool(d.get("ShowTileName", True)),
            border_color=d.get("BorderColor", ""),
            bg_color=d.get("BgColor", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "LaunchType": self.launch_type,
            "LaunchPath": self.launch_path,
            "Width": self.width,
            "TileNameColor": self.tile_name_color,
            "IconPath": self.icon_path,
            "Row": self.row,
            "Name": self.name,
            "Height": self.height,
            "BlockName": self.block_name,
            "ShowBlockTitle": self.show_block_title,
            "Col": self.col,
            "ShowTileName": self.show_tile_name,
            "BorderColor": self.border_color,
            "BgColor": self.bg_color,
        }


@dataclass
class PowerConfig:
    # 打包默认值（出厂值）。GPU 单位为 MHz，默认 700MHz；TDP 默认 10W；
    # GPU 浮动 / GPU 锁默认关闭。make_installer 默认不打包任何本机配置，
    # 因此安装后的程序即以这些代码默认值运行（详见 make_installer.py 顶部
    # INCLUDE_CONFIG 说明）。
    gpu: int = 700
    tdp: int = 10
    tdp_lock: bool = False
    gpu_float_enabled: bool = False
    gpu_lock: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PowerConfig":
        return cls(
            gpu=_coerce_int(d.get("Gpu", 700), 700),
            tdp=_coerce_int(d.get("Tdp", 10), 10),
            tdp_lock=bool(d.get("TdpLock", False)),
            gpu_float_enabled=bool(d.get("GpuFloatEnabled", False)),
            gpu_lock=bool(d.get("GpuLock", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "Gpu": self.gpu, "Tdp": self.tdp, "TdpLock": self.tdp_lock,
            "GpuFloatEnabled": self.gpu_float_enabled, "GpuLock": self.gpu_lock,
        }


# ---------------------------------------------------------------------------
# 配置预设（移植自 YeManCC 的「档位/电源计划」概念，重做为自由命名的预设系统）。
# 见 blocks/preset.py。每个预设快照 电源 / 锁帧 / 电源计划 / 风扇 四卡的值；
# 点列表项即把四卡改回该预设。预设名 = 组合值（TDP-电源计划-AT帧数-GPU上限-风扇）。
# ---------------------------------------------------------------------------
@dataclass
class PresetSpec:
    """单个配置预设。各域字典直接对应源块 OnExportPreset/OnApplyPreset 的契约。"""
    name: str = ""
    power: dict[str, Any] = field(default_factory=dict)       # tdp, gpu_ceiling, gpu_float, tdp_lock, gpu_lock
    rtss: dict[str, Any] = field(default_factory=dict)        # at_enabled, at_fps
    fan: dict[str, Any] = field(default_factory=dict)         # mode, pct
    powerplan: dict[str, Any] = field(default_factory=dict)   # name

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PresetSpec":
        return cls(
            name=str(d.get("Name", "")),
            power=dict(d.get("Power", {}) or {}),
            rtss=dict(d.get("Rtss", {}) or {}),
            fan=dict(d.get("Fan", {}) or {}),
            powerplan=dict(d.get("PowerPlan", {}) or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "Name": self.name,
            "Power": self.power,
            "Rtss": self.rtss,
            "Fan": self.fan,
            "PowerPlan": self.powerplan,
        }


@dataclass
class PresetsConfig:
    """配置预设列表（持久化于 layout.json 的 "PresetsConfig"）。"""
    presets: list[PresetSpec] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PresetsConfig":
        raw = d.get("Presets", []) or []
        presets = [PresetSpec.from_dict(x) for x in raw if isinstance(x, dict)]
        return cls(presets=presets)

    def to_dict(self) -> dict[str, Any]:
        return {"Presets": [p.to_dict() for p in self.presets]}


@dataclass
class PowerPlanConfig:
    """用户自主导入的 Windows 电源方案（GUID + 名称）。

    三个预设方案（YMSilent / YMPerformance / YMMedium，随包
    blocks/powerplans/*.pow）由 PowerPlanBlock 以固定 GUID 预导入，标记为
    不可移除，不在此持久化。
    """
    user_plans: list[dict[str, str]] = field(default_factory=list)
    # 用户「主动选择」的电源计划（app 自有意图，必准）——与系统读回的 active_guid
    # 可能发散（例如 AC/DC 自动切走、Windows 重置计划）。持久化以便重启后仍能显示
    # 「你的选择 vs 系统实际」的偏离告警。
    selected_guid: str = ""
    selected_name: str = ""
    # 电源计划卡片「最大频率」滑块的目标值(MHz)，跨重启持久化（拖拽即保存、按 Y 应用）。
    # 0 表示未设置（滑块初值回退到当前活动计划的实际最大频率）。
    max_freq_mhz: int = 0
    # 电源计划卡片「最大处理器状态」滑块的目标值(%)——PROCTHROTTLEMAX(bc5038f7)，
    # AMD Ryzen 上真正可靠的调频旋钮。100 表示不限(=默认 100%)，跨重启持久化。
    max_state_pct: int = 100

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PowerPlanConfig":
        raw = d.get("UserPlans", []) or []
        plans = [{"guid": str(x.get("guid", "")), "name": str(x.get("name", "")),
                  "pow": str(x.get("pow", ""))}
                 for x in raw if isinstance(x, dict) and x.get("guid")]
        return cls(
            user_plans=plans,
            selected_guid=str(d.get("SelectedGuid", "") or ""),
            selected_name=str(d.get("SelectedName", "") or ""),
            max_freq_mhz=_coerce_int(d.get("MaxFreqMhz", 0), 0),
            max_state_pct=_coerce_int(d.get("MaxStatePct", 100), 100),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"UserPlans": [{"guid": p["guid"], "name": p["name"],
                                "pow": p.get("pow", "")}
                               for p in self.user_plans],
                "SelectedGuid": self.selected_guid,
                "SelectedName": self.selected_name,
                "MaxFreqMhz": self.max_freq_mhz,
                "MaxStatePct": self.max_state_pct}


@dataclass
class SmtConfig:
    """SMT 热核隔离卡片的持久化配置。

    enabled        : 是否开启"热核检测 + 自动施加"（即功能卡片主页的 SMT自动）。
    saturated_thr  : 热核判定阈值(%)——单核占用≥此值才视为"饱和"，配合离群判定
                     决定是否施加。功能卡片编辑项里的"热核判定阈值"滑块即此值。
    min_standout   : 离群下限(pp)，热核超出簇内其余核均值的量需≥此才判定单核孤峰。
    k_sigma        : 备选 2σ 离群系数（仅报告显示）。
    active_floor   : 其余核占用≥此值才算"激活核"，参与均值/标准差。
    cluster_scope  : 是否按 EfficiencyClass 簇作用域比较（锁单簇运行时排除 parked 簇）。
    """

    enabled: bool = False
    saturated_thr: float = 80.0
    min_standout: float = 30.0
    k_sigma: float = 2.0
    active_floor: float = 5.0
    cluster_scope: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SmtConfig":
        return cls(
            enabled=bool(d.get("Enabled", False)),
            saturated_thr=_coerce_float(d.get("SaturatedThr", 80.0), 80.0),
            min_standout=_coerce_float(d.get("MinStandout", 30.0), 30.0),
            k_sigma=_coerce_float(d.get("KSigmal", 2.0), 2.0),
            active_floor=_coerce_float(d.get("ActiveFloor", 5.0), 5.0),
            cluster_scope=bool(d.get("ClusterScope", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "Enabled": self.enabled,
            "SaturatedThr": self.saturated_thr,
            "MinStandout": self.min_standout,
            "KSigmal": self.k_sigma,
            "ActiveFloor": self.active_floor,
            "ClusterScope": self.cluster_scope,
        }


@dataclass
class FanConfig:
    """风扇控制卡片的持久化配置（参考 MotionAssistant FanSettings 简化版）。

    字段语义：
      mode          : "auto" / "manual" / "curve" / "silent"
                      - auto    = EC 控制权交还 BIOS（写 ec_enable_addr=0）
                      - manual  = 固定 manual_pct 转速
                      - curve   = 按 curve 点表插值，1Hz tick + hysteresis_c 滞回
                      - silent  = 风扇停转（写 ec_pwm_addr=0，危险但用户要的）
      manual_pct    : 0-255 EC 寄存器值（manual 模式下写入 ec_pwm_addr）
      silent_pct    : 0-255 EC 寄存器值（silent 模式固定转速；0=停转）。
                      之前 silent 硬编码写 0，现改为可配置的「静音固定转速」。
      curve         : 温度→PWM 点表 [{temp:int, pwm:int}]；按温度升序排，
                      最末档若 <100℃ 则隐式补 (95, 255) 防止过热无风扇。
                      默认 4 拐点 (45/60/70/80℃) 对齐 MA FanSettings。
      hysteresis_c  : 滞回温度（℃），避免风扇在阈值附近抽搐（MA 同款 3℃）。
      update_hz     : curve 模式调度频率。固定 1Hz（与 MA FanControl 线程同款）。
      ec_enable_addr: EC「使能手动控制」寄存器地址。HX370 默认 629(0x275)，
                      对齐 MA 反编译 ``ECRamWrite4E4F(629, 1)`` 调用。
                      0 表示「无独立使能寄存器」（如 Win4/Win Mini，直写转速）。
      ec_pwm_addr   : EC「转速寄存器」地址。HX370 默认 6153(0x1809)，
                      对齐 MA ``ECRamWrite4E4F(6153, speed)``。
      board_quirk   : 机型变体标签（"wm2" / "win_mini" / "win4" / "duo"），
                      决定 SIO 协议（win4→2e2f，其余→4e4f）与寄存器映射。
      auto_detect   : True 时启动自动读取 WMI 机型并匹配寄存器映射（MA 风格
                      机型查表）；False 时沿用 board_quirk/地址 的手动配置。
      detected_model: 最近一次自动检测到的系统型号字符串（仅展示/诊断用，
                      不参与控制决策）。
      ec_rpm_addr   : EC「风扇转速计」低字节寄存器地址（16-bit 小端，转速 =
                      read(rpm_addr) | read(rpm_addr+1)<<8）。0 = 未设置 →
                      运行时回落到 pwm_addr+1（ITE EC 常见布局）。读不到合理
                      转速时可在此手动指定正确寄存器。
      temp_source   : 曲线/调速使用的**温度来源**，三选一（见 blocks/fan.py
                      _read_temp）：
                        - "acpi"  : 默认。WMI MSAcpi_ThermalZoneTemperature
                                    最热 ACPI 热区。零依赖，但读的是主板/外壳
                                    热区，掌机上常比 CPU 核心(Tctl/Tdie)低
                                    10~20℃，会导致「CPU 已 80℃ 风扇仍低转」。
                        - "hwinfo": HWiNFO 共享内存的 CPU(Tctl/Tdie) 温度，
                                    与 HWiNFO 面板读数一致，最准；需 HWiNFO
                                    在跑且开 Shared Memory。读不到/陈旧自动
                                    回落 acpi。
                        - "ec"    : 直接读 EC 温度寄存器(ec_temp_addr)，GPD
                                    原生、零外部依赖（对齐 MotionAssistant）。
                                    地址未知时填 0 即回落 acpi。
      ec_temp_addr  : EC「CPU 温度」低字节寄存器地址（摄氏度，0~125 合理）。
                      仅 temp_source="ec" 时生效；0 = 未设置 → 回落 acpi。
    """
    mode: str = "auto"
    manual_pct: int = 128
    silent_pct: int = 0
    curve: list = field(default_factory=lambda: [
        {"temp": 45, "pwm": 0},
        {"temp": 60, "pwm": 80},
        {"temp": 70, "pwm": 140},
        {"temp": 80, "pwm": 200},
        {"temp": 90, "pwm": 255},
    ])
    hysteresis_c: int = 3
    update_hz: int = 1
    ec_enable_addr: int = 629
    ec_pwm_addr: int = 6153
    board_quirk: str = "wm2"
    auto_detect: bool = True
    detected_model: str = ""
    ec_rpm_addr: int = 0
    temp_source: str = "acpi"
    ec_temp_addr: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FanConfig":
        raw_curve = d.get("Curve", []) or []
        curve: list = []
        if isinstance(raw_curve, list):
            for pt in raw_curve:
                if isinstance(pt, dict):
                    try:
                        curve.append({
                            "temp": int(pt.get("Temp", 0)),
                            "pwm": int(pt.get("Pwm", 0)),
                        })
                    except (TypeError, ValueError):
                        continue
        if not curve:
            curve = [
                {"temp": 45, "pwm": 0},
                {"temp": 60, "pwm": 80},
                {"temp": 70, "pwm": 140},
                {"temp": 80, "pwm": 200},
                {"temp": 90, "pwm": 255},
            ]
        return cls(
            mode=str(d.get("Mode", "auto")),
            manual_pct=int(d.get("ManualPct", 128) or 128),
            silent_pct=int(d.get("SilentPct", 0) or 0),
            curve=curve,
            hysteresis_c=int(d.get("HysteresisC", 3) or 3),
            update_hz=int(d.get("UpdateHz", 1) or 1),
            ec_enable_addr=int(d.get("EcEnableAddr", 629) or 629),
            ec_pwm_addr=int(d.get("EcPwmAddr", 6153) or 6153),
            board_quirk=str(d.get("BoardQuirk", "wm2")),
            auto_detect=bool(d.get("AutoDetect", True)),
            detected_model=str(d.get("DetectedModel", "") or ""),
            ec_rpm_addr=int(d.get("EcRpmAddr", 0) or 0),
            temp_source=str(d.get("TempSource", "acpi") or "acpi"),
            ec_temp_addr=int(d.get("EcTempAddr", 0) or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "Mode": self.mode,
            "ManualPct": self.manual_pct,
            "SilentPct": self.silent_pct,
            "Curve": [{"Temp": p["temp"], "Pwm": p["pwm"]} for p in self.curve],
            "HysteresisC": self.hysteresis_c,
            "UpdateHz": self.update_hz,
            "EcEnableAddr": self.ec_enable_addr,
            "EcPwmAddr": self.ec_pwm_addr,
            "BoardQuirk": self.board_quirk,
            "AutoDetect": self.auto_detect,
            "DetectedModel": self.detected_model,
            "EcRpmAddr": self.ec_rpm_addr,
            "TempSource": self.temp_source,
            "EcTempAddr": self.ec_temp_addr,
        }


@dataclass
class ThemeConfig:
    bg_card: str = "28,28,28"
    name: str = "碳黑"
    decor_seed: int = 0
    bg_app: str = "16,16,16"
    edit_apply_theme: str = "Green"
    fg_primary: str = "225,225,225"
    edit_cancel_theme: str = "Red"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ThemeConfig":
        return cls(
            bg_card=d.get("BgCard", "28,28,28"),
            name=d.get("Name", "碳黑"),
            decor_seed=_coerce_int(d.get("DecorSeed", 0), 0),
            bg_app=d.get("BgApp", "16,16,16"),
            edit_apply_theme=d.get("EditApplyTheme", "Green"),
            fg_primary=d.get("FgPrimary", "225,225,225"),
            edit_cancel_theme=d.get("EditCancelTheme", "Red"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "BgCard": self.bg_card, "Name": self.name, "DecorSeed": self.decor_seed,
            "BgApp": self.bg_app, "EditApplyTheme": self.edit_apply_theme,
            "FgPrimary": self.fg_primary, "EditCancelTheme": self.edit_cancel_theme,
        }


@dataclass
class ExtraTileBlock:
    block_name: str = "新区块_1"
    show_block_title: bool = True
    tiles: list[Tile] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExtraTileBlock":
        return cls(
            block_name=d.get("BlockName", "新区块_1"),
            show_block_title=bool(d.get("ShowBlockTitle", True)),
            tiles=[Tile.from_dict(t) for t in d.get("Tiles", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "BlockName": self.block_name,
            "ShowBlockTitle": self.show_block_title,
            "Tiles": [t.to_dict() for t in self.tiles],
        }


def breath_speed_to_period(speed: int) -> float:
    """Map a legacy ``breath_speed`` (10=fast .. 100=slow) to a breathing
    period in seconds, matching the original timer mapping
    (``speed*0.6`` ms clamped to 15..60, phase step 0.08/tick).  Used only to
    migrate old configs that still carry a ``BreathSpeed`` value."""
    ms = max(15, min(60, int(speed) * 6 // 10))
    return (2 * math.pi * (ms / 1000.0)) / 0.08


@dataclass
class InputConfig:
    """Gamepad / keyboard binding + behaviour settings (the ``Input`` section
    of ``layout.json``).

    ``bindings`` maps a *logical action* to the SDL gamepad button that triggers
    it.  Button names are the ``GamepadState`` boolean fields reported by the
    SDL backend: ``a, b, x, y, l3, lb, rb``.  The defaults mirror the original
    PS1-style convention (A=确认 / B=取消 / X=删除 / Y=保存) and the ABXY hint
    badge colours already drawn in ``gamepad_hint.py`` — so changing defaults
    would also require updating those badges.
    """

    bindings: dict[str, str] = field(default_factory=lambda: {
        "confirm": "a",
        "cancel": "b",
        "delete": "x",
        "save": "y",
        "help": "l3",
        "prev_panel": "lb",
        "next_panel": "rb",
    })
    # ``Del`` minimizes the window to the tray only when no text input
    # (QLineEdit / QTextEdit) has focus — so typing Delete in the editor's
    # fields is never swallowed by the global hotkey.
    del_minimize_when_no_text_focus: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "InputConfig":
        raw = d.get("Bindings", {}) or {}
        defaults = {
            "confirm": "a", "cancel": "b", "delete": "x", "save": "y",
            "help": "l3", "prev_panel": "lb", "next_panel": "rb",
        }
        bindings = dict(defaults)
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, str):
                    bindings[str(k).lower()] = v.lower()
        return cls(
            bindings=bindings,
            del_minimize_when_no_text_focus=bool(
                d.get("DelMinimizeWhenNoTextFocus", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "Bindings": self.bindings,
            "DelMinimizeWhenNoTextFocus": self.del_minimize_when_no_text_focus,
        }


@dataclass
class TaskListItem:
    """A single task in the 任务列表 card.

    ``source`` is ``"manual"`` for user-added tasks and ``"agent"`` for tasks
    auto-added by an agent after completing a job (e.g. "查看 logs").
    ``done`` marks completion; ``note`` holds an optional detail (log path …).
    """

    id: str = ""
    text: str = ""
    done: bool = False
    source: str = "manual"   # "manual" | "agent"
    note: str = ""
    created_at: str = ""     # ISO-ish timestamp (local), best-effort

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskListItem":
        if not isinstance(d, dict):
            return cls()
        return cls(
            id=str(d.get("Id", "") or ""),
            text=str(d.get("Text", "") or ""),
            done=bool(d.get("Done", False)),
            source=str(d.get("Source", "manual") or "manual"),
            note=str(d.get("Note", "") or ""),
            created_at=str(d.get("CreatedAt", "") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "Id": self.id,
            "Text": self.text,
            "Done": self.done,
            "Source": self.source,
            "Note": self.note,
            "CreatedAt": self.created_at,
        }


@dataclass
class TaskListConfig:
    """Persisted state for the 任务列表 card (layout.json)."""

    items: list[TaskListItem] = field(default_factory=list)
    # When True, the agent-auto-added tasks are shown with an "🤖" tag so the
    # user can tell machine-generated reminders apart from their own notes.
    show_agent_tag: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskListConfig":
        if not isinstance(d, dict):
            return cls()
        raw = d.get("Items", []) or []
        items = [TaskListItem.from_dict(x) for x in raw if isinstance(x, dict)]
        return cls(
            items=items,
            show_agent_tag=bool(d.get("ShowAgentTag", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "Items": [x.to_dict() for x in self.items],
            "ShowAgentTag": self.show_agent_tag,
        }


@dataclass
class AppConfig:
    extra_tile_blocks: list[ExtraTileBlock] = field(default_factory=list)
    power_config: PowerConfig = field(default_factory=PowerConfig)
    presets_config: PresetsConfig = field(default_factory=PresetsConfig)
    powerplan_config: PowerPlanConfig = field(default_factory=PowerPlanConfig)
    smt_config: SmtConfig = field(default_factory=SmtConfig)
    fan_config: FanConfig = field(default_factory=FanConfig)
    # -- 任务列表 (主页可增删 / agent 自动填写) -------------------------
    tasklist_config: TaskListConfig = field(default_factory=TaskListConfig)
    # -- 更新内容 (展示上一次打包 -> 本次打包之间的差异) --------------
    # 用户上一次见过的版本号；与 build_info.json 的 version 比对以决定
    # 卡片是否高亮「有新更新」。首次运行 / 缺失时为 ""。
    last_seen_version: str = ""
    card_colors: dict[str, str] = field(default_factory=dict)
    tiles: list[Tile] = field(default_factory=list)
    procmon_configs: dict[str, Any] = field(default_factory=dict)
    theme: ThemeConfig = field(default_factory=ThemeConfig)
    cards: list[str] = field(default_factory=list)
    hidden_cards: list[str] = field(default_factory=list)
    page_breaks: list[int] = field(default_factory=lambda: [0])
    button_style: str = "ZZZ"
    # -- breathing light (customizable) -----------------------------------
    # Full breath period in seconds (one dark→bright→dark cycle).
    breath_period_s: float = 2.36
    # Glow colour as "rrggbb"; empty string means "follow the theme accent".
    breath_color: str = ""
    # Peak glow alpha (0.0 .. 1.0). The trough stays fixed at 0.2 so it keeps
    # breathing. 1.0 reproduces the original look.
    breath_max_brightness: float = 1.0
    # 呼吸谷值（最暗 alpha，0.0 .. 1.0）。None/缺省 = 沿用 core.timers._BREATH_FLOOR。
    # 与峰值一起决定呼吸落差；谷值越接近峰值，脉动越弱（相等即等于关闭脉动）。
    breath_floor: float | None = None
    # 获得焦点时「冲到最亮」的动画时长（毫秒）。
    breath_flash_ms: int = 180
    # 冲到最亮后的停留时长（毫秒），给一个「点亮」的确认感后转入正常呼吸。
    breath_hold_ms: int = 120
    # 失焦黑罩的淡出时长（毫秒）。获得焦点时黑罩淡出与灯冲亮同时开始。
    breath_scrim_ms: int = 200
    # Master on/off for the breathing pulse. When False the glow stays lit at a
    # steady (peak) brightness instead of pulsing — the theme colour still
    # applies everywhere, only the animation is disabled. Default on.
    breath_enabled: bool = True
    # -- card tray background (customizable in 呼吸灯自定义) ------------
    # Tray fill colour as "rrggbb".  Default matches the original dark panel.
    tray_color: str = "0e1117"
    # Tray fill opacity, 0..100 (percent).  Default 86 ≈ rgba(...,0.86).
    tray_opacity: int = 86
    # Whether the tray shows its 1px light border.
    # 2026-08-30: 语义升级为「外框线宽度 > 0」的派生值（旧配置只存布尔；
    # 新增 tray_border_width 后本字段保留，二者由 apply_tray_settings 保持
    # 一致：width>0 ⇔ True）。旧 bool True 映射 2px，False 映射 0px。
    tray_border: bool = True
    # 外框线宽度（px）。0 = 关闭；1..4 = 描边宽度。「主题与蒙版」页用滑条
    # 调节（替代旧的 开/关 切换）。
    tray_border_width: int = 2
    # 页面底色（整个 HUD 窗口的背景填充，即 _theme_bg 的覆盖值）。
    # "rrggbb"；空串 = 跟随主题 bg_app（默认行为，老配置升级零感知）。
    page_bg_color: str = ""
    # Gaussian blur radius (px) of the frosted desktop backdrop behind the card
    # area. 0 = sharp (no blur).  Customizable in 呼吸灯自定义.
    tray_blur: int = 18
    # -- 编辑页蒙版深度（页面布局编辑 + 其所有子页面）------------------
    # 编辑态会在共享托盘之上额外压一层**纯黑**，让编辑控件保持对比度（否则
    # 呼吸辉光会把编辑器背景冲淡、文字发灰）。0 = 不压暗（编辑页与主页同亮
    # 度，辉光完整透出）；255 = 全黑（辉光完全被吃掉）。默认 118 ≈ 46%，
    # 与 2026-08-29 之前硬编码的 QColor(0, 0, 0, 118) 完全一致。可在
    # 呼吸灯自定义 → 「编辑页蒙版深度」滑块实时调节。
    editor_dim_alpha: int = 118
    # -- freeze block (进程冻结) ----------------------------------------
    # When True, the freeze candidate list also shows "Windows noise" processes
    # (explorer, msedge, runtimebroker, …) that are safe to freeze but clutter
    # the list. Hard-dangerous processes (dwm, lsass, …) stay hidden regardless
    # of this flag — see FreezeBlock.EXCLUDED.
    freeze_show_windows_apps: bool = False
    # User-maintained list of process names (fnmatch patterns allowed) that are
    # never shown in the freeze candidate list.  Persisted to layout.json; the
    # built-in SERVICE_HELPERS blacklist lives in FreezeBlock and is separate.
    freeze_hidden: list[str] = field(default_factory=list)
    # When True, processes with a visible top-level window rank above background
    # processes of equal game/memory tier in the freeze candidate list.
    freeze_prioritize_foreground: bool = True
    # When True, a successful un-freeze also minimises the Ningbingjue window
    # (to tray / taskbar) so the user returns straight to the just-thawed app.
    freeze_minimize_on_unfreeze: bool = True
    # -- focus overlay (选中焦点蒙层) -----------------------------------
    # Colour of the selection/focus rectangle as "rrggbb". Default matches the
    # original lime-yellow (#e6ff1e). Applies to BOTH the border frame and the
    # tinted mask inside it.
    focus_color: str = "e6ff1e"
    # Fill (mask) opacity, 0..100 (percent). 0 = no mask, only the border frame
    # is drawn (the "just a frame" look). Default 25 ≈ rgba(230,255,30,0.25).
    focus_opacity: int = 25
    # -- input (gamepad / keyboard) -------------------------------------
    # Gamepad bindings + keyboard behaviour. See ``InputConfig``.
    input_config: InputConfig = field(default_factory=InputConfig)
    # -- window alignment -----------------------------------------------
    # Which screen edge the HUD window hugs: "left" or "right".  Right is the
    # original behaviour (a tall right rail).  Toggled from the layout editor's
    # header; persisted so the choice survives restarts.  Anything other than
    # "left" is coerced to "right" (the safe default).
    window_align: str = "right"
    # -- xinput gate (手柄隔离) ----------------------------------------
    # 总开关。启用后凝冰结魄前台时目标游戏读不到手柄。
    xinput_gate_enabled: bool = False
    # 已部署游戏列表。每个元素 {"exe": path, "mode": "dll_hijack"|"device_disable"}
    xinput_gate_games: list[dict[str, str]] = field(default_factory=list)
    # -- 卡片自定义背景（原型：不透明自渲染，落稳定面→清晰）-----------
    # 启用后卡片托盘底用纯色或背景图替代默认深色，保留呼吸辉光与边框。
    # 默认关闭，不影响现有的 frosted 透出观感；开启后也不与游戏帧合成
    # （背景由 app 自渲染在稳定面上），故不恶化字体模糊、不抢帧预算。
    card_bg_enabled: bool = False
    card_bg_mode: str = "solid"  # "solid" | "image"
    card_bg_color: str = "0e1117"  # 纯色模式下的底色 "rrggbb"
    card_bg_image: str = ""  # 图片模式下的图片绝对路径（空=不加载）
    card_fill_color: str = "141821"  # 单卡填充色 "rrggbb"
    card_fill_alpha: int = 165        # 单卡填充不透明度 0-255
    # -- 主页面卡片排布（排版效果）-----------------------------------
    # 功能卡片之间的垂直间距（px）。默认 4（2026-08-29 起由 10 收紧）。
    card_gap: int = 4
    # 卡片区外圈留白（px），即主页面 inner_layout 的 contentsMargins。
    # 8 = 显示「卡片区背景」边框；调小更贴边。
    card_outer_margin: int = 8
    # -- 页面转场动画 ---------------------------------------------------
    # 切页时的转场方式："slide" 横向滑动 / "fade" 交叉淡化 / "none" 关闭
    # （走原硬切路径）。实现见 ui/page_transition.py：转场只绘制两张页面
    # 快照位图，每帧成本 O(1)、与卡片数量无关；任何异常一律降级为硬切。
    page_transition: str = "slide"
    # 转场时长（ms），clamp 到 [0, 600]。0 = 关闭动画。
    transition_ms: int = 180
    # -- 进入「页面布局编辑」的转场（HUD ↔ 编辑器全窗切换）----------------
    # 复用 ui/page_transition.py 的快照位图管线，但与卡片分页转场**独立**：
    # 走 _open_editor / _close_editor 的 QStackedWidget 硬切，默认交叉溶解。
    # 交叉溶解对 HUD↔编辑器两页的几何错位不敏感（不会像滑入那样暴露 22px
    # 高度差导致的拉伸闪烁）；想恢复右侧滑入可在 layout.json 设 EditorTransition:"slide"。
    # 模式同样白名单化（"slide" / "fade" / "none"），任何异常降级为硬切。
    editor_transition: str = "fade"
    # 进入编辑器转场时长（ms），clamp 到 [0, 600]。0 = 关闭动画。
    editor_transition_ms: int = 180

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AppConfig":
        raw_pm = d.get("ProcMonConfigs", {})
        if isinstance(raw_pm, list):
            procmon_configs = {
                c.get("Name", ""): {"Path": c.get("Path", "")}
                for c in raw_pm
            }
        else:
            procmon_configs = dict(raw_pm)
        # 外框线宽度：优先读 TrayBorderWidth；旧配置没有该字段时从布尔
        # TrayBorder 派生（True→2px，False→0px），升级零感知。
        # 注意 0 是合法值（关闭），不能用 `or` 兜底。
        if "TrayBorderWidth" in d:
            _tray_bw = _coerce_int(d.get("TrayBorderWidth"), 2)
        else:
            _tray_bw = 2 if bool(d.get("TrayBorder", True)) else 0
        _tray_bw = max(0, min(4, _tray_bw))
        # 页面转场模式白名单化：非法值 / 旧配置缺字段一律回落 "slide"。
        _page_transition = _normalize_transition_mode(d.get("PageTransition", "slide"))
        # 页面底色：只接受合法 6 位 hex（_safe_hex 校验字符合法性，P0-1 同款）；
        # 空/非法 = 跟随主题（存空串）。
        _page_bg = _safe_hex(d.get("PageBgColor", ""), "")
        return cls(
            extra_tile_blocks=[ExtraTileBlock.from_dict(b) for b in d.get("ExtraTileBlocks", [])],
            power_config=PowerConfig.from_dict(d.get("PowerConfig", {})),
            presets_config=PresetsConfig.from_dict(d.get("PresetsConfig", {})),
            powerplan_config=PowerPlanConfig.from_dict(d.get("PowerPlanConfig", {})),
            smt_config=SmtConfig.from_dict(d.get("SmtConfig", {})),
            fan_config=FanConfig.from_dict(d.get("FanConfig", {})),
            tasklist_config=TaskListConfig.from_dict(d.get("TaskListConfig", {})),
            last_seen_version=str(d.get("LastSeenVersion", "") or ""),
            card_colors=dict(d.get("CardColors", {})),
            tiles=[Tile.from_dict(t) for t in d.get("Tiles", [])],
            procmon_configs=procmon_configs,
            theme=ThemeConfig.from_dict(d.get("Theme", {})),
            cards=list(d.get("Cards", [])),
            hidden_cards=list(d.get("HiddenCards", [])),
            page_breaks=[_coerce_int(x, 0) for x in d.get("PageBreaks", [0])],
            button_style=d.get("ButtonStyle", "ZZZ"),
            breath_period_s=_read_breath_period(d),
            breath_color=str(d.get("BreathColor", "")).strip(),
            breath_max_brightness=_coerce_float(d.get("BreathMaxBrightness", 1.0), 1.0),
            breath_floor=_read_breath_floor(d),
            breath_flash_ms=_coerce_int(d.get("BreathFlashMs", 180), 180),
            breath_hold_ms=_coerce_int(d.get("BreathHoldMs", 120), 120),
            breath_scrim_ms=_coerce_int(d.get("BreathScrimMs", 200), 200),
            breath_enabled=bool(d.get("BreathEnabled", True)),
            tray_color=str(d.get("TrayColor", "0e1117")).strip() or "0e1117",
            tray_opacity=_coerce_int(d.get("TrayOpacity", 86) or 86, 86),
            tray_border=_tray_bw > 0,
            tray_border_width=_tray_bw,
            page_bg_color=_page_bg,
            tray_blur=_coerce_int(d.get("TrayBlur", 18) or 18, 18),
            # 编辑页蒙版深度。注意：**不能**写成 `d.get(...) or 118` —— 0 是
            # 合法值（完全不压暗），`or` 会把它吞成默认值。_coerce_int 只在
            # None / "" / 非数字时回落；范围随后 clamp 到 [0, 255]。
            editor_dim_alpha=max(0, min(255, _coerce_int(
                d.get("EditorDimAlpha", 118), 118))),
            freeze_show_windows_apps=bool(d.get("FreezeShowWindowsApps", False)),
            freeze_hidden=list(d.get("FreezeHidden", []) or []),
            freeze_prioritize_foreground=bool(d.get("FreezePrioritizeForeground", True)),
            freeze_minimize_on_unfreeze=bool(d.get("FreezeMinimizeOnUnfreeze", True)),
            # P0-1 fix: validate hex legality, not just length — a 6-char-but-
            # non-hex string (e.g. "gghhii") used to crash build_focus_style
            # on startup.  _safe_hex returns the default on any malformed input.
            focus_color=_safe_hex(d.get("FocusColor", "e6ff1e"), "e6ff1e"),
            # P0-2 fix: int(d["FocusOpacity"]) raised ValueError on "abc"/""/
            # True, which made from_dict fail and ConfigManager.load silently
            # fall back to AppConfig() — losing EVERY field.  _coerce_focus_opacity
            # only falls back for genuinely missing / None / empty / non-numeric
            # values, and clamps to [0, 100].  Legitimate 0 is preserved.
            focus_opacity=_coerce_focus_opacity(d.get("FocusOpacity"), 25),
            input_config=InputConfig.from_dict(d.get("Input", {})),
            window_align=_safe_align(d.get("WindowAlign", "right")),
            xinput_gate_enabled=bool(d.get("XInputGateEnabled", False)),
            xinput_gate_games=list(d.get("XInputGateGames", []) or []),
            card_bg_enabled=bool(d.get("CardBgEnabled", False)),
            card_bg_mode="image" if str(d.get("CardBgMode", "solid")).strip() == "image" else "solid",
            card_bg_color=_safe_hex(d.get("CardBgColor", "0e1117"), "0e1117"),
            card_bg_image=str(d.get("CardBgImage", "") or ""),
            card_fill_color=_safe_hex(d.get("CardFillColor", "141821"), "141821"),
            card_fill_alpha=_coerce_int(d.get("CardFillAlpha", 165) or 165, 165),
            # 主页面卡片排布（排版效果）。缺字段时回落默认 4 / 8。
            card_gap=max(0, _coerce_int(d.get("CardGap", 4) or 4, 4)),
            card_outer_margin=max(0, _coerce_int(d.get("CardOuterMargin", 8) or 8, 8)),
            # 页面转场（见上方 _page_transition 的白名单处理）。
            page_transition=_page_transition,
            transition_ms=max(0, min(600, _coerce_int(d.get("TransitionMs", 180), 180))),
            # 进入编辑器转场（见 editor_transition 字段说明）。模式白名单处理，
            # 时长 clamp 到 [0, 600]。
            editor_transition=_normalize_transition_mode(d.get("EditorTransition", "fade")),
            editor_transition_ms=max(0, min(600, _coerce_int(d.get("EditorTransitionMs", 180), 180))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ExtraTileBlocks": [b.to_dict() for b in self.extra_tile_blocks],
            "PowerConfig": self.power_config.to_dict(),
            "PresetsConfig": self.presets_config.to_dict(),
            "PowerPlanConfig": self.powerplan_config.to_dict(),
            "SmtConfig": self.smt_config.to_dict(),
            "FanConfig": self.fan_config.to_dict(),
            "TaskListConfig": self.tasklist_config.to_dict(),
            "LastSeenVersion": self.last_seen_version,
            "CardColors": self.card_colors,
            "Tiles": [t.to_dict() for t in self.tiles],
            "ProcMonConfigs": self.procmon_configs,
            "Theme": self.theme.to_dict(),
            "Cards": self.cards,
            "HiddenCards": self.hidden_cards,
            "PageBreaks": self.page_breaks,
            "ButtonStyle": self.button_style,
            "BreathPeriodS": self.breath_period_s,
            "BreathColor": self.breath_color,
            "BreathMaxBrightness": self.breath_max_brightness,
            "BreathFloor": self.breath_floor,
            "BreathFlashMs": self.breath_flash_ms,
            "BreathHoldMs": self.breath_hold_ms,
            "BreathScrimMs": self.breath_scrim_ms,
            "BreathEnabled": self.breath_enabled,
            "TrayColor": self.tray_color,
            "TrayOpacity": self.tray_opacity,
            "TrayBorder": self.tray_border,
            "TrayBorderWidth": max(0, min(4, int(getattr(self, "tray_border_width", 2) or 0))),
            "PageBgColor": str(getattr(self, "page_bg_color", "") or ""),
            "TrayBlur": self.tray_blur,
            "EditorDimAlpha": max(0, min(255, int(self.editor_dim_alpha))),
            "FreezeShowWindowsApps": self.freeze_show_windows_apps,
            "FreezeHidden": list(self.freeze_hidden),
            "FreezePrioritizeForeground": self.freeze_prioritize_foreground,
            "FreezeMinimizeOnUnfreeze": self.freeze_minimize_on_unfreeze,
            "FocusColor": self.focus_color,
            "FocusOpacity": self.focus_opacity,
            "WindowAlign": self.window_align,
            "XInputGateEnabled": self.xinput_gate_enabled,
            "XInputGateGames": list(self.xinput_gate_games),
            "CardBgEnabled": self.card_bg_enabled,
            "CardBgMode": self.card_bg_mode,
            "CardBgColor": self.card_bg_color,
            "CardBgImage": self.card_bg_image,
            "CardFillColor": self.card_fill_color,
            "CardFillAlpha": self.card_fill_alpha,
            "CardGap": max(0, int(self.card_gap)),
            "CardOuterMargin": max(0, int(self.card_outer_margin)),
            "PageTransition": _normalize_transition_mode(self.page_transition),
            "TransitionMs": max(0, min(600, int(getattr(self, "transition_ms", 180)))),
            "EditorTransition": _normalize_transition_mode(self.editor_transition),
            "EditorTransitionMs": max(0, min(600, int(getattr(self, "editor_transition_ms", 180)))),
            "Input": self.input_config.to_dict(),
        }

    def card_color(self, block_type: str, default: Color = BG_CARD) -> Color:
        return _rgb_csv_to_color(self.card_colors.get(block_type), default)


def _read_breath_period(d: dict[str, Any]) -> float:
    """Resolve the breathing period, migrating a legacy ``BreathSpeed`` value
    (10=fast .. 100=slow) to seconds when the new ``BreathPeriodS`` key is
    absent."""
    if "BreathPeriodS" in d and d["BreathPeriodS"] not in (None, ""):
        try:
            return float(d["BreathPeriodS"])
        except (TypeError, ValueError):
            pass
    if "BreathSpeed" in d and d["BreathSpeed"] not in (None, ""):
        try:
            return breath_speed_to_period(int(d["BreathSpeed"]))
        except (TypeError, ValueError):
            pass
    return 2.36


def _read_breath_floor(d: dict[str, Any]) -> float | None:
    """Resolve the breathing trough.

    ``None`` means "use ``core.timers._BREATH_FLOOR``" — that is both the
    default for a fresh/legacy ``layout.json`` (no ``BreathFloor`` key) and the
    fallback for an unparsable value, so upgrading never changes the look.
    """
    raw = d.get("BreathFloor", None)
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v < 0.0 or v > 1.0:
        return None
    return v


class ConfigManager:
    """Loads/saves ``layout.json`` and exposes computed view state."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self.config = AppConfig()
        # Report §6.2-5 / §5.1: keep the raw text on parse failure so the user
        # can recover it instead of losing the data silently.
        self.last_error_raw: str | None = None
        self.last_error: str | None = None

    def load(self, path: str | None = None) -> AppConfig:
        path = path or self.path
        if path and os.path.exists(path):
            raw: str | None = None
            try:
                with open(path, "r", encoding="utf-8-sig") as fh:
                    raw = fh.read()
                data = json.loads(raw)
                self.config = AppConfig.from_dict(data)
                self.last_error_raw = None
                self.last_error = None
            except Exception as exc:  # never silently drop the user's data
                self.last_error_raw = raw
                self.last_error = str(exc)
                print(f"[ConfigManager] 读取布局失败，保留原始内容以便恢复: {exc}")
                if not getattr(self, "config", None):
                    self.config = AppConfig()
        else:
            self.config = AppConfig()
        if path:
            self.path = path
        return self.config

    def save(self, path: str | None = None) -> None:
        path = path or self.path
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.config.to_dict(), fh, ensure_ascii=False, indent=2)
        except Exception as exc:  # never silently drop the user's data
            # 写入失败绝不能静默吞掉：否则用户以为配置已保存，实则 layout.json
            # 未更新，下次启动又恢复旧值。记录失败原因供上层/UI 读取，并打到 stderr。
            self.last_error_raw = f"save:{path}"
            self.last_error = str(exc)
            print(f"[config] 保存失败 {path}: {exc}", file=sys.stderr)

    # -- page/zone view derived from Cards + PageBreaks ----------------
    def pages(self) -> list[list[str]]:
        cards = self.config.cards
        if not cards:
            return []
        breaks = sorted(set(int(x) for x in self.config.page_breaks)) or [0]
        pages: list[list[str]] = []
        for i, b in enumerate(breaks):
            end = breaks[i + 1] if i + 1 < len(breaks) else len(cards)
            pages.append(cards[b:end])
        return pages