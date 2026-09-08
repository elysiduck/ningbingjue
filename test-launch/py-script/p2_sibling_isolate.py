#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P2 — 兄弟隔离施加（单核喂频）

在 P1 只读识别的基础上，真正把"游戏进程亲和排除最热核的 SMT 兄弟 LP"施加到系统。
设计铁律（来自项目约定）：
  * 默认 dry-run：只打印"将要做/将恢复什么"，绝不调用 SetProcessAffinityMask。
    必须显式传 --apply 才会真正修改亲和。
  * 只在该次采样的 P1 判定为 APPLY（存在单线程瓶颈）时才施加；SKIP 不碰任何亲和。
  * 施加前快照原亲和掩码，并在以下任一情况自动还原：判定翻回 SKIP / 目标进程退出 /
    Ctrl+C / 异常 / 脚本崩溃（通过落盘 session registry 实现崩溃安全）。
  * 排除范围极小：每次只去掉最热核那一个 SMT 兄弟 LP（24->23 个 LP 可用），
    游戏多线程余量几乎无损。

复用 p1_topo_probe 的：enumerate_topology / evaluate_hotspot / sample_core_util /
load_config / DEFAULT_CONFIG_PATH / _load_perf_probe。

注意（处理器组）：
  HX370 共 24 个逻辑处理器，落在单一处理器组(processor group)内，可用 64 位亲和掩码。
  若未来机器 >64 LP（多组），需改用 SetThreadGroupAffinity / GROUP_AFFINITY，此处不做。

仅依赖标准库 + ctypes，无三方依赖。
"""
import argparse
import ctypes
import json
import os
import struct
import sys
import time

# 复用 P1 的识别/采样逻辑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import p1_topo_probe as p1  # noqa: E402

# --------------------------------------------------------------------------
# 亲和掩码操作（kernel32，ctypes）
# --------------------------------------------------------------------------
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_INFORMATION = 0x0200

kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
kernel32.CloseHandle.restype = ctypes.c_int
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.GetProcessAffinityMask.restype = ctypes.c_int
kernel32.GetProcessAffinityMask.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_ulonglong),
    ctypes.POINTER(ctypes.c_ulonglong),
]
kernel32.SetProcessAffinityMask.restype = ctypes.c_int
kernel32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_ulonglong]


def open_proc(pid, access):
    h = kernel32.OpenProcess(access, False, pid)
    if not h:
        raise OSError(f"OpenProcess({pid}) 失败: WinErr={ctypes.get_last_error()}")
    return h


def get_affinity(pid):
    """返回 (proc_mask, sys_mask)。失败抛异常。"""
    h = open_proc(pid, PROCESS_QUERY_INFORMATION)
    try:
        pm = ctypes.c_ulonglong(0)
        sm = ctypes.c_ulonglong(0)
        ok = kernel32.GetProcessAffinityMask(h, ctypes.byref(pm), ctypes.byref(sm))
        if not ok:
            raise OSError(f"GetProcessAffinityMask({pid}) 失败: WinErr={ctypes.get_last_error()}")
        return pm.value, sm.value
    finally:
        kernel32.CloseHandle(h)


def set_affinity(pid, mask):
    h = open_proc(pid, PROCESS_SET_INFORMATION)
    try:
        ok = kernel32.SetProcessAffinityMask(h, ctypes.c_ulonglong(mask))
        if not ok:
            raise OSError(f"SetProcessAffinityMask({pid},0x{mask:X}) 失败: WinErr={ctypes.get_last_error()}")
    finally:
        kernel32.CloseHandle(h)


def lp_bit(lp):
    return 1 << lp


# --------------------------------------------------------------------------
# 目标进程解析
# --------------------------------------------------------------------------
def resolve_target_pid(args, perf_probe):
    """返回 (pid, name) 或 (None, reason)。"""
    if args.pid:
        return args.pid, f"--pid 指定({args.pid})"
    if perf_probe is None:
        return None, "无法加载 perf_probe，且未提供 --pid"
    try:
        game = p1.find_foreground_game(perf_probe)
    except Exception as exc:  # 任何探测异常都不应让 P2 崩
        return None, f"前台游戏探测异常: {exc}"
    if not game:
        return None, "未检测到前台游戏（请前台运行游戏，或显式 --pid）"
    return game["pid"], f"前台游戏 {game.get('name')} (exe={game.get('exe')})"


def enum_child_pids(ppid):
    """实验性：经 NtQuerySystemInformation 枚举 ppid 的直接子进程。

    偏移按 x64 布局（UniqueProcessId@0x48, InheritedFrom@0x50）。失败返回 []。
    仅 --include-children 时调用，且失败不影响主进程逻辑。
    """
    try:
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtQuerySystemInformation.restype = ctypes.c_int
        ntdll.NtQuerySystemInformation.argtypes = [
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)
        ]
        length = ctypes.c_uint32(0)
        ntdll.NtQuerySystemInformation(5, None, 0, ctypes.byref(length))
        buf = ctypes.create_string_buffer(length.value)
        if ntdll.NtQuerySystemInformation(5, buf, length, None) != 0:
            return []
        ptr_size = 8 if struct.calcsize("P") == 8 else 4
        pid_off = 0x48 if ptr_size == 8 else 0x44
        ppid_off = 0x50 if ptr_size == 8 else 0x48
        raw = buf.raw
        n = length.value
        ptr = 0
        found = []
        while ptr + 8 <= n:
            nxt = struct.unpack_from("<I", raw, ptr)[0]
            if ptr + pid_off + 8 > n:
                break
            cpid = struct.unpack_from("<Q" if ptr_size == 8 else "<I", raw, ptr + pid_off)[0]
            cppid = struct.unpack_from("<Q" if ptr_size == 8 else "<I", raw, ptr + ppid_off)[0]
            if cppid == ppid and cpid != ppid:
                found.append(int(cpid))
            if nxt == 0:
                break
            ptr += nxt
        return found
    except Exception:
        return []


# --------------------------------------------------------------------------
# 施加 / 还原（带 session registry 实现崩溃安全）
# --------------------------------------------------------------------------
SESSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".p2_session.json")


def _mask_to_lps(mask, total_lp):
    return [i for i in range(total_lp) if mask & lp_bit(i)]


def apply_isolation(pid, ev, topology, dry_run, log):
    """对 pid 排除最热核的 SMT 兄弟。返回原 proc_mask（用于还原）；失败返回 None。"""
    exclude_lps = sorted(ev["exclude_lps"])
    if not exclude_lps:
        log(f"  [skip] PID={pid} 无 SMT 兄弟可排除（已是独占物理核）")
        return None
    try:
        proc_mask, sys_mask = get_affinity(pid)
    except OSError as exc:
        log(f"  [warn] PID={pid} 读取亲和失败（可能权限不足/进程已退出）: {exc}")
        return None
    exclude_mask = 0
    for lp in exclude_lps:
        exclude_mask |= lp_bit(lp)
    new_mask = proc_mask & ~exclude_mask
    # 防御：必须保留至少一个可用 LP，且与原掩码不同才动手
    if new_mask == proc_mask:
        log(f"  [skip] PID={pid} 原掩码已不含兄弟 LP（无需改）")
        return None
    if new_mask == 0 or (new_mask & sys_mask) == 0:
        log(f"  [warn] PID={pid} 排除后无可运行 LP，放弃（保护进程不被饿死）")
        return None
    if dry_run:
        log(f"  [DRY-RUN] PID={pid} 原掩码={_mask_to_lps(proc_mask, topology['total_lp'])} "
            f"-> 将排除 {['LP'+str(x) for x in exclude_lps]} -> 新掩码={_mask_to_lps(new_mask, topology['total_lp'])}")
        return None
    set_affinity(pid, new_mask)
    log(f"  [APPLY] PID={pid} 排除兄弟 {['LP'+str(x) for x in exclude_lps]} "
        f"| 原={_mask_to_lps(proc_mask, topology['total_lp'])} -> 现={_mask_to_lps(new_mask, topology['total_lp'])}")
    return proc_mask


def restore_isolation(pid, original_mask, topology, dry_run, log):
    if original_mask is None:
        return
    try:
        if dry_run:
            log(f"  [DRY-RUN] PID={pid} 将还原掩码={_mask_to_lps(original_mask, topology['total_lp'])}")
            return
        cur, _ = get_affinity(pid)
        if cur == original_mask:
            return
        set_affinity(pid, original_mask)
        log(f"  [RESTORE] PID={pid} 还原掩码={_mask_to_lps(original_mask, topology['total_lp'])}")
    except OSError as exc:
        log(f"  [warn] PID={pid} 还原失败（进程可能已退出）: {exc}")


def save_session(registry):
    try:
        with open(SESSION_PATH, "w", encoding="utf-8") as f:
            json.dump(registry, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_session():
    try:
        if os.path.exists(SESSION_PATH):
            with open(SESSION_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def clear_session():
    try:
        if os.path.exists(SESSION_PATH):
            os.remove(SESSION_PATH)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def run_once(topology, perf_probe, cfg, cfg_source, args, applied, log):
    util, err = p1.sample_core_util(perf_probe, args.samples)
    if util is None:
        log(f"[warn] per-core 采样失败: {err}")
        return
    ev = p1.evaluate_hotspot(topology, util, cfg, cfg_source)
    if ev is None:
        log("[warn] 无占用数据，跳过本轮")
        return
    verdict = "APPLY" if ev["is_bottleneck"] else "SKIP"
    log(f"[采样] 最热 LP{ev['hot_lp']}={ev['hot_val']:.1f}%  verdict={verdict}")
    pids = applied["pids"]
    # 滞后（hysteresis）：避免 APPLY<->SKIP 在采样噪声下抖动导致反复施加/还原造成卡顿。
    # 需连续 2 轮同判定才真正切换状态。
    CONFIRM = 2
    if verdict == "APPLY":
        applied["restore_cnt"] = 0
        if pids:
            return  # 已施加，仅维持
        applied["apply_cnt"] = applied.get("apply_cnt", 0) + 1
        if applied["apply_cnt"] < CONFIRM:
            log(f"[info] APPLY 待确认（连续 {applied['apply_cnt']}/{CONFIRM}），暂缓施加")
            return
        pid, pname = resolve_target_pid(args, perf_probe)
        if pid is None:
            log(f"[warn] 未取得目标进程：{pname}")
            applied["apply_cnt"] = 0
            return
        targets = [pid]
        if args.include_children:
            kids = enum_child_pids(pid)
            if kids:
                targets += kids
                log(f"[info] 含子进程: {kids}")
        # 快照所有目标原掩码
        snap = {}
        for t in targets:
            try:
                pm, _ = get_affinity(t)
                snap[t] = pm
            except OSError:
                snap[t] = None
        orig = dict(snap)
        for t in targets:
            if snap.get(t) is None:
                continue
            apply_isolation(t, ev, topology, args.dry_run, log)
        if not args.dry_run:
            applied["pids"] = targets
            applied["orig"] = orig
            applied["exclude"] = sorted(ev["exclude_lps"])
            save_session(applied)
            log(f"[state] 已施加兄弟隔离，监控中（Ctrl+C 还原）。session 已落盘。")
        applied["apply_cnt"] = 0
    else:
        applied["apply_cnt"] = 0
        if not pids:
            return
        applied["restore_cnt"] = applied.get("restore_cnt", 0) + 1
        if applied["restore_cnt"] < CONFIRM:
            log(f"[info] SKIP 待确认（连续 {applied['restore_cnt']}/{CONFIRM}），暂缓还原")
            return
        log("[state] 判定稳定为 SKIP，还原亲和...")
        for t in pids:
            restore_isolation(t, applied["orig"].get(t), topology, args.dry_run, log)
        applied["pids"] = []
        applied["orig"] = {}
        clear_session()
        applied["restore_cnt"] = 0
        log("[state] 已还原。")


def selftest(args, log):
    """在当前进程上验证 ctypes 亲和机制（无需游戏）。施加+立即还原。"""
    pid = os.getpid()
    topo = p1.enumerate_topology()
    if topo is None:
        log("[selftest] 拓扑枚举失败，退出")
        return 1
    # 找一个带 SMT 兄弟的物理核
    for lp, sibs in topo["lp_siblings"].items():
        if sibs:
            ex = sorted(sibs)[0]
            break
    else:
        log("[selftest] 无 SMT 兄弟，无法自测")
        return 1
    try:
        pm, sm = get_affinity(pid)
    except OSError as exc:
        log(f"[selftest] 读取自身亲和失败: {exc}")
        return 1
    new_mask = pm & ~lp_bit(ex)
    if new_mask == 0 or (new_mask & sm) == 0:
        log("[selftest] 排除后无可运行 LP，跳过实际写入（仅验证读取）")
        log(f"[selftest] 原掩码 LP={_mask_to_lps(pm, topo['total_lp'])} 读取 OK")
        return 0
    log(f"[selftest] 原掩码 LP={_mask_to_lps(pm, topo['total_lp'])}")
    set_affinity(pid, new_mask)
    cur, _ = get_affinity(pid)
    log(f"[selftest] 写入排除 LP{ex} 后掩码 LP={_mask_to_lps(cur, topo['total_lp'])}")
    set_affinity(pid, pm)
    back, _ = get_affinity(pid)
    ok = (back == pm)
    log(f"[selftest] 还原后掩码匹配原值: {'OK' if ok else 'MISMATCH!'}")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="P2 兄弟隔离施加（单核喂频）")
    ap.add_argument("--apply", action="store_true",
                    help="真正施加亲和修改。缺省为 dry-run（只打印计划，不修改系统）")
    ap.add_argument("--loop", action="store_true", help="持续监控：APPLY 时施加，SKIP 时还原")
    ap.add_argument("--samples", type=int, default=3, help="每次采样次数(默认3)")
    ap.add_argument("--pid", type=int, default=None, help="显式指定目标进程 PID（否则取前台游戏）")
    ap.add_argument("--include-children", action="store_true",
                    help="实验性：同时对子进程施加（反作弊/launcher）。默认仅主进程")
    ap.add_argument("--config", default=p1.DEFAULT_CONFIG_PATH, help="阈值配置文件路径")
    ap.add_argument("--selftest", action="store_true", help="在当前进程验证 ctypes 亲和机制")
    ap.add_argument("--out", default=None, help="把日志同时写入此文件(UTF-8)")
    args = ap.parse_args()

    out_f = open(args.out, "w", encoding="utf-8") if args.out else None

    def log(msg):
        print(msg)
        if out_f:
            out_f.write(msg + "\n")
            out_f.flush()

    try:
        if args.selftest:
            rc = selftest(args, log)
            if out_f:
                out_f.close()
            return rc

        cfg, cfg_source = p1.load_config(args.config)
        perf_probe = p1._load_perf_probe()
        topology = p1.enumerate_topology()
        if topology is None:
            log("[ERROR] 拓扑枚举失败")
            if out_f:
                out_f.close()
            return 2

        mode = "APPLY(真改)" if args.apply else "DRY-RUN(只报告)"
        log("=" * 64)
        log(f" P2 兄弟隔离  mode={mode}")
        log(f" 拓扑: {len(topology['phys_cores'])} 核 / {topology['total_lp']} LP  "
            f"EfficiencyClass={topology['eff_classes']}")
        log(f" 配置来源: {cfg_source}")
        log("=" * 64)

        # 崩溃恢复：若上次异常退出遗留 session，提示/还原
        stale = load_session()
        if stale and stale.get("pids") and not args.dry_run:
            log(f"[recover] 发现上次遗留 session（PID={stale['pids']}），先还原再继续")
            for t in stale["pids"]:
                restore_isolation(t, stale.get("orig", {}).get(t), topology, False, log)
            clear_session()

        applied = {"pids": [], "orig": {}, "exclude": []}

        def loop_body():
            run_once(topology, perf_probe, cfg, cfg_source, args, applied, log)

        try:
            if args.loop:
                log("[loop] 每 3 秒评估；Ctrl+C 退出并还原")
                while True:
                    loop_body()
                    time.sleep(3)
            else:
                loop_body()
                if applied["pids"] and not args.dry_run:
                    log("[info] 单次模式已施加。如需持续监控请加 --loop；进程需手动还原时运行 --selftest 之外...")
                    log("       或重新运行本脚本（会自动检测并还原）。")
        except KeyboardInterrupt:
            log("\n[Ctrl+C] 还原亲和...")
            for t in applied["pids"]:
                restore_isolation(t, applied["orig"].get(t), topology, args.dry_run, log)
            clear_session()
            log("[done] 已退出并还原。")
        except Exception as exc:
            log(f"\n[FATAL] 异常: {type(exc).__name__}: {exc}")
            for t in applied["pids"]:
                restore_isolation(t, applied["orig"].get(t), topology, args.dry_run, log)
            clear_session()
            if out_f:
                out_f.close()
            return 2
    finally:
        if out_f:
            out_f.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
