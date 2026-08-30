"""Configuration management — ``layout.json`` load/save + dataclasses.

Mirrors the config dict the original script keeps in memory and persists to
``layout.json``.  All fields are optional with sensible defaults so partial
configs still load.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any

from .color.palette import BG_APP, BG_CARD, FG_PRIMARY
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
            width=int(d.get("Width", 1)),
            tile_name_color=d.get("TileNameColor", "255,255,255"),
            icon_path=d.get("IconPath", ""),
            row=int(d.get("Row", 0)),
            name=d.get("Name", "新磁贴"),
            height=int(d.get("Height", 1)),
            block_name=d.get("BlockName", ""),
            show_block_title=bool(d.get("ShowBlockTitle", True)),
            col=int(d.get("Col", 0)),
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
            gpu=int(d.get("Gpu", 700)),
            tdp=int(d.get("Tdp", 10)),
            tdp_lock=bool(d.get("TdpLock", False)),
            gpu_float_enabled=bool(d.get("GpuFloatEnabled", False)),
            gpu_lock=bool(d.get("GpuLock", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "Gpu": self.gpu, "Tdp": self.tdp, "TdpLock": self.tdp_lock,
            "GpuFloatEnabled": self.gpu_float_enabled, "GpuLock": self.gpu_lock,
        }


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
            saturated_thr=float(d.get("SaturatedThr", 80.0)),
            min_standout=float(d.get("MinStandout", 30.0)),
            k_sigma=float(d.get("KSigmal", 2.0)),
            active_floor=float(d.get("ActiveFloor", 5.0)),
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
            decor_seed=int(d.get("DecorSeed", 0)),
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
    # -- card tray background (customizable in 呼吸灯自定义) ------------
    # Tray fill colour as "rrggbb".  Default matches the original dark panel.
    tray_color: str = "0e1117"
    # Tray fill opacity, 0..100 (percent).  Default 86 ≈ rgba(...,0.86).
    tray_opacity: int = 86
    # Whether the tray shows its 1px light border.
    tray_border: bool = True
    # Gaussian blur radius (px) of the frosted desktop backdrop behind the card
    # area. 0 = sharp (no blur).  Customizable in 呼吸灯自定义.
    tray_blur: int = 18
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
        return cls(
            extra_tile_blocks=[ExtraTileBlock.from_dict(b) for b in d.get("ExtraTileBlocks", [])],
            power_config=PowerConfig.from_dict(d.get("PowerConfig", {})),
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
            tray_color=str(d.get("TrayColor", "0e1117")).strip() or "0e1117",
            tray_opacity=_coerce_int(d.get("TrayOpacity", 86) or 86, 86),
            tray_border=bool(d.get("TrayBorder", True)),
            tray_blur=_coerce_int(d.get("TrayBlur", 18) or 18, 18),
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
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ExtraTileBlocks": [b.to_dict() for b in self.extra_tile_blocks],
            "PowerConfig": self.power_config.to_dict(),
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
            "TrayColor": self.tray_color,
            "TrayOpacity": self.tray_opacity,
            "TrayBorder": self.tray_border,
            "TrayBlur": self.tray_blur,
            "FreezeShowWindowsApps": self.freeze_show_windows_apps,
            "FreezeHidden": list(self.freeze_hidden),
            "FreezePrioritizeForeground": self.freeze_prioritize_foreground,
            "FreezeMinimizeOnUnfreeze": self.freeze_minimize_on_unfreeze,
            "FocusColor": self.focus_color,
            "FocusOpacity": self.focus_opacity,
            "WindowAlign": self.window_align,
            "XInputGateEnabled": self.xinput_gate_enabled,
            "XInputGateGames": list(self.xinput_gate_games),
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
        except Exception:
            pass

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
