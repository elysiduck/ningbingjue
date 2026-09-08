# -*- coding: utf-8 -*-
"""
core/thermostat.py — Thermostat（恒温）模式控制律（纯逻辑，无 UI / 无 PyQt 依赖）

被三处复用，保证「同一份控制律真相」：
  - tools/fan_thermostat_profiler.py   （--preview 只读仿真）
  - ui/thermostat_test_window.py        （GUI 测量窗口的实控 / 预览）
  - 未来的 fan.py MODE_THERMOSTAT        （真实模式）

控制哲学：单边寻底，求「刚好压住目标温度的最低转速」。
  - 温度 > 目标（偏热）  → 升转速
  - 温度 < 目标（偏冷）  → 降转速
  - 死区内（|误差| ≤ deadband）→ 不动（抗传感器噪声 / 热滞后抖动）
  - 单步 PWM 变化上限 = step_limit（rate-limit，防过冲振荡）
  - 硬顶 hard_ceil → 强制满转（兜底，防控制器 bug 煮机）
  - 温度信号掉线（None）→ 由调用方累计轮数后交还 BIOS（本类只回报 sensor_lost）

「目标不可达」判定（unreach_*）必须给热惯性时间，不能转速一顶到 255 就立刻
交还 BIOS：满转后封装温度还要好几秒才掉下来。因此转速顶到极限后进入「等待收敛」
观察窗——保持极限转速、累计 pin 轮数；只有在观察窗满 **且** 温度已停止向目标
改善（plateau）时才判不可达；若温度仍在向目标移动（哪怕慢），继续等到硬上限。

注意：温度源默认 ACPI 皮肤温度，比 CPU 结温（RTSS 看到的温度）低 ~10-20℃，
目标数字钉的是皮肤温度。要钉结温需改用 ec / hwinfo 源。
"""
from __future__ import annotations

PWM_MIN = 0
PWM_MAX = 255

DEFAULT_K = 2.0          # PWM per ℃；最终由扫频斜率反推覆盖
DEADBAND_C = 2.0         # 死区（℃），带内不动
STEP_LIMIT = 20          # 单步 PWM 变化上限 = rate-limit
HARD_CEIL_C = 85.0       # 硬顶（℃），超过即满转兜底
DROP_ROUNDS = 3          # 连续掉线轮数 → 交还 BIOS（由调用方使用）
# 「等待收敛」观察窗（单位 = 控制循环轮数；循环周期 3s → 8 轮 ≈ 24s）
#   grace_rounds    : 顶到极限后至少等这么多轮，期间一律回报 pinned_*（不判不可达）
#   max_grace_rounds: 即便温度仍在缓慢改善，超过此轮数也强制判不可达（防无限等）
#   improve_eps     : 观察窗内「起始→当前」的整体改善量（℃）阈值；≤ 此值视为 plateau
GRACE_ROUNDS = 8
MAX_GRACE_ROUNDS = 25
IMPROVE_EPS = 0.2


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class ThermostatController:
    """恒温控制律：给定当前温度与目标，算出下一拍该写的 PWM。"""

    def __init__(self, target_c, k=DEFAULT_K, deadband=DEADBAND_C,
                 step_limit=STEP_LIMIT, hard_ceil=HARD_CEIL_C,
                 grace_rounds=GRACE_ROUNDS, max_grace_rounds=MAX_GRACE_ROUNDS,
                 improve_eps=IMPROVE_EPS):
        self.target_c = float(target_c)
        self.k = float(k)
        self.deadband = float(deadband)
        self.step_limit = int(step_limit)
        self.hard_ceil = float(hard_ceil)
        self.grace_rounds = int(grace_rounds)
        self.max_grace_rounds = int(max_grace_rounds)
        self.improve_eps = float(improve_eps)
        self._reset_pin()

    def _reset_pin(self) -> None:
        """清空「等待收敛」观察窗状态（温度进入死区/硬顶/掉线/正常可调时调用）。"""
        self._pin_rounds = 0
        self._pin_start_temp = None
        self._pin_best = None

    def step(self, temp_c, current_pwm):
        """推进一步，返回 ``(new_pwm, delta, status)``。

        status ∈ {hold, up, down, ceiling, sensor_lost,
                  pinned_low, pinned_high, unreach_low, unreach_high}
          - hold         : 死区内，不动
          - up / down    : 升 / 降转速（已限幅到 step_limit）
          - ceiling      : 达到硬顶，已强制满转（255）
          - sensor_lost  : temp_c 为 None；调用方应累计轮数后交还 BIOS
          - pinned_high  : 已满转(255)但仍偏高 → 降温中，等待收敛（**未判不可达**）
          - pinned_low   : 已停转(0)但仍偏低 → 升温中，等待收敛（**未判不可达**）
          - unreach_high : 满转且观察窗满、温度已 plateau 仍高于目标 → 目标不可达
          - unreach_low  : 停转且观察窗满、温度已 plateau 仍低于目标 → 目标不可达

        注意：pinned_* 阶段风扇保持在极限转速（255 / 0）不动，调用方应 keep 原值；
        只有 unreach_* 才意味着「放弃，交还 BIOS / 维持极限」。
        """
        current_pwm = clamp(int(current_pwm), PWM_MIN, PWM_MAX)
        if temp_c is None:
            self._reset_pin()
            return current_pwm, 0, "sensor_lost"
        if temp_c >= self.hard_ceil:
            self._reset_pin()
            return PWM_MAX, PWM_MAX - current_pwm, "ceiling"

        err = self.target_c - temp_c          # >0 偏冷 / <0 偏热
        if abs(err) <= self.deadband:
            self._reset_pin()
            return current_pwm, 0, "hold"

        # 偏热(temp > target) → 想要更高 PWM → delta>0
        delta = round(self.k * (temp_c - self.target_c))
        delta = clamp(delta, -self.step_limit, self.step_limit)
        # 出了死区至少要动 1 步，避免极小增益时卡死
        if delta == 0 and abs(err) > self.deadband:
            delta = 1 if (temp_c - self.target_c) > 0 else -1

        new = clamp(current_pwm + delta, PWM_MIN, PWM_MAX)
        if new != current_pwm:
            # 还能继续调，正常升/降；清掉 pin 计时
            self._reset_pin()
            return new, delta, ("up" if delta > 0 else "down")

        # ---- 已顶到转速极限，仍偏离目标 → 进入「等待收敛」观察窗 ----
        # 不再立刻判不可达：满转后温度还要好几秒才掉下来，必须给热惯性时间。
        high = (temp_c > self.target_c)       # 想升转速却已 255（偏热）
        self._register_pin(temp_c)
        return self._evaluate_pin(temp_c, current_pwm, high)

    def _register_pin(self, temp_c) -> None:
        if self._pin_rounds == 0:
            self._pin_rounds = 1
            self._pin_start_temp = temp_c
            self._pin_best = temp_c
        else:
            self._pin_rounds += 1
            if self._pin_best is None:
                self._pin_best = temp_c
            elif temp_c > self.target_c:
                self._pin_best = min(self._pin_best, temp_c)   # 偏热：越冷越好
            else:
                self._pin_best = max(self._pin_best, temp_c)   # 偏冷：越热越好

    def _evaluate_pin(self, temp_c, current_pwm, high):
        if self._pin_rounds < self.grace_rounds:
            # 观察窗内：保持极限转速，回报「等待收敛」
            return current_pwm, 0, ("pinned_high" if high else "pinned_low")

        # 观察窗已满：看温度是否真的在向目标收敛
        # 整体改善量（起始 → 当前）：偏热=降温量；偏冷=升温量
        if self._pin_start_temp is not None:
            overall = (self._pin_start_temp - temp_c) if high else (temp_c - self._pin_start_temp)
        else:
            overall = 0.0

        if overall <= self.improve_eps:
            # 温度已停止向目标改善（plateau）→ 确实不可达
            return current_pwm, 0, ("unreach_high" if high else "unreach_low")

        # 仍在向目标移动（哪怕慢）：继续等，直到 max_grace 硬上限
        if self._pin_rounds >= self.max_grace_rounds:
            return current_pwm, 0, ("unreach_high" if high else "unreach_low")
        return current_pwm, 0, ("pinned_high" if high else "pinned_low")
