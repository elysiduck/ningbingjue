"""``._pagination`` 纯逻辑单测（不依赖 Qt，可在无头环境直接跑）。

覆盖本轮需求（不封顶 / 最少 1 张 / 空页消失）：
* 每页不再有数量上限（cap=None）→ 不自动分栏，一段可容纳任意数量。
* 页内上移/下移只在页内、不跨边界。
* ▲ 移动到上一页 / ▼ 移动到下一页：跨页重排，但不再受容量上限阻塞。
* 空页（0 张卡）的断点在归一化/保存时被彻底删除。
"""

from __future__ import annotations

import pytest

from ningbingjue.ui._pagination import (
    pg_move_in_page,
    pg_move_to_next_page,
    pg_move_to_prev_page,
    pg_norm_breaks,
    pg_page_index_of,
    pg_pages,
    pg_prune_empty_pages,
)

CAP = None  # 不封顶：一页可容纳任意数量卡片


# --------------------------------------------------------------------------
# 需求 1：不封顶（不再自动按 6 拆分）
# --------------------------------------------------------------------------
def test_norm_no_auto_split_when_uncapped():
    # 14 张、单段 [0,14) → 不拆分，整段为一页
    br = pg_norm_breaks(14, [0], CAP)
    assert br == [0]
    segs = pg_pages(14, [0], CAP)
    assert segs == [(0, 14)]
    # 总数守恒
    assert sum(e - s for s, e in segs) == 14


def test_norm_under_cap_single_page():
    assert pg_norm_breaks(4, [0], CAP) == [0]
    assert pg_pages(4, [0], CAP) == [(0, 4)]


def test_norm_dedupe_and_force_zero():
    # 重复断点去重 + 强制首段从 0 开始
    # 输入 [3,3,9] → 去重为 [3,9]，补 0 得 [0,3,9]。
    assert pg_norm_breaks(10, [3, 3, 9], CAP) == [0, 3, 9]
    assert pg_pages(10, [3, 3, 9], CAP) == [(0, 3), (3, 9), (9, 10)]
    # 空断点 → 仅单页，起始为 0
    assert pg_norm_breaks(4, [], CAP) == [0]


def test_norm_drops_trailing_empty():
    # >= total 的尾随断点被丢弃
    assert pg_norm_breaks(8, [0, 6, 8], CAP) == [0, 6]


# --------------------------------------------------------------------------
# 需求 3（页内）：上移/下移不跨页边界
# --------------------------------------------------------------------------
def test_move_in_page_stays_in_page():
    # [0,6) [6,8) 两页
    order = [f"k{i}" for i in range(8)]
    breaks = pg_norm_breaks(8, [0, 6], CAP)
    # 第一页页首 k0 上移 → 越界忽略
    o, b = pg_move_in_page(order, breaks, CAP, 0, -1)
    assert o is order and b is breaks
    # 第一页页尾 k5 下移 → 越过到第二页，忽略
    o, b = pg_move_in_page(order, breaks, CAP, 5, 1)
    assert o is order
    # k1↔k2 同页交换 → 允许
    o, b = pg_move_in_page(order, breaks, CAP, 1, 1)
    assert o[1] == "k2" and o[2] == "k1"
    # 还原
    o, b = pg_move_in_page(o, b, CAP, 2, -1)
    assert o[1] == "k1" and o[2] == "k2"


# --------------------------------------------------------------------------
# 需求 2 + 3：▲ 移动到上一页 / ▼ 移动到下一页（不封顶后不再被容量阻塞）
# --------------------------------------------------------------------------
def test_move_to_prev_page_relocates():
    # 8 张，强制 [0,3) / [3,8) 两页；k3（第二页首）移到上一页
    order = [f"k{i}" for i in range(8)]
    breaks = pg_norm_breaks(8, [0, 3], CAP)
    assert pg_page_index_of(8, breaks, CAP, 3)[0] == 1
    res = pg_move_to_prev_page(order, breaks, CAP, 3)
    assert res is not None
    o, b = res
    assert pg_page_index_of(8, b, CAP, o.index("k3"))[0] == 0
    # 每页至少 1 张、总数守恒
    segs = pg_pages(8, b, CAP)
    assert all((e - s) >= 1 for s, e in segs)
    assert sum(e - s for s, e in segs) == 8


def test_move_to_next_page_relocates():
    # 8 张，[0,3) / [3,8)；k1（第一页）移到下一页
    order = [f"k{i}" for i in range(8)]
    breaks = pg_norm_breaks(8, [0, 3], CAP)
    assert pg_page_index_of(8, breaks, CAP, 1)[0] == 0
    res = pg_move_to_next_page(order, breaks, CAP, 1)
    assert res is not None
    o, b = res
    assert pg_page_index_of(8, b, CAP, o.index("k1"))[0] == 1
    segs = pg_pages(8, b, CAP)
    assert all((e - s) >= 1 for s, e in segs)
    assert sum(e - s for s, e in segs) == 8


def test_move_to_prev_succeeds_without_cap():
    # 不封顶：上一页哪怕已经“满”也能接收（这里 [0,6)/[6,8) 上一页 6 张）
    order = [f"k{i}" for i in range(8)]
    breaks = pg_norm_breaks(8, [0, 6], CAP)
    res = pg_move_to_prev_page(order, breaks, CAP, 6)
    assert res is not None
    o, b = res
    # k6 现在落在第一页
    assert pg_page_index_of(8, b, CAP, o.index("k6"))[0] == 0


def test_move_to_next_succeeds_without_cap():
    # 不封顶：下一页哪怕已经“满”也能接收
    order = [f"k{i}" for i in range(14)]
    breaks = pg_norm_breaks(14, [0, 6, 12], CAP)
    res = pg_move_to_next_page(order, breaks, CAP, 0)
    assert res is not None
    o, b = res
    assert pg_page_index_of(14, b, CAP, o.index("k0"))[0] == 1


def test_move_to_prev_blocked_on_first_page():
    order = [f"k{i}" for i in range(8)]
    breaks = pg_norm_breaks(8, [0, 3], CAP)
    res = pg_move_to_prev_page(order, breaks, CAP, 1)  # k1 在第一页
    assert res is None


def test_move_to_next_blocked_on_last_page():
    order = [f"k{i}" for i in range(8)]
    breaks = pg_norm_breaks(8, [0, 6], CAP)
    res = pg_move_to_next_page(order, breaks, CAP, 7)  # k7 在最后一页
    assert res is None


def test_no_empty_pages_after_moves():
    # 反复跨页移动后：任何页都不为空，且总数守恒
    order = [f"k{i}" for i in range(13)]
    breaks = pg_norm_breaks(13, [0, 5, 9], CAP)
    res = pg_move_to_prev_page(order, breaks, CAP, 9)  # 第三页首上移
    assert res is not None
    o, b = res
    segs = pg_pages(13, b, CAP)
    assert all((e - s) >= 1 for s, e in segs)
    assert sum(e - s for s, e in segs) == 13


# --------------------------------------------------------------------------
# 需求：空页断点被彻底删除
# --------------------------------------------------------------------------
def test_prune_removes_middle_empty_page():
    # [0,3) [3,3) 空 [3,7) → 删除中间空页，前后两页合并为单页 [0,7)，
    # 断点列表即 [0]（单页延伸到 total，无需再写 7）。
    assert pg_prune_empty_pages(7, [0, 3, 3, 7]) == [0]


def test_prune_removes_trailing_empty_page():
    # 尾随空页断点（== total）直接丢弃
    assert pg_prune_empty_pages(8, [0, 6, 8]) == [0, 6]


def test_prune_keeps_valid_pages():
    assert pg_prune_empty_pages(10, [0, 3, 9]) == [0, 3, 9]
