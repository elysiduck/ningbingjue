# -*- coding: utf-8 -*-
"""
fan_thermostat_profiler.py — 「Thermostat 恒温模式」plant 测量器（独立于 UI）

目的：在写任何控制器代码之前，先把「这台机器的风扇」这个被控对象量清楚：
  - 不同 PWM 转速对应的稳态皮肤温度（风扇曲线）
  - 温度对转速变化的响应速度（热时间常数，决定是否会振荡）
  - ACPI 皮肤温度 与 EC/结温（RTSS 看到的温度）的差值（决定滑块该设多少）

完全是测量 + 日志，**不会**把控制逻辑写进 fan 编辑页，也不会自动调风扇
除了你显式要求的 --hold / --sweep（那是为了激发响应才主动写转速）。

用法（需在掌机/目标机上以管理员运行，且关闭 Windows Core Isolation）：
  # 1) 自由观察 60s（只读数不写风扇）
  python fan_thermostat_profiler.py --free --seconds 60

  # 2) 扫频：0/51/102/153/204/255 每档停 45s，量出风扇曲线
  python fan_thermostat_profiler.py --sweep --levels 0,51,102,153,204,255 --dwell 45

  # 3) 钉住某个转速看温度怎么爬（例如手动给 120）
  python fan_thermostat_profiler.py --hold 120 --seconds 60

  # 4) 只读仿真：跑未来 Thermostat 控制律，但【不写风扇】，只看它会怎么调
  python fan_thermostat_profiler.py --preview 65 --seconds 120

通用参数：
  --source acpi|ec|hwinfo   控制/读数的温度源（默认 acpi=皮肤温度，矛盾二选定）
  --interval 1              采样间隔(秒)，默认 1
  --hard-ceil 85            硬顶温度(℃)，超过即中止并交还 BIOS，默认 85
  --out PATH                CSV 输出路径（默认 src/logs/fan_thermostat_profile_<时间戳>.csv）
  --no-compare             不额外读 EC/结温做差值对照

设计约定（与已确认的控制律一致，仅用于 --preview 仿真，不影响测量）：
  deadband=2℃（带内不动，抗传感器噪声/热滞后抖动）
  max_step=20（单步 PWM 变化上限 = rate-limit，防过冲振荡）
  K=2.0（PWM/℃，饱和上限 20；最终 K 由本测量器的斜率反推覆盖）
  掉线连续 3 轮 → 交还 BIOS
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from types import SimpleNamespace

# ---- 把 src 加进路径，能 import ningbingjue 包 ----
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "src"))
if SRC_ROOT not in sys.path:
    sys.path.insert(0, SRC_ROOT)

# 注意：FanBlock / FanConfig / ec_io 的 import 故意做成「惰性」（在 build_block /
# run 内才 import），因为 fan.py 顶层会拖进 PyQt6（整个 app 才有）。这样本测量器
# 模块可在无 PyQt6 的环境被 import 与单元测试，真机运行时再惰性加载。


# ---- 控制律：统一复用 core.thermostat（CLI / GUI / 未来 MODE 同一份真相）----
# 注意：本模块被无 PyQt 的环境 import 做单测，而 core.thermostat 是纯逻辑叶子
# 模块（不拖 PyQt），可以安全顶层 import。
from ningbingjue.core.thermostat import (  # noqa: E402
    ThermostatController, clamp, PWM_MAX, DEFAULT_K, DEADBAND_C,
    STEP_LIMIT, HARD_CEIL_C, DROP_ROUNDS,
)

# 兼容本文件其余代码引用的旧变量名
GAIN_K = DEFAULT_K
MAX_STEP_PWM = STEP_LIMIT
HARD_CEIL_DEFAULT = HARD_CEIL_C


def build_block(source: str):
    """构造 FanBlock（脱离 UI，仅用其 EC 写入 + 温度路由 + RPM 读数）。"""
    from ningbingjue.core.config import FanConfig
    from ningbingjue.blocks.fan import FanBlock
    cfg = SimpleNamespace(fan_config=FanConfig())
    block = FanBlock(cfg)
    if source in ("acpi", "ec", "hwinfo"):
        block.set_temp_source(source)
    return block


def restore_auto(block):
    try:
        block._restore_auto()
        print("[cleanup] 已交还 BIOS 自动控制（auto）")
    except Exception as e:  # noqa: BLE001
        print(f"[cleanup] 交还 BIOS 失败：{e}（请手动确认风扇状态）")


def open_csv(out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    f = open(out_path, "w", newline="", encoding="utf-8-sig")
    w = csv.writer(f)
    w.writerow([
        "ts_epoch", "elapsed_s", "mode", "source",
        "temp_c", "temp_ec", "temp_hwinfo",
        "pwm_cmd", "pwm_would", "rpm", "ec_ok", "note",
    ])
    return f, w


def compute_would_pwm(virtual_pwm, temp, target):
    """未来 Thermostat 控制律（只读仿真）：返回「本应写到」的 PWM。

    委托给 core.thermostat.ThermostatController，保证与 GUI / 未来 MODE 一致。
    """
    ctrl = ThermostatController(target_c=target)
    new, delta, _status = ctrl.step(temp, virtual_pwm)
    return new, delta


def run(args):
    if sys.platform != "win32":
        print("✗ 仅支持 Windows（需要 WinRing0 EC 访问）")
        return 2

    from ningbingjue.platform import ec_io
    block = build_block(args.source)
    print(f"[init] 设备识别：{block.detected_model or '未识别'} / "
          f"quirk={block.board_quirk} / temp_source={block.temp_source}")
    print(f"[init] ec_pwm_addr={block.ec_pwm_addr} ec_enable_addr={block.ec_enable_addr} "
          f"rpm_addr={block.ec_rpm_addr}")

    if not block._ec_health():
        print(f"✗ EC 不可用：{ec_io.last_error() or 'WinRing0 未加载'}")
        print("  请：① 以管理员身份运行  ② 关闭 Windows Core Isolation（内存完整性）")
        return 2

    # 启动温度探针
    try:
        block._thermal.ensure_started()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 温度探针启动失败：{e}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = args.out or os.path.join(SRC_ROOT, "logs", f"fan_thermostat_profile_{ts}.csv")
    f, w = open_csv(out_path)
    print(f"[log] CSV → {out_path}\n")

    mode = ("sweep" if args.sweep else
            "hold" if args.hold is not None else
            "preview" if args.preview is not None else "free")
    print(f"=== 模式: {mode} | 源: {block.temp_source} | 硬顶: {args.hard_ceil}℃ | "
          f"间隔: {args.interval}s ===")

    start = time.time()
    drop_count = 0
    virtual_pwm = args.hold if args.hold is not None else (block.last_pwm or 100)
    preview_ctrl = None   # --preview 沿用同一控制器，使观察窗状态跨轮累积

    # sweep 状态
    levels = [clamp(int(x), 0, 255) for x in args.levels.split(",")] if args.sweep else []
    level_idx = 0
    level_start = start
    if args.sweep:
        print(f"[sweep] 档位 = {levels}，每档 {args.dwell}s")
        ok = block._write_fan_pwm(levels[0], reason="sweep")
        if not ok:
            print("✗ 首档写入失败，退出")
            f.close(); restore_auto(block); return 2
    elif args.hold is not None:
        block._write_fan_pwm(args.hold, reason="hold")

    aborted = False
    try:
        while True:
            now = time.time()
            elapsed = now - start

            # sweep 换档
            if args.sweep and (now - level_start) >= args.dwell:
                level_idx += 1
                if level_idx >= len(levels):
                    print("[sweep] 全部档位完成")
                    break
                ok = block._write_fan_pwm(levels[level_idx], reason="sweep")
                level_start = now
                if not ok:
                    print(f"✗ 档位 {levels[level_idx]} 写入失败，中止")
                    aborted = True
                    break

            # 读数
            temp = block._read_temp()
            te = block._read_temp_ec() if not args.no_compare else None
            th = block._read_temp_hwinfo() if not args.no_compare else None
            rpm = block.read_fan_rpm()
            ec_ok = block._ec_health()

            note = ""
            # 掉线累计
            if temp is None:
                drop_count += 1
            else:
                drop_count = 0

            # 硬顶中止
            if temp is not None and temp >= args.hard_ceil:
                note = f"HARD_CEIL {temp:.1f}>={args.hard_ceil:.0f}; abort"
                w.writerow([f"{now:.3f}", f"{elapsed:.1f}", mode, block.temp_source,
                            f"{temp:.2f}" if temp is not None else "",
                            f"{te:.2f}" if te is not None else "",
                            f"{th:.2f}" if th is not None else "",
                            block.last_pwm, "", rpm if rpm is not None else "",
                            int(ec_ok), note])
                print(f"⛔ {note}")
                aborted = True
                break

            # 掉线 3 轮 → 交还 BIOS
            if drop_count >= DROP_ROUNDS:
                note = f"SENSOR_DROP {drop_count} rounds; restore auto"
                w.writerow([f"{now:.3f}", f"{elapsed:.1f}", mode, block.temp_source,
                            "", "", "", block.last_pwm, "", rpm if rpm is not None else "",
                            int(ec_ok), note])
                print(f"⚠ {note}")
                aborted = True
                break

            # --preview 只读仿真（沿用同一控制器，忠实反映「等待收敛」观察窗）
            pwm_would = ""
            if args.preview is not None and temp is not None:
                if preview_ctrl is None or preview_ctrl.target_c != args.preview:
                    preview_ctrl = ThermostatController(target_c=args.preview)
                _n, _d, _st = preview_ctrl.step(temp, virtual_pwm)
                pwm_would = _n
                virtual_pwm = pwm_would

            pwm_cmd = block.last_pwm if mode in ("free", "preview") else (
                levels[level_idx] if args.sweep else args.hold)

            w.writerow([
                f"{now:.3f}", f"{elapsed:.1f}", mode, block.temp_source,
                f"{temp:.2f}" if temp is not None else "",
                f"{te:.2f}" if te is not None else "",
                f"{th:.2f}" if th is not None else "",
                pwm_cmd, (pwm_would if pwm_would != "" else ""),
                rpm if rpm is not None else "",
                int(ec_ok), note,
            ])

            if mode == "free":
                line = f"t={elapsed:6.1f}s  temp={temp if temp is not None else '—':>6}℃"
            elif mode == "preview":
                line = (f"t={elapsed:6.1f}s  temp={temp:6.2f}℃  "
                        f"→ would_pwm={pwm_would} (target={args.preview})")
            else:
                line = (f"t={elapsed:6.1f}s  level[{level_idx}]={pwm_cmd}  "
                        f"temp={temp if temp is not None else '—'}℃  rpm={rpm}")
            if te is not None or th is not None:
                line += f"  | ec={te} hwinfo={th}"
            print(line, flush=True)

            # free/hold/preview 的 --seconds 上限
            if not args.sweep and args.seconds and elapsed >= args.seconds:
                print("[done] 达到 --seconds 上限")
                break

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[interrupted] 用户中止")
    finally:
        f.close()
        if not aborted or mode in ("sweep", "hold"):
            restore_auto(block)
        try:
            block.dispose()
        except Exception:
            pass
        print(f"\n[log] 完成 → {out_path}")
        if mode == "sweep":
            summarize_sweep(out_path, levels)
    return 0


def summarize_sweep(out_path, levels):
    """粗略汇总扫频结果：每档稳态温度 + 相邻档斜率（用于反推 K）。"""
    rows = []
    with open(out_path, encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        for row in r:
            if row.get("mode") != "sweep" or not row.get("temp_c"):
                continue
            rows.append((float(row["pwm_cmd"]), float(row["temp_c"])))
    if not rows:
        return
    # 按档位分组，取后 40% 样本中位数作为稳态温度
    by_level = {}
    for pwm, t in rows:
        by_level.setdefault(pwm, []).append(t)
    print("\n=== 扫频汇总（稳态温度 ≈ 各档后段中位数）===")
    print(f"{'PWM':>5}  {'稳态温度℃':>10}  {'样本数':>6}")
    steady = {}
    for pwm in levels:
        ts_ = by_level.get(float(pwm), [])
        if not ts_:
            continue
        tail = ts_[int(len(ts_) * 0.6):]
        med = sorted(tail)[len(tail) // 2]
        steady[pwm] = med
        print(f"{pwm:>5}  {med:>10.2f}  {len(ts_):>6}")
    # 斜率 dTemp/dPWM（越高说明每档转速对温度影响越大）
    items = sorted(steady.items())
    print("\n  相邻档斜率 dTemp/dPWM（用于定 K，建议 K≈MAX_STEP/|最大斜率×典型误差|）：")
    for i in range(1, len(items)):
        p0, t0 = items[i - 1]
        p1, t1 = items[i]
        if p1 != p0:
            slope = (t1 - t0) / (p1 - p0)
            print(f"    PWM {p0}→{p1}: {slope:+.3f} ℃/PWM")
    print("  例：若 10℃ 误差想用满 20 步，则 K≈20/10=2.0；若斜率很陡，远误差会更快触顶。")


def main():
    ap = argparse.ArgumentParser(description="Thermostat 风扇 plant 测量器")
    ap.add_argument("--free", action="store_true", help="自由观察（只读不写）")
    ap.add_argument("--sweep", action="store_true", help="扫频：逐档写转速测稳态温度")
    ap.add_argument("--hold", type=int, default=None, help="钉住某 PWM 转速观察")
    ap.add_argument("--preview", type=float, default=None,
                    help="只读仿真 Thermostat 控制律（不写风扇），参数为目标温度℃")
    ap.add_argument("--levels", default="0,51,102,153,204,255", help="--sweep 的档位列表")
    ap.add_argument("--dwell", type=float, default=45, help="--sweep 每档停留秒数")
    ap.add_argument("--seconds", type=float, default=None, help="free/hold/preview 总时长")
    ap.add_argument("--source", default="acpi", choices=["acpi", "ec", "hwinfo"])
    ap.add_argument("--interval", type=float, default=1.0, help="采样间隔秒")
    ap.add_argument("--hard-ceil", type=float, default=HARD_CEIL_DEFAULT)
    ap.add_argument("--out", default=None, help="CSV 输出路径")
    ap.add_argument("--no-compare", action="store_true", help="不读 EC/结温做差值")
    args = ap.parse_args()

    if not (args.free or args.sweep or args.hold is not None or args.preview is not None):
        ap.error("必须指定一种模式：--free / --sweep / --hold N / --preview T")

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
