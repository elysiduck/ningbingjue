"""measure_game_load.py — 游戏负载录制工具（只读，不调用 ryzenadj）。

目的：为负载门控 CO 监控（UndervoltController）采集真实阈值数据。
记录目标游戏进程的：
  - 进程自身 %Processor Time（主信号，ctypes+GetProcessTimes 500ms 采样）
  - 系统 GPU% 占用（辅信号，后台 PowerShell Get-Counter 1.5s 采样，可关闭）
  - 自动 phase_hint: heavy / light —— **必须与 UndervoltController 用同一套信号
    与阈值语义**才能直接标定控制器：有每核数据时按
    max_core>=--heavy OR mean_core>=--light 判 HEAVY（与控制器 cpu_heavy 规则一致），
    否则 LIGHT；关每核(--no-cores)时退回进程相对 cpu_pct（量程不同，0..100 相对总核，
    不如每核准，校准请保持每核开）。--heavy/--light 默认值已与控制器 hmax/hmean(75/35) 对齐
    （2026-07-29 据绝区零实机 2415 样本标定：50/25 让 HEAVY 占 90%、LIGHT 档失效；
    75/35 让 HEAVY 仅约 16% 的 P90 尖峰触发，正常游玩走 LIGHT(-20)）。

零第三方依赖；输出 CSV 到 <项目根>/logs/measure_YYYYMMDD_HHMMSS.csv。

用法（脚本位于 test-launch/py-script/ 内）：
  python test-launch/py-script/measure_game_load.py --exe eldenring.exe
  python test-launch/py-script/measure_game_load.py --exe starfield.exe --duration 180 --no-gpu
  python test-launch/py-script/measure_game_load.py --auto        # 自动探测前台游戏窗口
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

# 共享游戏探测（与 freeze block 共用，避免重复关键字表 / 前台窗口逻辑）
# 本脚本位于 <项目根>/test-launch/py-script/ 内；上溯 3 层
# （py-script -> test-launch -> 项目根）即 nginbjie/，其下 src / config / logs
# 全收进此目录，整文件夹拷到任意位置都能跑。
_project_root = Path(__file__).resolve().parent.parent.parent
_script_dir = Path(__file__).resolve().parent
_src = _project_root / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))
from ningbingjue.core.game_detect import (  # noqa: E402
    is_game_process,
    find_foreground_game,
    find_pid_by_exe,
    get_exe_by_pid,
    find_foreground_pid,
    enum_game_processes,
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


# GPU probe script now lives in ningbingjue.core.perf_probe.GPU_UTIL_PS
# (this recorder uses the shared start_gpu_monitor).


# start_gpu_monitor is imported from ningbingjue.core.perf_probe.


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
# 默认值与 UndervoltController 的 hmax/hmean 对齐（50/25），使 phase_hint 直接
# 反映控制器“cpu_heavy = max_core>=hmax OR mean_core>=hmean”的判定口径。
# 旧默认值 180/80 是基于“进程相对总核 %”（上限 100）的残留，180 永不可达，
# 会导致 phase_hint 恒为 light/mid、无法标定 —— 已废弃。
DEFAULT_HEAVY = 75.0
DEFAULT_LIGHT = 35.0


def _phase(maxc: float | None, meanc: float | None,
           heavy: float, light: float,
           no_cores: bool, cpu_pct: float) -> str:
    """phase_hint 必须与 UndervoltController 同口径，才能用于标定控制器阈值。

    - 有每核数据：HEAVY 当 max_core>=--heavy OR mean_core>=--light（与控制器
      cpu_heavy 规则逐字一致）；否则 LIGHT。
    - 关了每核(--no-cores)：退回进程相对 cpu_pct（量程 0..100 相对总核，
      与每核单核%不同），仅作粗略参考。
    """
    if not no_cores and (maxc is not None or meanc is not None):
        heavy_now = (
            (maxc is not None and maxc >= heavy)
            or (meanc is not None and meanc >= light)
        )
        return "heavy" if heavy_now else "light"
    # 退回进程相对 cpu_pct（量程不同，仅供参考）
    if cpu_pct >= heavy:
        return "heavy"
    if cpu_pct < light:
        return "light"
    return "mid"


def _core_stats(state: dict, disabled: bool):
    """从 perf_probe 的 core_state 算出 (cores_csv, avg, max)。

    cores_csv 形如 "12.3,45.6,8.1,..."（按核号升序）；无任何数据时返回 ('', None, None)。
    """
    if disabled:
        return "", None, None
    cores = state.get("cores")
    if not cores:
        return "", None, None
    cvals = [cores[k] for k in sorted(cores, key=lambda x: int(x))]
    if not cvals:
        return "", None, None
    return (
        ",".join(f"{v:.1f}" for v in cvals),
        sum(cvals) / len(cvals),
        max(cvals),
    )


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _match_exe(target: str, exe: str) -> bool:
    """--exe 与目标 exe 的宽松匹配（忽略大小写/.exe），支持多开实例。"""
    t = target.lower().replace(".exe", "")
    e = exe.lower().replace(".exe", "")
    return (t in e) or (e in t)


def _select_procs(args, waited_pid, waited_exe) -> list[tuple[int, str]]:
    """录制进程集合（多进程 + 过滤）。

    - --auto：取当前所有 is_game_process 命中的进程（enum_game_processes 已过滤）。
    - --exe：只保留与目标 exe 匹配的实例（支持多开）；若目标未被识别为游戏，
      回退到等待到的单个进程，保证仍能录制。
    """
    if args.auto:
        cands = enum_game_processes()
        if cands:
            return cands
        return [(waited_pid, waited_exe)] if waited_pid else []
    cands = [(p, e) for (p, e) in enum_game_processes() if _match_exe(args.exe, e)]
    if cands:
        return cands
    return [(waited_pid, waited_exe)] if waited_pid else []


def main() -> int:
    ap = argparse.ArgumentParser(
        description="录制游戏的进程 CPU 占用与系统 GPU 占用，用于标定降压阈值。",
    )
    ap.add_argument("--exe", required=False, default=None,
                    help="目标游戏 exe 名（可省略 .exe）；与 --auto 二选一")
    ap.add_argument("--auto", action="store_true",
                    help="自动探测前台游戏窗口（关键字+前台，无需 --exe）")
    ap.add_argument("--duration", type=int, default=300, help="录制秒数（默认 300）")
    ap.add_argument("--interval", type=int, default=500, help="CPU 采样间隔 ms（默认 500）")
    ap.add_argument("--no-gpu",   action="store_true", help="关闭 GPU 监控")
    ap.add_argument("--no-cores", action="store_true",
                    help="关闭每核 CPU 监控（不写 cpu_cores/cpu_avg/cpu_max 列）")
    ap.add_argument("--out",      default=None, help="CSV 输出路径（默认 logs/measure_<ts>.csv）")
    ap.add_argument("--report",   action="store_true",
                    help="录制结束后自动生成 HTML 报表（同目录 <csv>.html）")
    ap.add_argument("--stop-file", default=None,
                    help="若此文件存在则干净停止录制（用于 UI 手动停止，"
                         "避免残留 GPU/CPU 监控子进程）")
    ap.add_argument("--wait-timeout", type=int, default=120,
                    help="等待游戏出现的最长时间（秒，默认 120）")
    ap.add_argument("--heavy", type=float, default=DEFAULT_HEAVY,
                    help="HEAVY 判定阈值(对应控制器 hmax，单核最大%%)；默认 75")
    ap.add_argument("--light", type=float, default=DEFAULT_LIGHT,
                    help="HEAVY 判定阈值(对应控制器 hmean，单核均值%%)；默认 35")
    args = ap.parse_args()
    if not args.auto and not args.exe:
        ap.error("必须提供 --exe <游戏名> 或 --auto 之一")

    # 项目根 = 上溯 3 层（py-script -> test-launch -> 项目根）；logs 在其下
    project_root = _project_root
    logs_dir = project_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    if args.out:
        out_path = Path(args.out)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = logs_dir / f"measure_{ts}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    target = args.exe
    print(f"[measure] mode     : {'auto(前台窗口)' if args.auto else f'--exe {target}'}")
    print(f"[measure] interval : {args.interval} ms")
    print(f"[measure] duration : {args.duration} s")
    print(f"[measure] gpu      : {'off' if args.no_gpu else 'on'}")
    print(f"[measure] report   : {'on' if args.report else 'off'}")
    print(f"[measure] phase_hint: 基于每核信号(max>={args.heavy} OR mean>={args.light})"
          f" 与控制器同口径；关每核(--no-cores)时退回进程相对 cpu_pct(量程不同)")
    print(f"[measure] log file : {out_path}")
    sys.stdout.flush()

    # 定位游戏进程：--auto 轮询前台窗口，--exe 按名等待。
    # 仅用于“等到一个目标”，真正的录制进程集在下方用 enum_game_processes() 快照。
    waited = 0.0
    waited_pid = None
    waited_exe = target
    last_fg = None
    while waited_pid is None:
        if args.stop_file and os.path.exists(args.stop_file):
            print("[measure] 收到停止信号（等待阶段），干净退出。")
            return 0
        if args.auto:
            res = find_foreground_game(None)
            if res:
                waited_pid, waited_exe = res
            else:
                fpid = find_foreground_pid()
                fname = get_exe_by_pid(fpid) if fpid else None
                if fname and fname != last_fg:
                    print(f"[measure] 前台窗口: {fname} （未命中关键字/白名单，跳过）",
                          file=sys.stderr)
                    last_fg = fname
        else:
            waited_pid = find_pid_by_exe(target)
        if waited_pid is not None:
            break
        if waited >= args.wait_timeout:
            if args.auto:
                print(f"[measure] 等待 {args.wait_timeout}s 未检测到前台游戏窗口"
                      f"（请先切到游戏画面，或用 --exe <游戏名> 指定）。"
                      f"最后看到的前台: {last_fg}", file=sys.stderr)
            else:
                print(f"[measure] 等待 {args.wait_timeout}s 未找到 {target}，退出。",
                      file=sys.stderr)
            return 2
        time.sleep(1.0)
        waited += 1.0
    print(f"[measure] 等待到目标 pid={waited_pid} ({waited_exe}) after {waited:.0f}s")

    # 录制进程集：--auto 取全部游戏进程；--exe 取匹配实例（多开）。过滤由 is_game_process 保证。
    procs = _select_procs(args, waited_pid, waited_exe)
    if not procs:
        print("[measure] 未选定任何进程，退出。", file=sys.stderr)
        return 2
    proc_exe = {pid: exe for pid, exe in procs}
    print("[measure] 录制进程集: " +
          ", ".join(f"{exe}({pid})" for pid, exe in procs))

    # 每个进程单独开句柄
    handles: dict[int, tuple] = {}  # pid -> (h, prev_k, prev_u, prev_t)
    for pid, exe in procs:
        h = open_process(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, NO_INHERIT, pid)
        if not h:
            print(f"[measure] OpenProcess 失败 {exe}({pid}) err={ctypes.get_last_error()}",
                  file=sys.stderr)
            continue
        handles[pid] = (h, None, None, None)

    n_cores = os.cpu_count() or 1
    gpu_state, gpu_proc = start_gpu_monitor(not args.no_gpu)
    if not args.no_gpu and gpu_state["err"]:
        print(f"[measure] {gpu_state['err']}（仅记录 CPU%）")

    core_state, core_proc = start_core_monitor(not args.no_cores)
    if not args.no_cores and core_state["err"]:
        print(f"[measure] {core_state['err']}（不记录每核/均值/最大）")

    # 基线（让第一次 delta 有意义）
    start = time.monotonic()
    alive = 0
    for pid, (h, _, _, _) in handles.items():
        base = read_proc_times(h)
        if base is None:
            print(f"[measure] 进程 {proc_exe.get(pid, str(pid))} 基线失败（已退出？），跳过。",
                  file=sys.stderr)
            continue
        handles[pid] = (h, base[0], base[1], start)
        alive += 1
    if alive == 0:
        print("[measure] 所有进程都已退出，退出。", file=sys.stderr)
        if gpu_proc:
            gpu_proc.terminate()
        if core_proc:
            core_proc.terminate()
        return 1

    def _sanitize(exe: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "_", exe)

    proc_cols = [f"cpu__{_sanitize(exe)}__{pid}" for pid, exe in procs]
    out_path.write_text("", encoding="utf-8")  # truncate
    csvfile = out_path.open("w", newline="", encoding="utf-8")
    w = csv.writer(csvfile)
    w.writerow(["timestamp", "elapsed_s", "gpu_pct"] + proc_cols +
               ["cpu_cores", "cpu_avg", "cpu_max", "phase_hint"])
    csvfile.flush()

    interval_s = max(0.05, args.interval / 1000.0)
    end_t = start + args.duration

    print("[measure] 录制中... (Ctrl+C 或 --stop-file 提前结束)")
    try:
        while True:
            # 先检测停止信号，再 sleep，让 UI 点击停止后尽量立即响应
            if args.stop_file and os.path.exists(args.stop_file):
                print("[measure] 收到停止信号，干净退出。")
                break
            time.sleep(interval_s)
            now = time.monotonic()
            gpu_v = gpu_state["v"]
            cpu_cores, cpu_avg, cpu_max = _core_stats(core_state, args.no_cores)

            # 逐进程 CPU%（该进程 %Processor Time / 总核数，与单进程口径一致）
            proc_vals: dict[int, float | None] = {}
            for pid, st in list(handles.items()):
                h, prev_k, prev_u, prev_t = st
                times_ = read_proc_times(h)
                if times_ is None:
                    proc_vals[pid] = None
                    continue
                k, u = times_
                dt = now - prev_t
                if dt > 0 and prev_k is not None:
                    cpu_sec = (k - prev_k + u - prev_u) / 10_000_000.0
                    cpu_pct = (cpu_sec / dt / n_cores) * 100.0
                else:
                    cpu_pct = 0.0
                handles[pid] = (h, k, u, now)
                proc_vals[pid] = cpu_pct

            max_proc = max([v for v in proc_vals.values() if v is not None] + [0.0])
            row_vals = [proc_vals.get(pid) for pid, _ in procs]
            w.writerow([
                _now_iso(),
                f"{now-start:.2f}",
                f"{gpu_v:.1f}" if gpu_v is not None else "",
            ] + [f"{v:.2f}" if v is not None else "" for v in row_vals] + [
                cpu_cores,
                f"{cpu_avg:.1f}" if cpu_avg is not None else "",
                f"{cpu_max:.1f}" if cpu_max is not None else "",
                _phase(cpu_max, cpu_avg, args.heavy, args.light,
                       args.no_cores, max_proc),
            ])
            csvfile.flush()

            if now >= end_t:
                break
    except KeyboardInterrupt:
        print("\n[measure] Ctrl+C，提前结束。")
    finally:
        csvfile.close()
        for pid, (h, _, _, _) in handles.items():
            close_process(h)
        if gpu_proc:
            try:
                gpu_proc.terminate()
            except OSError:
                pass
        if core_proc:
            try:
                core_proc.terminate()
            except OSError:
                pass

    print(f"[measure] DONE -> {out_path}")
    if args.report:
        try:
            if str(_script_dir) not in sys.path:
                sys.path.insert(0, str(_script_dir))
            from gen_uv_report import generate_report
            rep = generate_report(out_path)
            print(f"[measure] REPORT -> {rep}")
        except Exception as e:  # 报表失败不影响录制结果
            print(f"[measure] 报表生成失败: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:        # pragma: no cover
        print(f"[measure] FATAL: {e}", file=sys.stderr)
        sys.exit(1)
