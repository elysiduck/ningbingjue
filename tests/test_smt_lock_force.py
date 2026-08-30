# -*- coding: utf-8 -*-
"""验证 SMT lock_once 修复：非瓶颈场景下也强制施加最热核隔离（不再 SKIP）。
+ 验证手柄 hint="A" 后 _enter_card 聚焦 lock_btn。
纯逻辑验证：mock topology/cpuset/process_manager，不起后台真采样。
"""
import sys, os, time, threading
from unittest.mock import MagicMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

# ---- 拓扑占位（4 物理核 SMT，前 4 大核每核 2 LP）----
phys = []
lp = 0
for i in range(4):
    phys.append({"index": i, "flags": 0x02, "smt": True, "eff": 0,
                 "lps": [lp, lp+1], "kind": "big"})
    lp += 2
lp_sibs = {0: {1}, 1: {0}, 2: {3}, 3: {2}}
lp_core = {0: 0, 1: 0, 2: 1, 3: 1}
TOPO = {"phys_cores": phys, "lp_siblings": lp_sibs,
        "lp_core": lp_core, "total_lp": 4, "eff_classes": [0]}

# ---- 非瓶颈占用（所有核都很低且均衡，is_bottleneck 必为 False）----
UTIL_LOW = {0: 12.0, 1: 10.0, 2: 14.0, 3: 11.0}

from ningbingjue.core import topology as topo_mod
from ningbingjue.platform import cpuset as cpuset_mod
from ningbingjue.blocks.smt import SmtBlock


def _fake_get_topology(self):
    return TOPO

def _fake_sample_core_util(perf_probe, n=3):
    # 返回低负载占用 -> is_bottleneck = False
    return dict(UTIL_LOW), None

def _fake_find_foreground_game(whitelist=None):
    return None  # 无游戏进程

def _fake_restore_all(procs, dry_run, log):
    return {"restored": len(procs)}

def _fake_apply_smt_isolation(game_pid, hot_lp, topology, processes, dry_run, log):
    # 记录被调用 -> 证明强制施加
    _fake_apply_smt_isolation.called = True
    _fake_apply_smt_isolation.hot_lp = hot_lp
    log(f"[fake-apply] game_pid={game_pid} hot_lp={hot_lp} procs={len(processes)}")
    return {"applied": len(processes), "skipped": 0, "game_pid": game_pid,
            "hot_lp": hot_lp, "mechanism": "CPU集合",
            "game_excluded": [], "nongame_excluded": [hot_lp]}
_fake_apply_smt_isolation.called = False


def test_lock_forces_apply_even_when_not_bottleneck():
    """非瓶颈场景下 lock_once 也必须施加（修复前会 SKIP）。"""
    cfg = MagicMock()
    cfg.smt_config = MagicMock(
        saturated_thr=80.0, min_standout=30.0, k_sigma=2.0,
        active_floor=5.0, cluster_scope=True, enabled=False)
    pm = MagicMock()
    pm.enumerate.return_value = [MagicMock(pid=1234, name="dummy.exe")]

    blk = SmtBlock(cfg, pm)
    with patch.object(SmtBlock, "_get_topology", _fake_get_topology), \
         patch.object(topo_mod, "sample_core_util", _fake_sample_core_util), \
         patch.object(topo_mod, "find_foreground_game", _fake_find_foreground_game), \
         patch.object(cpuset_mod, "restore_all", _fake_restore_all), \
         patch.object(cpuset_mod, "apply_smt_isolation", _fake_apply_smt_isolation):
        blk.lock_once()
        # 等后台线程完成（采样是 mock 即时返回，但仍需等线程调度）
        for _ in range(50):
            time.sleep(0.05)
            st = blk.status()
            if st.get("mode") == "lock" or st.get("error"):
                break

    st = blk.status()
    print(f"  status: {st}")
    assert _fake_apply_smt_isolation.called, \
        "FAIL: apply_smt_isolation 未被调用（lock_once 仍被 is_bottleneck gate）"
    assert st.get("mode") == "lock", f"FAIL: mode != lock, got {st.get('mode')}"
    assert st.get("is_bottleneck") is False, "前置：非瓶颈场景"
    assert st.get("applied", 0) > 0, "FAIL: applied=0（未施加任何进程）"
    print(f"  PASS: 非瓶颈场景下强制施加，hot_lp={_fake_apply_smt_isolation.hot_lp}, "
          f"applied={st.get('applied')}")


def test_lock_records_error_on_sample_failure():
    """采样失败时 lock_once 应记录 error 而非静默 return（修复前不设 _last）。"""
    _fake_apply_smt_isolation.called = False  # 跨测试共享，重置
    cfg = MagicMock()
    cfg.smt_config = MagicMock(
        saturated_thr=80.0, min_standout=30.0, k_sigma=2.0,
        active_floor=5.0, cluster_scope=True, enabled=False)
    pm = MagicMock()
    pm.enumerate.return_value = []

    blk = SmtBlock(cfg, pm)

    def _fail_sample(perf_probe, n=3):
        return None, "Get-Counter unavailable"
    with patch.object(SmtBlock, "_get_topology", _fake_get_topology), \
         patch.object(topo_mod, "sample_core_util", _fail_sample), \
         patch.object(topo_mod, "find_foreground_game", _fake_find_foreground_game), \
         patch.object(cpuset_mod, "restore_all", _fake_restore_all), \
         patch.object(cpuset_mod, "apply_smt_isolation", _fake_apply_smt_isolation):
        blk.lock_once()
        for _ in range(50):
            time.sleep(0.05)
            st = blk.status()
            if st.get("mode") == "lock":
                break
    st = blk.status()
    print(f"  status: {st}")
    assert st.get("mode") == "lock", f"FAIL: mode != lock, got {st.get('mode')}"
    assert st.get("error"), "FAIL: 采样失败但未记录 error"
    assert not _fake_apply_smt_isolation.called, "FAIL: 采样失败却仍施加"
    print(f"  PASS: 采样失败记录 error='{st.get('error')}'，未施加")


def test_lock_btn_hint_is_a():
    """SmtPanel.nav_items 中 lock_btn 的 hint 应为 'A'（手柄 _enter_card 聚焦）。"""
    from ningbingjue.ui.panels import SmtPanel
    items_src = SmtPanel.nav_items
    # 不实例化（需要 Qt），直接读源码字符串断言
    import inspect
    src = inspect.getsource(items_src)
    assert 'hint="A"' in src, "FAIL: lock_btn 缺少 hint='A'"
    # 进一步确认 hint='A' 绑在 lock_btn 这一行
    lines = [l.strip() for l in src.splitlines() if "lock_btn" in l]
    assert any('hint="A"' in l for l in lines), \
        f"FAIL: lock_btn 行未带 hint='A', lines={lines}"
    print(f"  PASS: lock_btn nav_items 已带 hint='A'")
    print(f"    {lines}")


if __name__ == "__main__":
    print("=" * 60)
    print("TEST 1: lock_once 非瓶颈场景强制施加")
    print("=" * 60)
    test_lock_forces_apply_even_when_not_bottleneck()
    print()
    print("=" * 60)
    print("TEST 2: lock_once 采样失败记录 error")
    print("=" * 60)
    test_lock_records_error_on_sample_failure()
    print()
    print("=" * 60)
    print("TEST 3: lock_btn nav_items hint='A'")
    print("=" * 60)
    test_lock_btn_hint_is_a()
    print()
    print("ALL TESTS PASSED")
