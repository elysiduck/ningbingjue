"""Hysteresis test for FanBlock curve evaluation.

Verifies the core MA FanControl design: when temperature oscillates near a
threshold by less than hysteresis_c, the PWM band must NOT flip-flop.

Scenario:
  Curve: [{40,0}, {50,100}, {60,200}, {70,255}]
  Hysteresis: 3°C

  - At 49℃ → band 0 (PWM 0)
  - At 51℃ → band 1 (PWM 100)  (crossed threshold UP)
  - At 49℃ (oscillating back) → MUST STAY at band 1 (PWM 100), NOT drop
                                 to 0, because |51 - 49| = 2 < hysteresis 3
  - At 47℃ → drops to band 0 (PWM 0)  (47 < 50 - 3)
  - At 53℃ → band 1 (PWM 100)
  - At 57℃ → band 1 still (within 3°C of 50... no wait, 57 ≥ 50 + 3? = 53 → crosses to band 2)

Let me trace this carefully:
  - 49℃ → up_band=0 (49 >= 40 but < 50) → target=0, _last_band_idx=0
  - 51℃ → up_band=1 (51 >= 50) → target=100
       - last_temp=40, 51 >= 40+3 → up_band(1) > last(0) → band=1, target=100
  - 49℃ → up_band=0 → target=0 (initial)
       - last_temp=50, 49 < 50-3=47? No → not cooling path
       - last_band_idx(1) < len(4)-1, 49 >= 50+3=53? No → not heating path
       - stay in band 1 → target_pwm stays at last target (need to verify
         the code: target_pwm is reset to _pwm_for_band(up_band) every call)
       - Actually the code does: target_pwm = _pwm_for_band(pts, up_band)
         → at 49℃, up_band=0 → target_pwm=0
       - Then the hysteresis block only updates target_pwm when crossing
         a hysteresis boundary; in the "stay" case, target_pwm is NOT
         updated, so it stays at the value from the previous call.
       - BUT the previous call set target_pwm = 100, and this call did
         target_pwm = _pwm_for_band(pts, 0) = 0, then the hysteresis
         block did NOT update target_pwm (because we're in the "stay"
         branch), so target_pwm = 0 is returned.
       - BUG: the "stay" branch doesn't preserve last target_pwm.

Hmm, looking at the code more carefully:
  target_pwm = _pwm_for_band(pts, up_band)   # initial = 0
  if self._last_band_idx >= 0:
      ...
      elif (crosses_up):
          target_pwm = _pwm_for_band(pts, up_band)
      # else: stay → target_pwm NOT updated
  return _clamp_pct(target_pwm)

So when in "stay" branch, target_pwm = _pwm_for_band(pts, up_band) = 0
which is wrong — it should be the last band's PWM.

Need to fix: in "stay" branch, target_pwm should be set to last band's PWM,
not the up_band's PWM.

This test verifies the bug fix.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

import types
if sys.platform != "win32":
    ec_stub = types.ModuleType("ningbingjue.platform.ec_io")
    ec_stub.health_check = lambda: False
    ec_stub.available = lambda: False
    ec_stub.write_ec = lambda addr, value: False
    ec_stub.read_ec = lambda addr: None
    ec_stub.shutdown = lambda: None
    ec_stub._is_admin = lambda: True
    sys.modules["ningbingjue.platform.ec_io"] = ec_stub


class _MockEC:
    def health_check(self): return True
    def write_ec(self, addr, value): return True
    def read_ec(self, addr): return 0
    def shutdown(self): pass
    def _is_admin(self): return True


class _MockThermal:
    def __init__(self, t=65.0): self._t = t
    is_running = True
    def ensure_started(self): return True
    def read_temp_c(self): return self._t
    def dispose(self): pass


def test_hysteresis_no_flipflop():
    from ningbingjue.blocks.fan import FanBlock
    from ningbingjue.core.config import AppConfig, FanConfig
    import ningbingjue.core.thermal as thermal_mod

    fc = FanConfig(mode="curve",
                    curve=[{"temp": 40, "pwm": 0},
                           {"temp": 50, "pwm": 100},
                           {"temp": 60, "pwm": 200},
                           {"temp": 70, "pwm": 255}],
                    hysteresis_c=3)
    cfg = AppConfig()
    cfg.fan_config = fc

    orig = thermal_mod.ThermalProbe._instance
    thermal_mod.ThermalProbe._instance = _MockThermal(40.0)
    try:
        blk = FanBlock(cfg, ec_writer=_MockEC())

        # Trace: temperature sequence
        # Curve pts:  {40:0}, {50:100}, {60:200}, {70:255}, hysteresis=3
        # Band 0 = [40, 50), Band 1 = [50, 60), Band 2 = [60, 70), Band 3 = [70, ∞)
        # Cooling path triggers when temp_c < last_temp - 3
        # Heating path triggers when temp_c > last_temp + 3
        #
        # 49 → up_band=0 → band 0 → PWM 0
        # 51 → up_band=1, last_temp=40, 51>40+3=43 → heating up to band 1 → 100
        # 49 → up_band=0, last_temp=50, 49<50-3=47? NO → not cooling
        #       49>50+3=53? NO → not heating → stay band 1 → 100  ✓ hysteresis
        # 46 → up_band=0, last_temp=50, 46<50-3=47? YES → cooling path, down=0
        #       down_band(0) < last(1) → band=0 → 0
        # 53 → up_band=1, last_temp=40 (band 0), 53>40+3=43 → heating
        #       up_band(1)>last(0) → band=1 → 100
        # 57 → up_band=1 (57<60), last_temp=50, 57<47? NO → not cooling
        #       last_band(1)<3, 57>50+3=53? YES → heating path, up(1) > last(1)? NO
        #       → stay band 1 → 100
        # 58 → up_band=1 (58<60), similar logic → stay band 1 → 100
        # 65 → up_band=2 (65>=60), last_temp=50, 65<47? NO → not cooling
        #       last_band(1)<3, 65>50+3=53? YES → heating, up(2)>last(1) YES → band=2 → 200
        sequence = [
            (49, 0),    # band 0 → PWM 0
            (51, 100),  # crossed up to band 1
            (49, 100),  # oscillating back, must stay band 1 (hysteresis)
            (46, 0),    # dropped below 50-3=47 → cooling to band 0
            (53, 100),  # rose above 40+3=43 → heating to band 1
            (57, 100),  # within 3°C of 50 (47..53) → stays band 1
            (58, 100),  # within 3°C of 50 → stays band 1
            (65, 200),  # crossed 50+3 → heating to band 2
        ]
        for temp, expected_pwm in sequence:
            actual = blk._eval_curve(temp)
            assert actual == expected_pwm, (
                f"temp={temp}℃ expected PWM={expected_pwm}, got {actual}"
                f" (band_idx={blk._last_band_idx})")
        print("[PASS] Hysteresis no flip-flop")
    finally:
        thermal_mod.ThermalProbe._instance = orig


if __name__ == "__main__":
    test_hysteresis_no_flipflop()
    print("=== hysteresis OK ===")
