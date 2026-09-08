# -*- coding: utf-8 -*-
# =====================================================================
#  blocks/smt.py  --  SMT 热核隔离区块（业务逻辑层）
# =====================================================================
#  功能卡片「SMT 热核隔离」的业务逻辑。把拓扑识别（core/topology.py）与
#  CPU 集合施加（platform/cpuset.py）粘合起来，提供三种操作：
#
#    SMT自动  ：开启热核检测 + 持续自动施加 CPU 集合（后台 RepeatingTimer，
#               带 2 轮滞回，避免抖动）；瓶颈消失后自动复原全核。
#    SMT锁定  ：每次执行前先把所有进程 CPU 集合复原为全核，再 3s 采样一次，
#               算出最热核 **强制** 施加一次（不持续检测，不 gate is_bottleneck
#               阈值——锁定 = 用户主动要求，无论是否达到瓶颈都施加）。
#    SMT复原  ：所有进程 CPU 集合绑定为全核。
#    调试记录  ：模拟一次热核监测会话 -> 生成自包含 HTML 报表（编辑项按钮调用）。
#
#  隔离语义（见 platform/cpuset.apply_smt_isolation）：
#    * 游戏进程  ：CPU 集合排除热核的 SMT 兄弟（保留热核主线程独占整物理核）。
#    * 其他进程  ：CPU 集合排除整个热物理核（把热核彻底让给游戏）。
# =====================================================================
from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Any, Callable

from ..core import topology
from ..platform import cpuset
from ..core.timers import RepeatingTimer
from .base import BlockBlueprint


# --------------------------------------------------------------------------
# 模块级辅助：纯模拟用的拓扑 / 占用（调试记录不触碰系统）
# --------------------------------------------------------------------------
def _fallback_topology() -> dict:
    """拓扑枚举拿不到时（如非 Win / 测试），构造一个 6 物理核的最小占位
    拓扑：前 4 个为 SMT 大核（每核 2 LP），后 2 个为单 LP 小核。"""
    phys: list[dict] = []
    lp = 0
    for i in range(6):
        if i < 4:
            lps = [lp, lp + 1]
            lp += 2
            smt = True
        else:
            lps = [lp]
            lp += 1
            smt = False
        eff = 0 if i < 4 else 255
        phys.append({
            "index": i, "flags": 0x02 if smt else 0, "smt": smt,
            "eff": eff, "lps": lps,
            "kind": "big" if eff == 0 else "small",
        })
    lp_siblings: dict[int, set] = {}
    lp_core: dict[int, int] = {}
    for c in phys:
        for x in c["lps"]:
            lp_core[x] = c["index"]
        for x in c["lps"]:
            lp_siblings[x] = set(y for y in c["lps"] if y != x)
    return {
        "phys_cores": phys, "lp_siblings": lp_siblings, "lp_core": lp_core,
        "total_lp": lp, "eff_classes": [0, 255],
    }


def _synthetic_util(topo: dict) -> dict[int, float]:
    """造一条「单核孤峰 + SMT 兄弟略高、其余核低压」的模拟占用，使判定为
    APPLY（饱和且离群），用于调试报表可视化。"""
    util: dict[int, float] = {}
    phys = topo.get("phys_cores", [])
    if not phys:
        return {0: 50.0}
    hot_ci = next((c["index"] for c in phys if len(c["lps"]) > 1), phys[0]["index"])
    for c in phys:
        if c["index"] == hot_ci:
            lps = c["lps"]
            util[lps[0]] = 95.0
            if len(lps) > 1:
                util[lps[1]] = 48.0
        else:
            for x in c["lps"]:
                util[x] = 12.0 + (hashlib.md5(str(x).encode()).digest()[0] % 18)
    return util


class SmtBlock:
    """SMT 热核隔离区块实例。

    构造参数与 power / procmon 等真实区块一致：(config, process_manager)。
    config 为 AppConfig（含 smt_config）；process_manager 提供 enumerate()。
    """

    # 默认卡片色（暖橙，区别于 undervolt 的紫、power 的品红）。
    DEFAULT_COLOR = "#ff9f43"

    def __init__(self, config: Any, process_manager: Any,
                 ui: dict[str, Any] | None = None) -> None:
        self.config = config            # AppConfig（smt_config 在其上）
        self.pm = process_manager       # Win32ProcessManager / Mock
        self.ui = ui or {}
        self._lock = threading.RLock()
        self._auto_timer: RepeatingTimer | None = None
        self._auto_running = False
        self._topology: dict | None = None
        self._consecutive_bottleneck = 0
        self._consecutive_clear = 0
        self._last_apply_sig: tuple | None = None
        self._last: dict[str, Any] = {"mode": "idle"}
        self._status_cb: Callable[[], None] | None = None

    # -- 拓扑缓存（拓扑运行时不变，只枚举一次）-------------------------
    def _get_topology(self) -> dict | None:
        if self._topology is None:
            try:
                self._topology = topology.enumerate_topology()
            except Exception:
                self._topology = None
        return self._topology

    # -- 进程枚举 -> (pid, name) ---------------------------------------
    def _enumerate(self) -> list[tuple[int, str]]:
        try:
            procs = self.pm.enumerate()
        except Exception:
            return []
        return [(p.pid, p.name) for p in procs]

    # -- 日志（落到 logs/smt.log，失败静默）---------------------------
    def _log(self, msg: str) -> None:
        try:
            root = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..", ".."))
            p = os.path.join(root, "logs", "smt.log")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(p, "a", encoding="utf-8", errors="ignore") as fh:
                fh.write(f"[{ts}] {msg}\n")
        except Exception:
            pass

    # -- 配置读写（OnSaveConfig / OnLoadConfig 钩子）-------------------
    def save_config(self) -> Any:
        """OnSaveConfig 钩子：config.smt_config 已是 AppConfig 上的同一对象，
        set_auto / 编辑项对该对象的就地修改会由 config_manager.save() 落盘。"""
        sc = getattr(self.config, "smt_config", None)
        return sc

    def load_config(self) -> None:
        """OnLoadConfig 钩子（boot 时每个区块都会被调用）。"""
        sc = getattr(self.config, "smt_config", None)
        if sc is None:
            from ..core.config import SmtConfig
            sc = SmtConfig()
            self.config.smt_config = sc
        self._last = {"mode": "idle", "enabled": bool(sc.enabled)}

    # -- 状态（供面板 sync 读取）--------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            sc = getattr(self.config, "smt_config", None)
            base: dict[str, Any] = {
                "enabled": bool(sc.enabled) if sc else False,
                "auto_running": self._auto_running,
                "saturated_thr": float(sc.saturated_thr) if sc else 80.0,
                "min_standout": float(sc.min_standout) if sc else 30.0,
            }
            base.update(self._last)
            return base

    def set_status_callback(self, cb: Callable[[], None] | None) -> None:
        self._status_cb = cb

    def _notify(self) -> None:
        cb = self._status_cb
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    # -- 实时占用（热核视图柱状图用）----------------------------------
    def current_util(self) -> tuple[dict | None, dict[int, float] | None]:
        """采样一次 per-LP 占用，供实时柱状图使用。返回 (topology, util_dict)。

        util_dict: {lp_index: 占用%}；采样失败返回 (topology, None)。
        该方法从后台采样线程调用，只读 self._topology（一次性赋值，原子读）并
        启动独立 PowerShell 子进程，无共享可变状态，线程安全。
        """
        topo = self._get_topology()
        if topo is None:
            topo = _fallback_topology()
        try:
            util, _err = topology.sample_core_util(None, n=1)
        except Exception as e:  # noqa: BLE001
            self._log(f"[current_util] 异常: {e}")
            util = None
        if util is None:
            return topo, None
        return topo, util

    # -- 阈值配置（来自 smt_config）-----------------------------------
    def _cfg_dict(self) -> dict[str, Any]:
        sc = getattr(self.config, "smt_config", None)
        if sc is None:
            return dict(topology.DEFAULT_CONFIG)
        return {
            "saturated_thr": sc.saturated_thr,
            "min_standout": sc.min_standout,
            "k_sigma": sc.k_sigma,
            "active_floor": sc.active_floor,
            "cluster_scope": sc.cluster_scope,
        }

    # -- 阈值设置（编辑项滑块调用，就地改 + 落盘）---------------------
    def set_threshold(self, name: str, value: float, save: bool = True) -> None:
        sc = getattr(self.config, "smt_config", None)
        if sc is None:
            return
        if hasattr(sc, name):
            setattr(sc, name, float(value))
        if save:
            self._notify()

    def set_enabled(self, on: bool, save: bool = True) -> None:
        """编辑项开关 / 卡片按钮共用：开启=启动持续检测，关闭=停止并复原全核。"""
        self.set_auto(on)
        if save:
            self._notify()

    # -- SMT自动（持续检测 + 自动施加）-------------------------------
    def set_auto(self, on: bool) -> None:
        """开启/关闭持续热核检测自动施加。"""
        with self._lock:
            if on and not self._auto_running:
                self._consecutive_bottleneck = 0
                self._consecutive_clear = 0
                self._last_apply_sig = None
                self._auto_timer = RepeatingTimer(3000, self._auto_tick)
                self._auto_timer.start()
                self._auto_running = True
            elif not on and self._auto_running:
                if self._auto_timer is not None:
                    self._auto_timer.stop()
                self._auto_timer = None
                self._auto_running = False
                # 关闭自动时复原所有进程为全核，避免残留隔离。
                try:
                    self._restore_all(dry_run=False)
                except Exception:
                    pass
        sc = getattr(self.config, "smt_config", None)
        if sc is not None:
            sc.enabled = bool(on)
        with self._lock:
            self._last = {"mode": "auto" if on else "idle",
                          "auto_running": self._auto_running,
                          "ts": time.strftime("%H:%M:%S")}

    def _auto_tick(self) -> None:
        """后台每 3s 检测一次；2 轮滞回：连续 2 次瓶颈才施加，连续 2 次清除才复原。"""
        try:
            topo = self._get_topology()
            if topo is None:
                return
            game = topology.find_foreground_game()
            game_pid = game["pid"] if game else None
            util, err = topology.sample_core_util(None, n=3)
            if util is None:
                return
            ev = topology.evaluate_hotspot(topo, util, self._cfg_dict(), "配置")
            if ev is None:
                return
            with self._lock:
                if ev["is_bottleneck"]:
                    self._consecutive_bottleneck += 1
                    self._consecutive_clear = 0
                else:
                    self._consecutive_clear += 1
                    self._consecutive_bottleneck = 0
                b = self._consecutive_bottleneck
                c = self._consecutive_clear
                self._last = {
                    "mode": "auto", "auto_running": True,
                    "hot_lp": ev["hot_lp"], "hot_val": ev["hot_val"],
                    "is_bottleneck": ev["is_bottleneck"], "game": game,
                    "consecutive_bottleneck": b, "consecutive_clear": c,
                    "ts": time.strftime("%H:%M:%S"),
                }
            if b >= 2:
                procs = self._enumerate()
                sig = (ev["hot_lp"], tuple(sorted(p[0] for p in procs)))
                with self._lock:
                    changed = (sig != self._last_apply_sig)
                    self._last_apply_sig = sig
                if changed:
                    cpuset.apply_smt_isolation(game_pid, ev["hot_lp"], topo,
                                               procs, False, self._log)
            elif c >= 2:
                with self._lock:
                    self._last_apply_sig = None
                self._restore_all(False)
        except Exception as e:  # noqa: BLE001
            self._log(f"[auto-tick] 异常: {e}")
        self._notify()

    # -- SMT锁定（一次性：复原 -> 3s 采样 -> 强制施加一次）------------
    def lock_once(self) -> dict[str, Any]:
        """后台线程执行：先复原所有进程为全核，再 3s 采样一次，算出最热核
        **强制**施加一次（不持续检测）。

        锁定语义 = 用户主动要求施加最热核隔离，**不受 is_bottleneck 阈值 gate**。
        阈值 gate 是自动模式（set_auto）防抖才需要的——避免频繁施加/复原抖动。
        锁定是一次性动作，无论是否达到"单核孤峰"瓶颈阈值都应施加，否则按钮
        在多数非瓶颈场景下形同虚设（用户感知"没有作用"）。
        is_bottleneck 仍记录在 _last 中供 sync 显示区分"瓶颈锁定/强制锁定"。
        """

        def _worker() -> None:
            try:
                # 1) 每次执行前先把所有进程 CPU 集合复原为全核
                self._restore_all(False)
                # 2) 3s 采样（sample_core_util n=3 即多次采样约 3s）
                topo = self._get_topology()
                if topo is None:
                    self._log("[lock] 拓扑获取失败")
                    with self._lock:
                        self._last = {"mode": "lock", "error": "拓扑获取失败",
                                      "ts": time.strftime("%H:%M:%S")}
                    self._notify()
                    return
                game = topology.find_foreground_game()
                game_pid = game["pid"] if game else None
                util, err = topology.sample_core_util(None, n=3)
                if util is None:
                    self._log(f"[lock] 采样失败: {err}")
                    with self._lock:
                        self._last = {"mode": "lock", "error": f"采样失败: {err}",
                                      "ts": time.strftime("%H:%M:%S")}
                    self._notify()
                    return
                ev = topology.evaluate_hotspot(topo, util, self._cfg_dict(), "配置")
                if ev is None:
                    with self._lock:
                        self._last = {"mode": "lock", "error": "热核评估失败",
                                      "ts": time.strftime("%H:%M:%S")}
                    self._notify()
                    return
                procs = self._enumerate()
                # 3) 强制施加最热核隔离（不 gate is_bottleneck）。
                #    is_bottleneck 仅作为状态展示信息，不影响是否施加。
                apply = cpuset.apply_smt_isolation(
                    game_pid, ev["hot_lp"], topo, procs, False, self._log)
                bottleneck = bool(ev.get("is_bottleneck", False))
                self._log(f"[lock] 强制施加完成（{'瓶颈触发' if bottleneck else '强制锁定'}）")
                with self._lock:
                    self._last = {
                        "mode": "lock",
                        "hot_lp": ev["hot_lp"], "hot_val": ev["hot_val"],
                        "is_bottleneck": bottleneck, "game": game,
                        "applied": apply.get("applied", 0) if apply else 0,
                        "ts": time.strftime("%H:%M:%S"),
                    }
            except Exception as e:  # noqa: BLE001
                self._log(f"[lock] 异常: {e}")
                with self._lock:
                    self._last = {"mode": "lock", "error": str(e),
                                  "ts": time.strftime("%H:%M:%S")}
            self._notify()

        threading.Thread(target=_worker, daemon=True).start()
        return {"started": True}

    def on_power_resume(self) -> bool | None:
        """OnPowerResume 钩子：休眠恢复后重新施加 SMT 热核隔离。

        门控：仅当休眠前曾成功锁定过（self._last.mode == "lock"）才重新施加。
        CPU 亲和性在休眠后通常保持（进程没退出），但休眠期间被冻结的进程恢复
        运行后负载分布可能变化，且部分系统会在唤醒时重置亲和性掩码——重新
        lock_once（先复原全核再采样最热核施加）是最稳妥的恢复方式。

        幂等：未锁定过（mode != "lock"）则 no-op。lock_once 本身也是幂等的
        （每次先 _restore_all 复原再施加）。
        """
        with self._lock:
            last_mode = (self._last or {}).get("mode", "")
        if last_mode != "lock":
            return None
        try:
            self.lock_once()
            return True
        except Exception:
            return False

    # -- SMT复原（所有进程绑定全核）-----------------------------------
    def restore_all(self) -> dict[str, Any]:
        """后台线程：把所有进程 CPU 集合复原为全核。"""

        def _worker() -> None:
            try:
                self._restore_all(False)
            except Exception as e:  # noqa: BLE001
                self._log(f"[restore] 异常: {e}")
            self._notify()

        threading.Thread(target=_worker, daemon=True).start()
        return {"started": True}

    def _restore_all(self, dry_run: bool = False) -> dict[str, Any]:
        procs = self._enumerate()
        res = cpuset.restore_all(procs, dry_run, self._log)
        with self._lock:
            self._last = {"mode": "restore", "restored": res.get("restored", 0),
                          "ts": time.strftime("%H:%M:%S")}
        return res

    # -- 调试记录（编辑项「调试/记录」按钮调用）-----------------------
    def debug_simulate(self) -> str:
        """模拟一次热核监测会话，生成自包含 HTML 报表，返回路径（不触碰系统）。"""
        from ..core import smt_report

        topo = self._get_topology()
        if topo is None:
            topo = _fallback_topology()
        cfg = self._cfg_dict()

        # 合成一条趋势：最热核占用从低位爬升，制造离群时间线。
        util = _synthetic_util(topo)
        ev = topology.evaluate_hotspot(topo, util, cfg, "调试模拟")
        hot = ev["hot_lp"] if ev else 0
        samples: list[dict] = []
        for i in range(8):
            u = dict(util)
            u[hot] = min(100.0, u.get(hot, 50.0) + i * 4.0)
            samples.append({"hot_lp": hot, "hot_val": u.get(hot, 0.0), "util": u})
        if ev is None:
            ev = topology.evaluate_hotspot(topo, util, cfg, "调试模拟")

        # 计算「将要施加」的排除集合（纯展示，不真正改系统）。
        game = topology.find_foreground_game()
        apply = None
        if ev is not None:
            hot_ci = topo["lp_core"].get(ev["hot_lp"], -1)
            nongame_excluded: list[int] = []
            for c in topo["phys_cores"]:
                if c["index"] == hot_ci:
                    nongame_excluded = list(c["lps"])
            game_excluded = sorted(topo["lp_siblings"].get(ev["hot_lp"], set()))
            apply = {
                "applied": 0, "skipped": 0,
                "game_pid": game["pid"] if game else None,
                "mechanism": "模拟(未施加)",
                "hot_lp": ev["hot_lp"], "hot_core": hot_ci,
                "game_excluded": game_excluded,
                "nongame_excluded": sorted(nongame_excluded),
            }

        record = {
            "mode": "调试模拟",
            "topology": topo,
            "cfg": cfg,
            "samples": samples,
            "final": ev or {},
            "apply": apply,
            "game": game,
        }
        root = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                            "..", "..", ".."))
        logs = os.path.join(root, "logs")
        os.makedirs(logs, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        out = os.path.join(logs, f"smt_debug_{ts}.html")
        return smt_report.generate_smt_html(record, out_path=out)

    # -- 关闭（进程退出时清理定时器）----------------------------------
    def shutdown(self) -> None:
        with self._lock:
            if self._auto_timer is not None:
                self._auto_timer.stop()
            self._auto_timer = None
            self._auto_running = False

    # -- 注册 ----------------------------------------------------------
    def register(self, registry: Any) -> None:
        bp = BlockBlueprint(
            block_type="smt",
            name="SMT 热核隔离",
            color=self.DEFAULT_COLOR,
            card=self.ui.get("card") if isinstance(self.ui, dict) else None,
            hooks={
                "GetCardHeight": lambda: 230,
                "OnSaveConfig": self.save_config,
                "OnLoadConfig": self.load_config,
                "OnPowerResume": self.on_power_resume,
            },
        )
        registry.register(bp.to_block())
