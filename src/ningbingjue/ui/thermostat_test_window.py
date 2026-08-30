# -*- coding: utf-8 -*-
"""
ui/thermostat_test_window.py — Thermostat 目标温度「测量 / 测试」窗口

独立于风扇控制编辑项（不在设置面板里堆测试控件）：在编辑项里点一下按钮，
弹出一个**独立顶层窗口**，用活的 FanBlock 直接驱动风扇，把「这台机器」量清楚：

  - 皮肤温度(ACPI) 与 结温(EC/HWiNFO) 的差值  → 决定滑块该设多少
  - 不同 PWM 对应的稳态温度（扫频）            → 决定曲线形状
  - 实控钉温：温度高了风扇就一直猛吹，直到稳定在目标（你要的效果）

四种模式：
  free    只读观察（不碰风扇）
  sweep   逐档写转速，量出「转速→稳态温度」曲线
  preview 只读仿真未来控制律（不写风扇），看它会怎么调
  live    实控：直接写 PWM 把温度钉在目标上（你要测的核心）

安全闸（与已确认决策一致）：
  - 硬顶 85℃ → 强制满转
  - 温度掉线连续 3 轮 → 交还 BIOS 自动控制并停止
  - 目标不可达：低温→停转(RPM=1) / 高温→交还 BIOS
  - 关闭窗口自动交还 BIOS；绝不长期占着 EC 把风扇钉死

该窗口**复用** core.thermostat.ThermostatController —— 与 CLI 测量器、未来
MODE_THERMOSTAT 是同一份控制律真相。
"""
from __future__ import annotations

import csv
import os
import time
from typing import Any, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QPaintEvent, QPainter, QPen
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSlider, QTextEdit, QVBoxLayout, QWidget,
)

from .panels import _NoWheelComboBox
from ..core.thermostat import (
    ThermostatController, PWM_MAX, DEFAULT_K, DEADBAND_C,
    STEP_LIMIT, HARD_CEIL_C, DROP_ROUNDS,
)

_HERE = os.path.dirname(os.path.abspath(__file__))          # .../src/ningbingjue/ui
_LOGS = os.path.normpath(os.path.join(_HERE, "..", "..", "logs"))

# 控制源显示名 → block 上的读数方法
_TEMP_SOURCES = [
    ("皮肤温度 (ACPI，默认)", "acpi"),
    ("结温 (EC 寄存器)", "ec"),
    ("结温 (HWiNFO 共享内存)", "hwinfo"),
]

_STATUS_CN = {
    "hold": "保持（死区内不动）",
    "up": "偏热 → 升转速",
    "down": "偏冷 → 降转速",
    "ceiling": "⛔ 硬顶！强制满转",
    "sensor_lost": "⚠ 温度掉线",
    "unreach_low": "已停转（仍低于目标，安全）",
    "unreach_high": "⚠ 目标不可达（已满转仍偏高）→ 交还 BIOS",
    "pinned_high": "已满转，降温中…等待收敛（未判不可达）",
    "pinned_low": "已停转，升温中…等待收敛（未判不可达）",
}


class _StripChart(QWidget):
    """轻量实时条带图（QPainter 手绘，不依赖 matplotlib）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.samples: list[dict] = []
        self.target: Optional[float] = None
        self.temp_lo, self.temp_hi = 30.0, 95.0
        self.setStyleSheet("background:#11151f; border-radius:6px;")

    def push(self, temp: Optional[float], pwm: int, target: Optional[float]) -> None:
        self.samples.append({"t": temp, "p": pwm})
        if len(self.samples) > 240:
            self.samples.pop(0)
        self.target = target
        self.update()

    def clear(self) -> None:
        self.samples.clear()
        self.update()

    def _y(self, v: float, lo: float, hi: float, h: int) -> float:
        return h - (v - lo) / (hi - lo) * h

    def paintEvent(self, ev: QPaintEvent) -> None:  # noqa: N802
        super().paintEvent(ev)
        w = self.width()
        h = self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 网格
        pen = QPen(QColor(255, 255, 255, 28))
        p.setPen(pen)
        for g in range(0, 101, 25):
            y = self._y(g, 0, 100, h)
            p.drawLine(0, int(y), w, int(y))
        if not self.samples:
            p.setPen(QColor(150, 160, 175))
            p.drawText(8, 20, "（运行后显示 温度/转速 曲线）")
            return
        n = len(self.samples)
        # 目标温度横线
        if self.target is not None:
            ty = self._y(self.target, self.temp_lo, self.temp_hi, h)
            pg = QPen(QColor(120, 230, 160, 200))
            pg.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pg)
            p.drawLine(0, int(ty), w, int(ty))
        # 温度曲线（橙）：映射到 [temp_lo, temp_hi]
        pt = QPen(QColor(255, 170, 90))
        pt.setWidth(2)
        p.setPen(pt)
        for i, s in enumerate(self.samples):
            t = s["t"]
            if t is None:
                continue
            x = w * i / max(1, n - 1)
            y = self._y(min(self.temp_hi, max(self.temp_lo, t)),
                        self.temp_lo, self.temp_hi, h)
            if i == 0:
                p.moveTo(int(x), int(y))
            else:
                p.lineTo(int(x), int(y))
        # 转速曲线（青）：映射到 [0,255]
        pp = QPen(QColor(110, 210, 255))
        pp.setWidth(2)
        p.setPen(pp)
        for i, s in enumerate(self.samples):
            x = w * i / max(1, n - 1)
            y = self._y(s["p"], 0, PWM_MAX, h)
            if i == 0:
                p.moveTo(int(x), int(y))
            else:
                p.lineTo(int(x), int(y))
        p.end()


class ThermostatTestWindow(QWidget):
    def __init__(self, block: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.block = block
        self.setWindowFlags(Qt.WindowType.Window)
        self.setWindowTitle("Thermostat 目标温度测试")
        self.resize(560, 620)

        # --- 运行状态 ---
        self._running = False
        self._mode = "live"
        self._target = 65.0
        self._pwm = int(getattr(block, "last_pwm", 0) or 0)
        self._lost = 0
        self._sweep_levels: list[int] = []
        self._sweep_idx = 0
        self._sweep_dwell = 45.0
        self._sweep_elapsed = 0.0
        self._orig_mode: Optional[str] = None
        self._csv = None
        self._csv_w = None
        self._csv_path: Optional[str] = None
        self._start_ts = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(3000)          # 3s / tick（与已确认周期一致）
        self._timer.timeout.connect(self._tick)

        self._build_ui()

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        warn = QLabel(
            "⚠ 本窗口会直接驱动风扇。关闭窗口会自动交还 BIOS 自动控制（AUTO），"
            "不会把风扇钉死。\n目标默认钉【皮肤温度】；想看皮肤 vs 结温差多少，"
            "结温列会一并显示。")
        warn.setWordWrap(True)
        warn.setStyleSheet(
            "color:#ffd28a; font-size:11px; background:rgba(255,170,90,0.10);"
            " border-radius:6px; padding:6px 8px;")
        root.addWidget(warn)

        # 模式
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("模式"))
        self._mode_combo = _NoWheelComboBox()
        self._mode_combo.addItem("实控钉温 (live)", "live")
        self._mode_combo.addItem("只读仿真 (preview)", "preview")
        self._mode_combo.addItem("扫频 (sweep)", "sweep")
        self._mode_combo.addItem("只读观察 (free)", "free")
        self._mode_combo.setMinimumHeight(32)
        self._mode_combo.setStyleSheet(
            "QComboBox{background:rgba(255,255,255,0.08); color:#dfe6f0;"
            " border-radius:4px; padding:2px 8px; font-size:12px;}"
            "QComboBox::drop-down{border:none; width:18px;}"
            "QComboBox QAbstractItemView{background:#1e2230; color:#dfe6f0;"
            " selection-background-color:#62e0a8;}")
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._mode_combo, 1)
        root.addLayout(mode_row)

        # 控制温度源
        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("控制源"))
        self._src_combo = _NoWheelComboBox()
        for label, key in _TEMP_SOURCES:
            self._src_combo.addItem(label, key)
        self._src_combo.setMinimumHeight(32)
        self._src_combo.setStyleSheet(self._mode_combo.styleSheet())
        src_row.addWidget(self._src_combo, 1)
        root.addLayout(src_row)

        # 目标温度滑块 40-85
        tgt_row = QHBoxLayout()
        self._tgt_slider = QSlider(Qt.Orientation.Horizontal)
        self._tgt_slider.setRange(40, 85)
        self._tgt_slider.setValue(int(self._target))
        self._tgt_slider.setSingleStep(1)
        self._tgt_slider.valueChanged.connect(
            lambda v: self._tgt_lbl.setText(f"{v}℃"))
        tgt_row.addWidget(QLabel("目标温度"))
        tgt_row.addWidget(self._tgt_slider, 1)
        self._tgt_lbl = QLabel(f"{int(self._target)}℃")
        self._tgt_lbl.setStyleSheet("color:#ffd28a; font-weight:bold; min-width:48px;")
        tgt_row.addWidget(self._tgt_lbl)
        root.addLayout(tgt_row)

        # 扫频参数（仅 sweep 用）
        sweep_row = QHBoxLayout()
        sweep_row.addWidget(QLabel("扫频档位"))
        self._levels_edit = QLineEdit("0,51,102,153,204,255")
        self._levels_edit.setStyleSheet(
            "QLineEdit{background:rgba(255,255,255,0.08); color:#dfe6f0;"
            " border-radius:4px; padding:3px 6px;}")
        sweep_row.addWidget(self._levels_edit, 2)
        sweep_row.addWidget(QLabel("每档秒"))
        self._dwell_edit = QLineEdit("45")
        self._dwell_edit.setFixedWidth(54)
        self._dwell_edit.setStyleSheet(self._levels_edit.styleSheet())
        sweep_row.addWidget(self._dwell_edit)
        root.addLayout(sweep_row)

        # 读数
        self._readout = QLabel("皮肤 — ℃  |  结温 — ℃  |  差值 — ℃\n"
                               "当前 PWM —  |  RPM —  |  状态 —")
        self._readout.setWordWrap(True)
        self._readout.setStyleSheet(
            "color:#cfe; font-size:12px; background:rgba(127,209,255,0.08);"
            " border-radius:6px; padding:8px 10px;")
        root.addWidget(self._readout)

        # 图表
        self._chart = _StripChart(self)
        root.addWidget(self._chart, 1)

        # 日志
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(110)
        self._log.setStyleSheet(
            "QTextEdit{background:#0e1119; color:#bcd; border-radius:6px;"
            " font-family:Consolas,Menlo,monospace; font-size:11px;}")
        root.addWidget(self._log)

        # 按钮
        btn_row = QHBoxLayout()
        self._run_btn = QPushButton("开始")
        self._run_btn.setMinimumHeight(38)
        self._run_btn.setStyleSheet(
            "QPushButton{background:#62e0a8; color:#102; border-radius:6px;"
            " font-weight:bold; font-size:13px;}"
            "QPushButton:disabled{background:#3a6; color:#9db;}")
        self._run_btn.clicked.connect(self._on_start)
        self._stop_btn = QPushButton("停止 / 交还 BIOS")
        self._stop_btn.setMinimumHeight(38)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(
            "QPushButton{background:#e08a6a; color:#201; border-radius:6px;"
            " font-weight:bold; font-size:13px;}"
            "QPushButton:disabled{background:#7a5; color:#9db;}")
        self._stop_btn.clicked.connect(lambda _=False: self._on_stop(True))
        btn_row.addWidget(self._run_btn)
        btn_row.addWidget(self._stop_btn)
        root.addLayout(btn_row)

        self._on_mode_changed()

    def _on_mode_changed(self) -> None:
        m = self._mode_combo.currentData() or "live"
        self._mode = m
        show_sweep = (m == "sweep")
        self._levels_edit.setEnabled(show_sweep)
        self._dwell_edit.setEnabled(show_sweep)
        self._tgt_slider.setEnabled(m in ("live", "preview"))

    # ---------------------------------------------------------------- 运行
    def _on_start(self) -> None:
        if self._running:
            return
        if not self._ec_ok():
            self._log_msg("✗ EC 不可用（未以管理员运行 / Core Isolation 未关）。")
            return
        self._target = float(self._tgt_slider.value())
        # 持久化控制器：观察窗状态必须跨 tick 累积，绝不能在每拍新建
        self._controller = ThermostatController(
            target_c=self._target, k=DEFAULT_K, deadband=DEADBAND_C,
            step_limit=STEP_LIMIT, hard_ceil=HARD_CEIL_C)
        self._lost = 0
        self._start_ts = time.time()
        self._pwm = int(getattr(self.block, "last_pwm", 0) or 0)
        # 静默后台曲线循环：把 block 切到非 curve 模式，sampler 早退不抢写
        self._orig_mode = getattr(self.block, "mode", "auto")
        try:
            self.block.mode = "manual"
        except Exception:  # noqa: BLE001
            pass

        if self._mode == "sweep":
            try:
                self._sweep_levels = [max(0, min(255, int(x)))
                                      for x in self._levels_edit.text().split(",") if x.strip() != ""]
                self._sweep_dwell = max(5.0, float(self._dwell_edit.text() or "45"))
            except Exception:  # noqa: BLE001
                self._sweep_levels = [0, 51, 102, 153, 204, 255]
                self._sweep_dwell = 45.0
            if not self._sweep_levels:
                self._sweep_levels = [0, 51, 102, 153, 204, 255]
            self._sweep_idx = 0
            self._sweep_elapsed = 0.0
            self._write_pwm(self._sweep_levels[0], "sweep")
            self._log_msg(f"[sweep] 档位={self._sweep_levels} 每档{self._sweep_dwell:.0f}s")
        elif self._mode == "live":
            self._log_msg(f"[live] 目标={self._target:.0f}℃ 硬顶={HARD_CEIL_C:.0f}℃ "
                          f"死区={DEADBAND_C:.0f}℃ 单步≤{STEP_LIMIT} K={DEFAULT_K}")
        elif self._mode == "preview":
            self._log_msg(f"[preview] 只读仿真 目标={self._target:.0f}℃（不写风扇）")
        else:
            self._log_msg("[free] 只读观察")

        self._ensure_csv()
        self._chart.clear()
        self._running = True
        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._timer.start()
        self._tick()   # 立即跑一拍，避免等 3s

    def _on_stop(self, restore: bool = True) -> None:
        if not self._running:
            return
        self._timer.stop()
        self._running = False
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        if self._csv:
            try:
                self._csv.close()
            except Exception:  # noqa: BLE001
                pass
            self._csv = None
        if restore and self.block is not None:
            self._restore_block()
        self._log_msg("■ 已停止。" + ("已交还 BIOS 自动控制。" if restore else ""))

    def _restore_block(self) -> None:
        try:
            if self._orig_mode == "auto":
                self.block._restore_auto()
            else:
                self.block.set_mode(self._orig_mode)
                self.block.apply(silent=True)
        except Exception:  # noqa: BLE001
            try:
                self.block._restore_auto()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 每拍
    def _tick(self) -> None:
        if not self._running:
            return
        ctrl_src = self._src_combo.currentData() or "acpi"
        skin = self._read_temp("acpi")
        junc = self._read_temp("ec") if ctrl_src != "ec" else skin
        if ctrl_src == "hwinfo" and junc is None:
            junc = self._read_temp("hwinfo")
        rpm = self.block.read_fan_rpm()

        # 掉线累计
        if skin is None:
            self._lost += 1
        else:
            self._lost = 0

        # 掉线 3 轮 → 交还 BIOS
        if self._lost >= DROP_ROUNDS:
            self._log_msg(f"⚠ 温度掉线 {self._lost} 轮 → 交还 BIOS 并停止")
            self._append_csv(None, None, "SENSOR_DROP")
            self._on_stop(True)
            return

        ctrl_temp = self._read_temp(ctrl_src)
        status = "hold"
        new_pwm = self._pwm
        delta = 0
        controller = getattr(self, "_controller", None)
        if controller is None:
            controller = self._controller = ThermostatController(
                target_c=self._target, k=DEFAULT_K, deadband=DEADBAND_C,
                step_limit=STEP_LIMIT, hard_ceil=HARD_CEIL_C)

        if self._mode == "free":
            self._log_msg(f"t={self._elapsed():.0f}s 皮肤={_fmt(skin)} 结温={_fmt(junc)} "
                          f"RPM={rpm if rpm is not None else '—'}")
        elif self._mode == "preview":
            new_pwm, delta, status = controller.step(ctrl_temp, self._pwm)
            self._log_msg(f"t={self._elapsed():.0f}s 控制温={_fmt(ctrl_temp)} "
                          f"→ 预测PWM={new_pwm}({delta:+d}) [{_STATUS_CN[status]}]")
        elif self._mode == "sweep":
            self._sweep_elapsed += self._timer.interval() / 1000.0
            if self._sweep_elapsed >= self._sweep_dwell:
                self._sweep_idx += 1
                self._sweep_elapsed = 0.0
                if self._sweep_idx >= len(self._sweep_levels):
                    self._log_msg("[sweep] 全部档位完成 → 停止并交还 BIOS")
                    self._append_csv(skin, junc, "SWEEP_DONE")
                    self._on_stop(True)
                    return
                self._write_pwm(self._sweep_levels[self._sweep_idx], "sweep")
            self._log_msg(f"t={self._elapsed():.0f}s 档[{self._sweep_idx}]="
                          f"{self._sweep_levels[self._sweep_idx]} 皮肤={_fmt(skin)} "
                          f"结温={_fmt(junc)} RPM={rpm if rpm is not None else '—'}")
            new_pwm = self._sweep_levels[self._sweep_idx]
        elif self._mode == "live":
            new_pwm, delta, status = controller.step(ctrl_temp, self._pwm)
            if status == "ceiling":
                self._write_pwm(PWM_MAX, "ceiling")
                self._log_msg(f"⛔ 硬顶 {HARD_CEIL_C:.0f}℃ → 强制满转")
            elif status == "unreach_high":
                self._append_csv(skin, junc, "UNREACH_HIGH")
                self._log_msg("⚠ 目标不可达（已满转仍偏高）→ 交还 BIOS")
                self._on_stop(True)
                return
            elif status == "unreach_low":
                self._write_pwm(0, "unreach_low")
                self._log_msg("已停转（仍低于目标，安全）")
            elif status in ("up", "down"):
                self._write_pwm(new_pwm, status)
            elif status in ("pinned_high", "pinned_low"):
                # 已满转/已停转，降温/升温中：保持极限转速不动，等待收敛观察窗
                pass
            else:  # hold
                pass
            self._pwm = int(getattr(self.block, "last_pwm", self._pwm) or self._pwm)
            self._log_msg(f"t={self._elapsed():.0f}s 控制温={_fmt(ctrl_temp)} "
                          f"PWM={self._pwm}({delta:+d}) RPM={rpm if rpm is not None else '—'} "
                          f"[{_STATUS_CN[status]}]")

        # 读数 + 图表 + CSV
        delta_txt = junc - skin if (skin is not None and junc is not None) else None
        self._readout.setText(
            f"皮肤(ACPI) {_fmt(skin)}℃   |   结温 {_fmt(junc)}℃   |   "
            f"差值 {(_fmt(delta_txt)+'℃') if delta_txt is not None else '—'}\n"
            f"当前 PWM {self._pwm}   |   RPM {rpm if rpm is not None else '—'}   |   "
            f"状态 {_STATUS_CN.get(status, status)}")
        self._chart.push(skin, self._pwm, self._target)
        self._append_csv(skin, junc, status)

    # ---------------------------------------------------------------- 辅助
    def _ec_ok(self) -> bool:
        try:
            return bool(self.block._ec_health())
        except Exception:  # noqa: BLE001
            return False

    def _read_temp(self, src: str) -> Optional[float]:
        try:
            if src == "acpi":
                return self.block._read_temp_acpi()
            if src == "ec":
                return self.block._read_temp_ec()
            if src == "hwinfo":
                return self.block._read_temp_hwinfo()
        except Exception:  # noqa: BLE001
            return None
        return None

    def _write_pwm(self, pwm: int, reason: str) -> bool:
        pwm = max(0, min(PWM_MAX, int(pwm)))
        try:
            ok = self.block._write_fan_pwm(pwm, reason=f"thermostat_{reason}")
        except Exception as e:  # noqa: BLE001
            self._log_msg(f"✗ 写 PWM={pwm} 失败：{e}")
            return False
        self._pwm = pwm
        return ok

    def _elapsed(self) -> float:
        return time.time() - self._start_ts

    def _ensure_csv(self) -> None:
        try:
            os.makedirs(_LOGS, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            self._csv_path = os.path.join(_LOGS, f"fan_thermostat_profile_{ts}.csv")
            self._csv = open(self._csv_path, "w", newline="", encoding="utf-8-sig")
            self._csv_w = csv.writer(self._csv)
            self._csv_w.writerow([
                "elapsed_s", "mode", "control_source", "target_c",
                "skin_temp_c", "junction_temp_c", "delta_c",
                "pwm_cmd", "rpm", "status"])
            self._csv.flush()
        except Exception as e:  # noqa: BLE001
            self._csv = None
            self._log_msg(f"（CSV 日志不可用，仅界面显示：{e}）")

    def _append_csv(self, skin, junc, status) -> None:
        if self._csv is None or self._csv_w is None:
            return
        try:
            delta = (junc - skin) if (skin is not None and junc is not None) else ""
            self._csv_w.writerow([
                f"{self._elapsed():.1f}",
                self._mode,
                self._src_combo.currentData() or "acpi",
                f"{self._target:.1f}",
                f"{skin:.2f}" if skin is not None else "",
                f"{junc:.2f}" if junc is not None else "",
                f"{delta:.2f}" if delta != "" else "",
                self._pwm,
                (self.block.read_fan_rpm() or ""),
                status])
            self._csv.flush()
        except Exception:  # noqa: BLE001
            pass

    def _log_msg(self, msg: str) -> None:
        self._log.append(msg)
        # 限长，避免内存无限增长
        if self._log.document().blockCount() > 400:
            self._log.clear()

    def closeEvent(self, ev) -> None:  # noqa: N802
        self._on_stop(True)
        super().closeEvent(ev)


def _fmt(v: Optional[float]) -> str:
    return f"{v:.1f}" if isinstance(v, (int, float)) else "—"