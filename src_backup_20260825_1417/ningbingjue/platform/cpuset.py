# -*- coding: utf-8 -*-
# =====================================================================
#  platform/cpuset.py  --  SMT 热核隔离施加层（Windows CPU Sets / 亲和回退）
# =====================================================================
#  本模块把 topology.evaluate_hotspot 算出的"应排除哪些逻辑核"真正落到系统上。
#
#  隔离语义（来自功能卡片需求）：
#    * 游戏进程  ：CPU 集合 = 全核 减去 热核的"兄弟线程"（保留热核主线程，
#                   让游戏主线程独享整物理核 -> 喂频）。
#    * 其他进程  ：CPU 集合 = 全核 减去 整个热物理核（主线程+兄弟都排除），
#                   把热核彻底让给游戏。
#    * 复原      ：所有进程 CPU 集合恢复为全核。
#
#  机制：优先用 Windows CPU Sets API（GetSystemCpuSetInformation /
#        SetProcessDefaultCpuSets / SetThreadSelectedCpuSets），这是用户要求的
#        "CPU 集合"。若系统不支持（返回空 / 老版本 Windows），自动回退到
#        SetProcessAffinityMask（硬亲和，同样实现"排除某核/某兄弟"语义）。
#
#  安全：对系统关键进程、自身进程、打不开的进程一律失败跳过，绝不抛异常上抛。
#        所有 apply 都是 best-effort。
# =====================================================================
from __future__ import annotations

import ctypes
import os
import sys
import threading

try:
    import _winreg  # noqa: F401  (pywin32 not required; we use ctypes for paths)
except Exception:  # pragma: no cover
    _winreg = None


# --------------------------------------------------------------------------
# 系统关键进程名（绝不允许改其 CPU 集合，避免桌面/系统崩溃）
# --------------------------------------------------------------------------
_PROTECTED_NAMES = {
    "system", "idle", "system idle process",
    "smss.exe", "csrss.exe", "wininit.exe", "services.exe", "lsass.exe",
    "registry", "fontdrvhost.exe", "dwm.exe", "winlogon.exe",
    "svchost.exe",  # 过于核心，整体排除更安全（不逐一区分）
}


def _is_protected(name: str | None) -> bool:
    n = (name or "").lower()
    if not n:
        return False
    return n in _PROTECTED_NAMES


# --------------------------------------------------------------------------
# ctypes 绑定（惰性）
# --------------------------------------------------------------------------
_kernel32 = None
_lock = threading.Lock()


def _k32():
    global _kernel32
    if _kernel32 is None:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return _kernel32


PROCESS_SET_INFORMATION = 0x0200
PROCESS_SET_LIMITED_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
THREAD_SET_LIMITED_INFORMATION = 0x0400


def _open_proc(pid: int, access: int):
    h = _k32().OpenProcess(access, False, pid)
    if not h:
        raise OSError(f"OpenProcess({pid}) 失败 WinErr={ctypes.get_last_error()}")
    return h


# --------------------------------------------------------------------------
# CPU Sets 支持探测 + LP->CpuSetId 映射
# --------------------------------------------------------------------------
_cached_supported = None
_cached_map = None  # (lp_to_setid: dict[int,int], all_setids: list[int])


def _parse_cpu_set_info(raw: bytes):
    """解析 GetSystemCpuSetInformation 缓冲，返回 (lp_to_setid, all_setids)。"""
    lp_to_setid: dict[int, int] = {}
    all_setids: list[int] = []
    ptr = 0
    n = len(raw)
    while ptr + 8 <= n:
        size = ctypes.c_uint32.from_buffer_copy(raw, ptr + 0).value
        typ = ctypes.c_uint32.from_buffer_copy(raw, ptr + 4).value
        if size == 0:
            break
        if typ == 0:  # CpuSetInformation
            # offset 8: CpuSetId (ULONG)
            cpu_set_id = ctypes.c_uint32.from_buffer_copy(raw, ptr + 8).value
            # offset 14: Group (USHORT); offset 16: Number (UCHAR, LP index)
            group = ctypes.c_uint16.from_buffer_copy(raw, ptr + 14).value
            number = raw[ptr + 16]
            lp_to_setid[number] = cpu_set_id
            all_setids.append(cpu_set_id)
        ptr += size
    return lp_to_setid, all_setids


def _query_cpu_sets():
    """返回 (lp_to_setid, all_setids) 或 None（不支持）。"""
    global _cached_map
    if _cached_map is not None:
        return _cached_map
    try:
        fn = _k32().GetSystemCpuSetInformation
        fn.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        fn.restype = ctypes.c_int
        length = ctypes.c_uint32(0)
        fn(None, 0, ctypes.byref(length))
        if length.value == 0:
            _cached_map = None
            return None
        buf = ctypes.create_string_buffer(length.value)
        if fn(buf, length.value, ctypes.byref(length)) == 0:
            _cached_map = None
            return None
        _cached_map = _parse_cpu_set_info(buf.raw)
        return _cached_map
    except Exception:
        _cached_map = None
        return None


def cpu_sets_supported() -> bool:
    """是否支持 Windows CPU Sets API（GetSystemCpuSetInformation 返回非空）。"""
    global _cached_supported
    if _cached_supported is None:
        m = _query_cpu_sets()
        _cached_supported = bool(m and m[1])
    return _cached_supported


def get_lp_to_setid():
    """返回 (lp_to_setid, all_setids)；不支持时返回 (None, None)。"""
    return _query_cpu_sets()


# --------------------------------------------------------------------------
# 亲和回退的掩码读写
# --------------------------------------------------------------------------
def get_affinity(pid):
    """返回 (proc_mask, sys_mask)；失败抛 OSError。"""
    h = _open_proc(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    try:
        pm = ctypes.c_ulonglong(0)
        sm = ctypes.c_ulonglong(0)
        ok = _k32().GetProcessAffinityMask(h, ctypes.byref(pm), ctypes.byref(sm))
        if not ok:
            raise OSError(f"GetProcessAffinityMask({pid}) 失败 WinErr={ctypes.get_last_error()}")
        return pm.value, sm.value
    finally:
        _k32().CloseHandle(h)


def set_affinity(pid, mask):
    h = _open_proc(pid, PROCESS_SET_INFORMATION)
    try:
        ok = _k32().SetProcessAffinityMask(h, ctypes.c_ulonglong(mask))
        if not ok:
            raise OSError(f"SetProcessAffinityMask({pid}) 失败 WinErr={ctypes.get_last_error()}")
    finally:
        _k32().CloseHandle(h)


# --------------------------------------------------------------------------
# CPU Sets 施加（进程级默认集合 + 游戏线程级 selected 集合）
# --------------------------------------------------------------------------
def _set_process_cpuset(pid, setids):
    """设置进程默认 CPU 集合；setids 为空 -> 复位为全核（系统默认）。"""
    h = _open_proc(pid, PROCESS_SET_LIMITED_INFORMATION)
    try:
        if not setids:
            arr = (ctypes.c_ulong * 0)()
            cnt = 0
        else:
            arr = (ctypes.c_ulong * len(setids))(*setids)
            cnt = len(setids)
        ok = _k32().SetProcessDefaultCpuSets(h, arr, cnt)
        if not ok:
            raise OSError(f"SetProcessDefaultCpuSets({pid}) 失败 WinErr={ctypes.get_last_error()}")
    finally:
        _k32().CloseHandle(h)


def _set_thread_cpuset(tid, setids):
    """设置单个线程的 selected CPU 集合；setids 为空 -> 复位（跟随进程默认）。"""
    # OpenThread
    ht = _k32().OpenThread(THREAD_SET_LIMITED_INFORMATION, False, tid)
    if not ht:
        return False
    try:
        if not setids:
            arr = (ctypes.c_ulong * 0)()
            cnt = 0
        else:
            arr = (ctypes.c_ulong * len(setids))(*setids)
            cnt = len(setids)
        ok = _k32().SetThreadSelectedCpuSets(ht, arr, cnt)
        return bool(ok)
    finally:
        _k32().CloseHandle(ht)


def _enum_thread_ids(pid):
    """枚举某进程的所有线程 ID（best-effort，失败返回 []）。"""
    try:
        thr = ctypes.windll.kernel32 if hasattr(ctypes, "windll") else _k32()
        CreateToolhelp32Snapshot = thr.CreateToolhelp32Snapshot
        CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        Thread32First = thr.Thread32First
        Thread32Next = thr.Thread32Next
        THREADENTRY32 = ctypes.c_uint32 * 12  # 足够容纳前几个字段
        snap = CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if not snap or snap == ctypes.c_void_p(-1).value:
            return []
        ids = []
        # 简化的 THREADENTRY32：dwSize@0, cntUsage@4, th32ThreadID@8,
        # th32OwnerProcessID@12, ...
        entry = ctypes.create_string_buffer(28)
        ctypes.c_uint32.from_buffer_copy(
            ctypes.c_uint32(28).to_bytes(4, "little")).value  # placeholder
        import struct as _struct
        ctypes.memmove(entry, (28).to_bytes(4, "little"), 4)
        if Thread32First(snap, entry):
            while True:
                tid = _struct.unpack_from("<I", entry, 8)[0]
                owner = _struct.unpack_from("<I", entry, 12)[0]
                if owner == pid:
                    ids.append(tid)
                if not Thread32Next(snap, entry):
                    break
        thr.CloseHandle(snap)
        return ids
    except Exception:
        return []


# --------------------------------------------------------------------------
# 排除集合计算
# --------------------------------------------------------------------------
def _excluded_setids(excluded_lps, lp_to_setid, all_setids):
    """把"应排除的 LP 集合"映射为 (allowed_setids, excluded_setids)。"""
    ex = set()
    for lp in excluded_lps:
        sid = lp_to_setid.get(lp)
        if sid is not None:
            ex.add(sid)
    allowed = [s for s in all_setids if s not in ex]
    return allowed, sorted(ex)


def _excluded_mask(excluded_lps, sys_mask):
    """把"应排除的 LP 集合"映射为 new_mask（亲和回退）。"""
    mask = sys_mask
    for lp in excluded_lps:
        mask &= ~(1 << lp)
    if mask == 0:  # 不能把所有位都清掉（进程会饿死）
        mask = sys_mask
    return mask


# --------------------------------------------------------------------------
# 单进程施加 / 复原
# --------------------------------------------------------------------------
def exclude_for_process(pid, name, excluded_lps, dry_run, log,
                        lp_to_setid=None, all_setids=None, pin_threads=False):
    """对单进程排除指定 LP（热核或兄弟）。返回 True 表示已施加/本应施加。"""
    if pid == os.getpid():
        return False  # 不打扰自身
    if _is_protected(name):
        if not dry_run:
            log(f"  [skip] 受保护进程 {name}({pid}) 跳过")
        return False
    try:
        if cpu_sets_supported() and lp_to_setid is not None:
            allowed, ex = _excluded_setids(excluded_lps, lp_to_setid, all_setids)
            if dry_run:
                log(f"  [DRY-RUN] {name}({pid}) 排除集合={ex} -> 允许={allowed}")
                return True
            _set_process_cpuset(pid, allowed)
            if pin_threads:
                for tid in _enum_thread_ids(pid):
                    _set_thread_cpuset(tid, allowed)
            log(f"  [APPLY] {name}({pid}) 排除热核集合={ex}")
        else:
            pm, sm = get_affinity(pid)
            new_mask = _excluded_mask(excluded_lps, sm)
            if new_mask == pm:
                return False
            if dry_run:
                log(f"  [DRY-RUN] {name}({pid}) 亲和掩码 {pm:#x} -> {new_mask:#x} "
                    f"(排除 LP={sorted(excluded_lps)})")
                return True
            set_affinity(pid, new_mask)
            log(f"  [APPLY] {name}({pid}) 亲和掩码 {pm:#x} -> {new_mask:#x} "
                f"(排除 LP={sorted(excluded_lps)})")
        return True
    except OSError as exc:
        log(f"  [warn] {name}({pid}) 设置失败（可能权限不足/已退出）: {exc}")
        return False


def restore_process(pid, name, dry_run, log):
    """复原单进程为全核。"""
    if pid == os.getpid():
        return False
    if _is_protected(name):
        return False
    try:
        if cpu_sets_supported():
            if dry_run:
                log(f"  [DRY-RUN] 复原 {name}({pid}) -> 全核")
                return True
            _set_process_cpuset(pid, [])  # 复位为系统默认（全核）
            log(f"  [RESTORE] 复原 {name}({pid}) -> 全核")
        else:
            pm, sm = get_affinity(pid)
            if pm == sm:
                return False
            if dry_run:
                log(f"  [DRY-RUN] 复原 {name}({pid}) 亲和 {pm:#x} -> {sm:#x}")
                return True
            set_affinity(pid, sm)
            log(f"  [RESTORE] 复原 {name}({pid}) 亲和 {pm:#x} -> {sm:#x}")
        return True
    except OSError as exc:
        log(f"  [warn] 复原 {name}({pid}) 失败: {exc}")
        return False


# --------------------------------------------------------------------------
# 顶层：SMT 隔离施加 / 全复原
# --------------------------------------------------------------------------
def apply_smt_isolation(game_pid, hot_lp, topology, processes, dry_run, log):
    """对全部进程施加 SMT 隔离。

    game_pid : 前台游戏 PID（None 表示无游戏，则所有进程都按"非游戏"排除热核）
    hot_lp   : 最热逻辑核
    topology : enumerate_topology() 的结果
    processes: list of (pid, name)  —— 通常由 process_manager.enumerate() 产出

    返回 dict 记录（供 HTML 报表）。
    """
    if topology is None or hot_lp is None:
        log("[apply] 缺少拓扑/热核，跳过")
        return {"applied": 0, "skipped": 0, "game_pid": game_pid}

    lp_to_setid, all_setids = (None, None)
    if cpu_sets_supported():
        lp_to_setid, all_setids = get_lp_to_setid() or (None, None)

    hot_ci = topology["lp_core"].get(hot_lp, -1)
    hot_core_lps = set()
    for c in topology["phys_cores"]:
        if c["index"] == hot_ci:
            hot_core_lps = set(c["lps"])
            break
    game_excluded = set(topology["lp_siblings"].get(hot_lp, set()))  # 仅兄弟
    nongame_excluded = hot_core_lps  # 整个热物理核

    mechanism = "CPU集合" if (lp_to_setid is not None) else "亲和掩码"
    log(f"[apply] 机制={mechanism}  热核=LP{hot_lp}(物理核#{hot_ci})  "
        f"游戏排除兄弟={sorted(game_excluded)}  其他排除整核={sorted(hot_core_lps)}")

    applied = 0
    skipped = 0
    for pid, name in processes:
        if game_pid is not None and pid == game_pid:
            ok = exclude_for_process(pid, name, game_excluded, dry_run, log,
                                     lp_to_setid, all_setids, pin_threads=True)
        else:
            ok = exclude_for_process(pid, name, nongame_excluded, dry_run, log,
                                     lp_to_setid, all_setids, pin_threads=False)
        if ok:
            applied += 1
        else:
            skipped += 1
    log(f"[apply] 完成：施加 {applied} 个进程，跳过 {skipped} 个")
    return {
        "applied": applied, "skipped": skipped,
        "game_pid": game_pid, "mechanism": mechanism,
        "hot_lp": hot_lp, "hot_core": hot_ci,
        "game_excluded": sorted(game_excluded),
        "nongame_excluded": sorted(hot_core_lps),
    }


def restore_all(processes, dry_run, log):
    """把所有进程复原为全核。"""
    restored = 0
    for pid, name in processes:
        if restore_process(pid, name, dry_run, log):
            restored += 1
    log(f"[restore] 完成：复原 {restored} 个进程")
    return {"restored": restored}
