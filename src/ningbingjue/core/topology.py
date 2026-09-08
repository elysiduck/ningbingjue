# -*- coding: utf-8 -*-
# =====================================================================
#  core/topology.py  --  拓扑枚举 + 最热核识别 + 热核判定（迁移自 test-launch）
# =====================================================================
#  本模块是 "单核喂频 / 兄弟隔离" 的核心计算层，被 SMT 功能卡片（blocks/smt.py）
#  与调试记录器（core/smt_report.py）共用。
#
#  它只做"读取与识别 + 纯计算"，不修改任何 CPU 亲和 / CPU 集合。真正的施加
#  在 platform/cpuset.py。
#
#  依赖：Windows + 标准库 + ctypes；per-core 占用复用 core/perf_probe.py 的
#        CORE_UTIL_PS（start_core_monitor）。
# =====================================================================
from __future__ import annotations

import ctypes
import json
import os
import struct


from .perf_probe import start_core_monitor
from . import game_detect


# --------------------------------------------------------------------------
# 阈值配置：默认内置 + 外部 json 覆盖（便于自行微调，无需改代码）
# --------------------------------------------------------------------------
# 默认配置文件：test-launch/p1_config.json（与旧 P1 脚本同目录，可选）。
_SCRIPT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_CONFIG_PATH = os.path.join(_SCRIPT_ROOT, "test-launch", "p1_config.json")

DEFAULT_CONFIG = {
    "saturated_thr": 80.0,   # 热核饱和阈值(%)，低于此视为未饱和 -> SKIP
    "min_standout": 30.0,    # 离群下限(pp)：热核-簇内其余核均值 必须>=此值才算单核孤峰
    "k_sigma": 2.0,          # 备选 2σ 离群系数（仅报告显示）
    "active_floor": 5.0,     # 其余核占用>=此值才算"激活核"，参与均值/标准差
    "cluster_scope": True,   # 按 EfficiencyClass 簇作用域比较（锁单簇运行时排除 parked 簇）
}


def load_config(path):
    """加载阈值配置。文件缺失/损坏/缺字段均回退内置默认值，绝不崩溃。

    返回 (cfg_dict, source_str)。source_str 用于报告里告知配置来源。
    json 中以 '_' 开头的键视为说明字段，自动忽略。
    """
    cfg = dict(DEFAULT_CONFIG)
    source = "内置默认"
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                for k in DEFAULT_CONFIG:
                    v = user.get(k)
                    if isinstance(v, (int, float)):
                        cfg[k] = float(v)
                source = "文件 " + os.path.basename(path)
        except Exception as e:
            source = f"内置默认(读取{os.path.basename(path)}失败: {e})"
    return cfg, source


def find_foreground_game(whitelist=None):
    """桥接 core/game_detect.find_foreground_game，返回 {pid,name} 或 None。

    game_detect.find_foreground_game 返回 (pid, name) 元组或 None。
    """
    res = game_detect.find_foreground_game(whitelist)
    if not res:
        return None
    pid, name = res
    return {"pid": pid, "name": name}


# --------------------------------------------------------------------------
# 拓扑枚举：GetLogicalProcessorInformationEx(RelationProcessorCore = 0)
# --------------------------------------------------------------------------
LTP_PC_SMT = 0x02          # PROCESSOR_RELATIONSHIP.Flags 的 SMT 位
RELATION_PROCESSOR_CORE = 0  # LOGICAL_PROCESSOR_RELATIONSHIP 枚举值


def _get_logical_processor_info_ex(rel_type: int):
    """返回原始 bytes 缓冲区；失败返回 None。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = kernel32.GetLogicalProcessorInformationEx
    fn.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    fn.restype = ctypes.c_int  # BOOL

    length = ctypes.c_uint32(0)
    # 第一次调用：传 NULL 拿所需长度（返回 FALSE，last error = 203 缓冲区不足）
    fn(rel_type, None, ctypes.byref(length))
    if length.value == 0:
        return None
    buf = ctypes.create_string_buffer(length.value)
    if fn(rel_type, buf, ctypes.byref(length)) == 0:
        return None
    return buf.raw


def _parse_core_masks(raw: bytes):
    """解析 RelationProcessorCore 条目，返回 list of (flags, eff_class, [lp_indices])。

    结构（Win11 现代布局，Reserved[20]）：
      +0  DWORD RelationshipType
      +4  DWORD Size
      +8  BYTE  Flags              (PROCESSOR_RELATIONSHIP)
      +9  BYTE  EfficiencyClass
      +10 BYTE  Reserved[20]
      +30 WORD  GroupMaskCount
      +32 GROUP_AFFINITY GroupMask[GroupMaskCount]   (每组 16 字节: Mask:Q Group:H Reserved[3]:6)

    旧布局（Reserved[2]）回退：GroupMaskCount@12, GroupMask@16。
    我们用"GroupMaskCount 合法性 + 不越界"来验证布局，自动回退。
    """
    cores = []
    ptr = 0
    n = len(raw)
    while ptr + 8 <= n:
        rel_type = struct.unpack_from("<I", raw, ptr)[0]
        size = struct.unpack_from("<I", raw, ptr + 4)[0]
        if size == 0:
            break
        if rel_type == RELATION_PROCESSOR_CORE:
            flags = raw[ptr + 8]
            eff = raw[ptr + 9]
            # —— 现代布局：GroupMaskCount@30, 首个 GroupMask@32 ——
            gmc = struct.unpack_from("<H", raw, ptr + 30)[0]
            base = ptr + 32
            ok = (1 <= gmc <= 4) and (base + gmc * 16 <= min(ptr + size, n))
            if not ok:
                # —— 回退旧布局：GroupMaskCount@12, 首个 GroupMask@16 ——
                gmc = struct.unpack_from("<H", raw, ptr + 12)[0]
                base = ptr + 16
                ok = (1 <= gmc <= 4) and (base + gmc * 16 <= min(ptr + size, n))
            masks = []
            if ok:
                for i in range(gmc):
                    mask, _grp = struct.unpack_from("<QH", raw, base + i * 16)
                    masks.append(mask)
            # 汇总所有 mask 的位 -> 逻辑处理器索引
            lps = []
            for m in masks:
                for bit in range(64):
                    if m & (1 << bit):
                        lps.append(bit)
            cores.append((flags, eff, lps))
        ptr += size
    return cores


def enumerate_topology():
    """返回拓扑模型 dict，失败返回 None。"""
    raw = _get_logical_processor_info_ex(RELATION_PROCESSOR_CORE)
    if raw is None:
        return None
    parsed = _parse_core_masks(raw)
    if not parsed:
        return None

    phys_cores = []
    # 兄弟判定：以"同物理核的 LP 数 > 1"为准（比依赖 Flags 的 SMT 位更稳，
    # 某些 VM/老系统不置 LTP_PC_SMT 位，但 mask 已说明有兄弟）。
    lp_siblings = {}        # lp_index -> set of other lp indices in same physical core
    lp_core = {}            # lp_index -> 物理核序号
    for ci, (flags, eff, lps) in enumerate(parsed):
        phys_cores.append({
            "index": ci,
            "flags": flags,
            "smt": len(lps) > 1,
            "eff": eff,
            "lps": lps,
        })
        for lp in lps:
            lp_core[lp] = ci
        for lp in lps:
            lp_siblings[lp] = set(x for x in lps if x != lp)

    # 大小核分类（按 EfficiencyClass：最小值=性能核，最大值=能效核）
    effs = sorted({c["eff"] for c in phys_cores})
    if len(effs) <= 1:
        for c in phys_cores:
            c["kind"] = "unknown"
    else:
        lo, hi = effs[0], effs[-1]
        for c in phys_cores:
            if c["eff"] == lo:
                c["kind"] = "big"     # 性能核（Strix Point = Zen5）
            elif c["eff"] == hi:
                c["kind"] = "small"   # 能效核（Strix Point = Zen5c）
            else:
                c["kind"] = f"mid{int(c['eff'])}"

    total_lp = sum(len(c["lps"]) for c in phys_cores)
    return {
        "phys_cores": phys_cores,
        "lp_siblings": lp_siblings,
        "lp_core": lp_core,
        "total_lp": total_lp,
        "eff_classes": effs,
    }


def _eff_of_lp(topology, lp):
    """返回某逻辑处理器所属物理核的 EfficiencyClass（未知/找不到返回 None）。"""
    ci = topology["lp_core"].get(lp)
    if ci is None:
        return None
    for c in topology["phys_cores"]:
        if c["index"] == ci:
            return c["eff"]
    return None


# --------------------------------------------------------------------------
# per-core 占用采样：复用现有 CORE_UTIL_PS（\Processor(*)\% Processor Time）
# --------------------------------------------------------------------------
def sample_core_util(perf_probe, n: int = 3):
    """采样 n 次 per-core 占用，返回 {lp_index: 平均占用%}；失败返回 (None, err)。"""
    state, proc = start_core_monitor(True)
    if proc is None:
        return None, (state.get("err") if isinstance(state, dict) else None) or "core monitor start failed"
    samples = []
    try:
        for _ in range(n):
            line = proc.stdout.readline()
            line = line.strip()
            if not line:
                continue
            d = {}
            for tok in line.split("|"):
                if ":" in tok:
                    idx, _, val = tok.partition(":")
                    try:
                        d[int(idx)] = float(val)
                    except ValueError:
                        pass
            if d:
                samples.append(d)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
    if not samples:
        return None, "no core samples returned (Get-Counter unavailable?)"
    avg = {}
    for d in samples:
        for k, v in d.items():
            avg[k] = avg.get(k, 0.0) + v
    for k in avg:
        avg[k] /= len(samples)
    return avg, None


# --------------------------------------------------------------------------
# 热核判定（纯计算，无副作用）—— P1 报告与 SMT 施加共用
# --------------------------------------------------------------------------
def evaluate_hotspot(topology, avg_util, cfg=None, cfg_source="内置默认"):
    """只读计算热核 + 兄弟隔离判定。返回结构化 dict，供 build_report / SMT 复用。

    返回 None 表示无法计算（无占用数据）。否则含：
      hot_lp/hot_val/hot_ci/hot_kind/sibs  —— 识别结果
      exclude_lps                          —— 应排除的 LP 集合（施加用）
      is_bottleneck                        —— APPLY / SKIP 主判据
      其余统计字段（均值/标准差/standout/阈值）供报告展示
    """
    phys = topology["phys_cores"]
    if not avg_util:
        return None
    ranked_all = sorted(avg_util.items(), key=lambda kv: kv[1], reverse=True)
    hot_lp = ranked_all[0][0]
    hot_val = avg_util[hot_lp]
    hot_ci = topology["lp_core"].get(hot_lp, -1)
    hot_kind = next((c["kind"] for c in phys if c["index"] == hot_ci), "?")
    sibs = topology["lp_siblings"].get(hot_lp, set())

    cfg = cfg or DEFAULT_CONFIG
    SATURATED_THR = cfg["saturated_thr"]
    MIN_STANDOUT = cfg["min_standout"]
    K_SIGMA = cfg["k_sigma"]
    ACTIVE_FLOOR = cfg["active_floor"]
    CLUSTER_SCOPE = bool(cfg.get("cluster_scope", True))

    hot_eff = _eff_of_lp(topology, hot_lp)
    do_cluster = CLUSTER_SCOPE and len(topology["eff_classes"]) > 1 and hot_eff is not None

    if do_cluster:
        others = [v for k, v in avg_util.items()
                  if k != hot_lp and _eff_of_lp(topology, k) == hot_eff]
        opp_vals = [v for k, v in avg_util.items()
                    if _eff_of_lp(topology, k) != hot_eff]
        opp_sum = sum(opp_vals)
        opp_max = max(opp_vals) if opp_vals else 0.0
        cluster_label = hot_kind
        other_label = "大核" if hot_kind == "small" else "小核"
        parked_note = (f"  另一簇({other_label})"
                       f"占用 峰值{opp_max:.1f}% 合计{opp_sum:.1f}%"
                       + (" -> 已 parked/空闲，已排除出比较" if opp_max < ACTIVE_FLOOR
                          else " -> 有活动，但仍只与热核同簇比较"))
    else:
        others = [v for k, v in avg_util.items() if k != hot_lp]
        opp_sum = opp_max = 0.0
        cluster_label = None
        parked_note = None

    if not others:
        mean_others = 0.0
        std_others = 0.0
    else:
        active_others = [v for v in others if v >= ACTIVE_FLOOR]
        base = active_others if active_others else others
        mean_others = sum(base) / len(base)
        var_others = sum((v - mean_others) ** 2 for v in base) / len(base)
        std_others = var_others ** 0.5
    standout = hot_val - mean_others          # 热核超出"簇内其余核均值"的量
    sigma_thr = mean_others + K_SIGMA * std_others
    is_outlier = standout >= MIN_STANDOUT      # 主判据：明显离群 => "只有这一颗在烧"
    is_sigma_outlier = hot_val >= sigma_thr    # 备选：2σ 离群
    # 主判据：饱和 + 相对其余激活核明显离群（=> 单核孤峰且高）
    is_bottleneck = (hot_val >= SATURATED_THR) and is_outlier

    return {
        "hot_lp": hot_lp,
        "hot_val": hot_val,
        "hot_ci": hot_ci,
        "hot_kind": hot_kind,
        "sibs": sibs,
        "exclude_lps": set(sibs),             # 应排除的 LP 集合（施加用）
        "do_cluster": do_cluster,
        "cluster_label": cluster_label,
        "parked_note": parked_note,
        "mean_others": mean_others,
        "std_others": std_others,
        "standout": standout,
        "sigma_thr": sigma_thr,
        "is_outlier": is_outlier,
        "is_sigma_outlier": is_sigma_outlier,
        "is_bottleneck": is_bottleneck,
        "SATURATED_THR": SATURATED_THR,
        "MIN_STANDOUT": MIN_STANDOUT,
        "ACTIVE_FLOOR": ACTIVE_FLOOR,
        "K_SIGMA": K_SIGMA,
        "cfg_source": cfg_source,
    }


# --------------------------------------------------------------------------
# 报告（文本；HTML 版见 core/smt_report.py）
# --------------------------------------------------------------------------
def build_report(topology, avg_util, cfg=None, cfg_source="内置默认"):
    lines = []
    phys = topology["phys_cores"]
    lines.append("=" * 64)
    lines.append(" P1 只读验证报告  (单核喂频 / 兄弟隔离)")
    lines.append("=" * 64)
    lines.append(f" 物理核数: {len(phys)}    逻辑处理器数: {topology['total_lp']}")
    lines.append(f" EfficiencyClass 取值: {topology['eff_classes']}")
    if len(topology['eff_classes']) <= 1:
        lines.append("  [注意] 仅一个 EfficiencyClass：可能运行在 VM / 老系统，")
        lines.append("          无法区分大小核。请在真实 HX370 + Win11 上确认。")
    lines.append("")

    lines.append("-" * 64)
    lines.append(" 物理核 / 逻辑处理器 / 大小核 / SMT 兄弟 映射")
    lines.append("-" * 64)
    for c in phys:
        kind = {"big": "大核(Zen5)", "small": "小核(Zen5c)",
                "unknown": "?未知"}.get(c["kind"], c["kind"])
        lps = c["lps"]
        sib = ""
        if len(lps) > 1:
            sib = "  SMT兄弟对=" + "<->".join(f"LP{x}" for x in lps)
        lines.append(
            f"  核#{c['index']:>2}  EffCls={c['eff']}  Flags={c['flags']:#04x}  {kind:<10}"
            f"  LP={lps}{sib if sib else '  无SMT'}"
        )

    if avg_util:
        lines.append("")
        lines.append("-" * 64)
        lines.append(" 当前 per-core 占用（复用 CORE_UTIL_PS，取平均，Top 10）")
        lines.append("-" * 64)
        ranked = sorted(avg_util.items(), key=lambda kv: kv[1], reverse=True)[:10]
        for lp, val in ranked:
            ci = topology["lp_core"].get(lp, -1)
            sibs = topology["lp_siblings"].get(lp, set())
            sib_txt = "  兄弟=" + " ".join(f"LP{x}" for x in sorted(sibs)) if sibs else "  无兄弟"
            kind = next((c["kind"] for c in phys if c["index"] == ci), "?")
            lines.append(f"  LP{lp:>2}  {val:6.1f}%   核#{ci:<2}  {kind:<10}{sib_txt}")

        ev = evaluate_hotspot(topology, avg_util, cfg, cfg_source)
        hot_lp = ev["hot_lp"]
        hot_val = ev["hot_val"]
        hot_ci = ev["hot_ci"]
        hot_kind = ev["hot_kind"]
        sibs = ev["sibs"]

        lines.append("")
        lines.append("=" * 64)
        lines.append(" 识别结论（仅报告，未施加）")
        lines.append("=" * 64)
        lines.append(f"  最热逻辑核: LP{hot_lp}  ({hot_val:.1f}%)")
        lines.append(f"    所属物理核: #{hot_ci}   类型: {hot_kind}")
        if ev["do_cluster"]:
            lines.append(f"  比较作用域: {ev['cluster_label']} 簇内（已排除另一 parked 簇）")
            if ev["parked_note"]:
                lines.append(ev["parked_note"])
        else:
            lines.append("  比较作用域: 全局（未区分大小核，无法做簇作用域）")
        scope_word = "簇内其余" if ev["do_cluster"] else "其余"
        lines.append(f"  {scope_word}激活核均值: {ev['mean_others']:.1f}%   标准差: {ev['std_others']:.1f}%")
        lines.append(f"  热核超出{scope_word}均值: {ev['standout']:.1f} 个百分点   (离群下限={ev['MIN_STANDOUT']:.0f}pp)")
        lines.append(f"  2σ 离群阈值: {ev['sigma_thr']:.1f}%   是否 2σ 离群: {'是' if ev['is_sigma_outlier'] else '否'}")
        if ev["is_bottleneck"]:
            verdict = "APPLY（存在单线程瓶颈，建议施加兄弟隔离）"
        else:
            verdict = "SKIP（负载均衡/未饱和，施加只会减损多线程余量，不建议）"
        lines.append(f"  判定闸门: 饱和阈值={ev['SATURATED_THR']:.0f}%  离群下限={ev['MIN_STANDOUT']:.0f}pp"
                     f"  激活下限={ev['ACTIVE_FLOOR']:.0f}%  2σ系数={ev['K_SIGMA']:.1f}"
                     f"  簇作用域={'是' if ev['do_cluster'] else '否'}")
        lines.append(f"  配置来源: {ev['cfg_source']}")
        lines.append(f"  -> 结论建议: {verdict}")
        if sibs:
            sib_list = ", ".join(f"LP{x}" for x in sorted(sibs))
            if ev["is_bottleneck"]:
                lines.append(f"    其 SMT 兄弟: {sib_list}")
                lines.append(f"    -> 施加方式：把游戏进程亲和排除 {sib_list}")
                lines.append(f"       （核#{hot_ci} 上只剩游戏线程独享整物理核）")
            else:
                lines.append(f"    其 SMT 兄弟: {sib_list}（当前判定为 SKIP，暂不施加）")
        else:
            lines.append("    该核无 SMT 兄弟，无需隔离（已是独占物理核）")
        lines.append("")
        lines.append("  提示：请在前台运行游戏时再跑本脚本，最热核即为主线程瓶颈核。")
    else:
        lines.append("")
        lines.append("  [警告] 未能获取 per-core 占用（Get-Counter 不可用）。")
        lines.append("          拓扑识别仍有效；占用识别请检查 PowerShell Get-Counter。")
    return "\n".join(lines)