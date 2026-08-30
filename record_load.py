r"""record_load.py - 游戏负载录制（独立、不挑进程、零三方依赖）。

与 measure_game_load.py 的区别：
  - 默认「跟前台窗口」模式：你站在哪个窗口上，就录哪个进程，
    无需游戏识别、无需先等、无需白名单。开游戏->跑脚本->打完收工。
  - 也可以 --exe 指定进程名（固定录某个进程）。
  - 前台切换时自动重新基线，不会把两个进程的 CPU 时间混在一起。
  - 只读，不调用 ryzenadj。输出 CSV 到 <项目根>/logs/record_<ts>.csv。

  本版新增「每核占用」采样（PDH \Processor(*)\% Processor Time），
  从而能算：
    - cpu_max_core  : 最忙核心占用（抓绝区零这类单核瓶颈型游戏）
    - cpu_mean_core : 平均核心占用（抓原神这类负载均衡型游戏）
    - cores         : 全核原始占用串，供画图直观看「一颗红、其余半」
  GPU 占用仍采，作为 heavy 判定的共信号。

用法：
  python record_load.py                       # 跟前台窗口录 300s
  python record_load.py --duration 180       # 录 3 分钟
  python record_load.py --exe genshin.exe    # 固定录某进程
  python record_load.py --no-gpu             # 不采 GPU%
  python record_load.py --no-cores           # 不采每核占用
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

# 复用共享的游戏/前台窗口探测（仅取 PID / exe 名，不依赖 is_game 判定）
# 本脚本直接放在 nginbjie\ 内，src 与脚本同目录
_src = Path(__file__).resolve().parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))
from ningbingjue.core.game_detect import (  # noqa: E402
    find_foreground_pid,
    get_exe_by_pid,
    find_pid_by_exe,
)


# --------------------------------------------------------------------------
# Shared performance probe (single source of truth for GPU/CPU sampling).
# --------------------------------------------------------------------------
from ningbingjue.core.perf_probe import (  # noqa: E402
    start_gpu_monitor,
    start_core_monitor,
    open_process,
    close_process,
    read_proc_times,
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_READ,
    NO_INHERIT,
)


# GPU/CORE probe scripts and monitor starters now live in
# ningbingjue.core.perf_probe (GPU_UTIL_PS, CORE_UTIL_PS,
# start_gpu_monitor, start_core_monitor). This recorder imports them.


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
DEFAULT_HEAVY = 180.0
DEFAULT_LIGHT = 80.0
# 三信号 heavy 阈值（仅用于 phase_hint 预览，真实阈值需按每核数据标定）
DEFAULT_HMAX = 85.0    # cpu_max_core 命中 -> 单核瓶颈型重负载（如绝区零）
DEFAULT_HMEAN = 50.0   # cpu_mean_core 命中 -> 均衡型重负载（如原神）
DEFAULT_HGPU = 55.0    # gpu 命中 -> 重负载渲染


def _phase_v2(maxc, meanc, gpu, hmax, hmean, hgpu) -> str:
    """三信号 OR 判定 heavy/light（仅负载视角；是否在游戏由 GameWatcher 白名单决定）。"""
    if maxc is not None and maxc >= hmax:
        return "heavy"
    if meanc is not None and meanc >= hmean:
        return "heavy"
    if gpu is not None and gpu >= hgpu:
        return "heavy"
    return "light"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="录制前台（或指定）进程的 CPU 占用、每核占用与系统 GPU 占用，用于标定降压阈值。",
    )
    ap.add_argument("--exe", default=None,
                    help="固定录某个进程 exe 名（可省略 .exe）；不传则跟前台窗口")
    ap.add_argument("--duration", type=int, default=300, help="录制秒数（默认 300）")
    ap.add_argument("--interval", type=int, default=500, help="采样间隔 ms（默认 500）")
    ap.add_argument("--no-gpu", action="store_true", help="关闭 GPU 监控")
    ap.add_argument("--no-cores", action="store_true", help="关闭每核占用监控")
    ap.add_argument("--out", default=None, help="CSV 输出路径（默认 logs/record_<ts>.csv）")
    ap.add_argument("--wait-timeout", type=int, default=60,
                    help="--exe 模式下等待进程出现的最长秒数（默认 60）")
    ap.add_argument("--heavy", type=float, default=DEFAULT_HEAVY, help="(保留)聚合 CPU heavy 阈值")
    ap.add_argument("--light", type=float, default=DEFAULT_LIGHT, help="(保留)聚合 CPU light 阈值")
    ap.add_argument("--hmax", type=float, default=DEFAULT_HMAX, help="cpu_max_core 的 heavy 阈值")
    ap.add_argument("--hmean", type=float, default=DEFAULT_HMEAN, help="cpu_mean_core 的 heavy 阈值")
    ap.add_argument("--hgpu", type=float, default=DEFAULT_HGPU, help="gpu 的 heavy 阈值")
    args = ap.parse_args()

    # 脚本所在目录（ningbingjue\）即项目根；logs 同目录，整文件夹自包含
    project_root = Path(__file__).resolve().parent
    logs_dir = project_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    if args.out:
        out_path = Path(args.out)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = logs_dir / f"record_{ts}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    follow = args.exe is None
    print(f"[record] mode     : {'跟前台窗口' if follow else f'--exe {args.exe}'}")
    print(f"[record] interval : {args.interval} ms")
    print(f"[record] duration : {args.duration} s")
    print(f"[record] gpu      : {'off' if args.no_gpu else 'on'}")
    print(f"[record] cores    : {'off' if args.no_cores else 'on'}")
    print(f"[record] log file : {out_path}")
    if not args.no_cores:
        print(f"[record] heavy 阈值(预览): max_core>={args.hmax} 或 mean_core>={args.hmean} 或 gpu>={args.hgpu}")
        print(f"[record] 注: 阈值待按每核数据标定，phase_hint 仅供参考")
    sys.stdout.flush()

    # --exe 模式：先等到进程出现
    fixed_pid = None
    if not follow:
        waited = 0.0
        while True:
            fixed_pid = find_pid_by_exe(args.exe)
            if fixed_pid is not None:
                break
            if waited >= args.wait_timeout:
                print(f"[record] 等待 {args.wait_timeout}s 未找到 {args.exe}，退出。",
                      file=sys.stderr)
                return 2
            time.sleep(1.0)
            waited += 1.0
        print(f"[record] 锁定进程 pid={fixed_pid} ({args.exe}) after {waited:.0f}s")

    n_cores = os.cpu_count() or 1
    gpu_state, gpu_proc = start_gpu_monitor(not args.no_gpu)
    if not args.no_gpu and gpu_state["err"]:
        print(f"[record] {gpu_state['err']}（仅记录 CPU%）", file=sys.stderr)
    core_state, core_proc = start_core_monitor(not args.no_cores)
    if not args.no_cores and core_state["err"]:
        print(f"[record] {core_state['err']}（不记录每核）", file=sys.stderr)

    out_path.write_text("", encoding="utf-8")
    csvfile = out_path.open("w", newline="", encoding="utf-8")
    w = csv.writer(csvfile)
    w.writerow(["timestamp", "elapsed_s", "exe", "cpu_pct", "gpu_pct",
                "cpu_max_core", "cpu_mean_core", "cores", "phase_hint"])
    csvfile.flush()

    interval_s = max(0.05, args.interval / 1000.0)
    start = time.monotonic()
    end_t = start + args.duration

    # 当前目标进程的句柄 / 基线
    cur_pid = None
    cur_h = None
    cur_exe = ""
    prev_k = prev_u = prev_t = None

    def _switch_to(tpid: int | None):
        """切换录制目标；返回 True 表示已就位可采样，False 表示本帧跳过。"""
        nonlocal cur_pid, cur_h, cur_exe, prev_k, prev_u, prev_t
        if cur_h:
            close_process(cur_h)
            cur_h = None
        cur_pid = None
        if not tpid:
            return False
        h = open_process(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, NO_INHERIT, tpid)
        if not h:
            return False
        base = read_proc_times(h)
        if base is None:
            close_process(h)
            return False
        cur_pid = tpid
        cur_h = h
        cur_exe = get_exe_by_pid(tpid) or f"pid{tpid}"
        prev_k, prev_u = base
        prev_t = time.monotonic()
        return True

    def _core_stats():
        cores = core_state["cores"]
        if not cores:
            return None, None, ""
        maxc = max(cores.values())
        meanc = sum(cores.values()) / len(cores)
        cores_str = ",".join(f"{v:.1f}" for v in cores.values())
        return maxc, meanc, cores_str

    print("[record] 录制中... (Ctrl+C 提前结束)")
    try:
        while True:
            now = time.monotonic()
            tpid = fixed_pid if not follow else find_foreground_pid()

            maxc, meanc, cores_str = _core_stats()
            gpu_v = gpu_state["v"]

            # 目标进程变了（或首次）→ 重新基线，本帧只写占位
            if tpid != cur_pid:
                ready = _switch_to(tpid)
                if not ready:
                    w.writerow([_now_iso(), f"{now-start:.2f}", "", "",
                                f"{gpu_v:.1f}" if gpu_v is not None else "",
                                f"{maxc:.1f}" if maxc is not None else "",
                                f"{meanc:.1f}" if meanc is not None else "",
                                cores_str, ""])
                else:
                    w.writerow([_now_iso(), f"{now-start:.2f}", cur_exe, "0.00",
                                f"{gpu_v:.1f}" if gpu_v is not None else "",
                                f"{maxc:.1f}" if maxc is not None else "",
                                f"{meanc:.1f}" if meanc is not None else "",
                                cores_str, "baseline"])
                csvfile.flush()
                if now >= end_t:
                    break
                time.sleep(interval_s)
                continue

            # 正常 delta 采样
            times = read_proc_times(cur_h) if cur_h else None
            if times is None:
                w.writerow([_now_iso(), f"{now-start:.2f}", cur_exe, "",
                            f"{gpu_v:.1f}" if gpu_v is not None else "",
                            f"{maxc:.1f}" if maxc is not None else "",
                            f"{meanc:.1f}" if meanc is not None else "",
                            cores_str, ""])
                csvfile.flush()
                cur_pid = None  # 强制下一帧重基线
                if now >= end_t:
                    break
                time.sleep(interval_s)
                continue

            k, u = times
            dt = now - prev_t
            cpu_pct = ((k - prev_k + u - prev_u) / 10_000_000.0 / dt / n_cores * 100.0) \
                if dt > 0 else 0.0
            prev_k, prev_u, prev_t = k, u, now
            ph = _phase_v2(maxc, meanc, gpu_v, args.hmax, args.hmean, args.hgpu)
            w.writerow([
                _now_iso(), f"{now-start:.2f}", cur_exe,
                f"{cpu_pct:.2f}",
                f"{gpu_v:.1f}" if gpu_v is not None else "",
                f"{maxc:.1f}" if maxc is not None else "",
                f"{meanc:.1f}" if meanc is not None else "",
                cores_str,
                ph,
            ])
            csvfile.flush()
            print(f"[t={now-start:6.1f}s] {cur_exe:24s} cpu={cpu_pct:6.2f}% "
                  f"maxc={maxc if maxc is not None else float('nan'):6.1f}% "
                  f"meanc={meanc if meanc is not None else float('nan'):6.1f}% "
                  f"gpu={gpu_v if gpu_v is not None else float('nan'):6.1f}% {ph}",
                  flush=True)

            if now >= end_t:
                break
            time.sleep(interval_s)
    except KeyboardInterrupt:
        print("\n[record] Ctrl+C，提前结束。")
    finally:
        csvfile.close()
        if cur_h:
            close_process(cur_h)
        for p in (gpu_proc, core_proc):
            if p:
                try:
                    p.terminate()
                except OSError:
                    pass

    print(f"[record] DONE -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
