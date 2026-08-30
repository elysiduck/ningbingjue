"""纯分页逻辑（不依赖 Qt，便于单测）。

约定
----
* ``breaks`` 是各页「起始索引」有序列表（首段必为 0），不含末尾 total。
* ``order`` 为完整卡片序列。
* 所有函数都先把 ``breaks`` 归一化为「每段 ≤ cap」的形式，再据此计算。

``LayoutEditPanel`` 仅做薄封装：在自己的 ``order`` / ``_page_breaks`` 上
调用这里的函数，再触发 UI 重绘。
"""

from __future__ import annotations

from collections import Counter
from typing import Optional


def pg_norm_breaks(order_len: int, breaks: list[int],
                   cap: Optional[int] = None) -> list[int]:
    """归一化：返回『每段 ≤ cap』的起始索引列表。

    * 去重、强制首段从 0 开始；
    * 任意超出上限的连续段，在每 cap 个处分割（自动分栏）；
    * 丢弃 >= total 的尾随空页断点（避免残留空页）。
    * ``cap`` 为 ``None``（或 <= 0）表示不封顶：不做任何自动分栏，
      一段可以容纳任意数量的卡片。
    """
    total = order_len
    br = sorted({int(x) for x in breaks if 0 <= int(x) <= total})
    if not br or br[0] != 0:
        br = [0] + [b for b in br if b != 0]
    out: list[int] = []
    for i in range(len(br)):
        s = br[i]
        e = br[i + 1] if i + 1 < len(br) else total
        out.append(s)
        # 段内若超过上限，按 cap 自动拆成若干栏。
        # cap 为 None 表示不封顶，跳过自动分栏。
        if cap:
            p = s + cap
            while p < e:
                out.append(p)
                p += cap
    out = sorted({b for b in out if b < total})
    if not out or out[0] != 0:
        out = [0] + out
    return out


def pg_pages(order_len: int, breaks: list[int], cap: int) -> list[tuple[int, int]]:
    """返回归一化后的各页区间 ``[(start, end), ...]``（end 不含）。"""
    br = pg_norm_breaks(order_len, breaks, cap)
    segs: list[tuple[int, int]] = []
    for i in range(len(br)):
        s = br[i]
        e = br[i + 1] if i + 1 < len(br) else order_len
        if s < e:
            segs.append((s, e))
    return segs


def pg_page_index_of(order_len: int, breaks: list[int], cap: int,
                     pos: int) -> Optional[tuple[int, int, int]]:
    """返回 ``pos`` 所在页的 ``(页序号, start, end)``；不在任何页返回 None。"""
    for i, (s, e) in enumerate(pg_pages(order_len, breaks, cap)):
        if s <= pos < e:
            return (i, s, e)
    return None


def pg_move_in_page(order: list[str], breaks: list[int], cap: int,
                     pos: int, delta: int):
    """页内上移/下移：仅在本页内交换，越界（跨页）则忽略。

    返回 ``(order, breaks)``（可能未改动，原对象不变）。
    """
    seg = pg_page_index_of(len(order), breaks, cap, pos)
    if seg is None:
        return order, breaks
    _, s, e = seg
    npos = pos + delta
    if npos < s or npos >= e:
        return order, breaks
    order = list(order)
    order[pos], order[npos] = order[npos], order[pos]
    return order, breaks


def pg_move_to_prev_page(order: list[str], breaks: list[int],
                             cap: Optional[int], pos: int):
    """▲ 移动到上一页：把卡片挪到上一页末尾。

    成功返回 ``(order, breaks)``；不可行（无上一页 / 上一页已满）
    返回 ``None``。``cap`` 为 ``None`` 时不做容量上限判断。
    """
    breaks = pg_norm_breaks(len(order), breaks, cap)
    segs = pg_pages(len(order), breaks, cap)
    seg = pg_page_index_of(len(order), breaks, cap, pos)
    if seg is None:
        return None
    pi, s, e = seg
    if pi <= 0:
        return None
    prev_s, prev_e = segs[pi - 1]
    if cap and (prev_e - prev_s) >= cap:
        return None
    order = list(order)
    breaks = list(breaks)
    # 当前页断点（起始索引）后移一位 → 上一页扩容、本页缩容。
    breaks[pi] += 1
    # 把 pos 处卡片插入到「上一页末尾」(索引 s-1) 之前。
    item = order.pop(pos)
    order.insert(s - 1, item)
    breaks = pg_norm_breaks(len(order), breaks, cap)
    return order, breaks


def pg_move_to_next_page(order: list[str], breaks: list[int],
                             cap: Optional[int], pos: int):
    """▼ 移动到下一页：把卡片挪到下一页开头。

    成功返回 ``(order, breaks)``；不可行（无下一页 / 下一页已满）
    返回 ``None``。``cap`` 为 ``None`` 时不做容量上限判断。
    """
    breaks = pg_norm_breaks(len(order), breaks, cap)
    segs = pg_pages(len(order), breaks, cap)
    seg = pg_page_index_of(len(order), breaks, cap, pos)
    if seg is None:
        return None
    pi, s, e = seg
    if pi >= len(segs) - 1:
        return None
    next_s, next_e = segs[pi + 1]
    if cap and (next_e - next_s) >= cap:
        return None
    order = list(order)
    breaks = list(breaks)
    # 下一页断点（起始索引）前移一位 → 本页缩容、下一页扩容。
    breaks[pi + 1] -= 1
    # 把 pos 处卡片插入到「下一页开头」(索引 e-1) 之前。
    item = order.pop(pos)
    order.insert(e - 1, item)
    breaks = pg_norm_breaks(len(order), breaks, cap)
    return order, breaks


def pg_prune_empty_pages(order_len: int, breaks: list[int]) -> list[int]:
    """删除会产生空页（0 张卡片）的断点，保证『保存后每个页面至少有 1 张卡』。

    规则：
    * 强制首段从 0 开始、去掉越界断点（与 ``pg_norm_breaks`` 一致）；
    * 某个断点值【出现超过一次】→ 它夹出了一个零宽段（空页），整组删掉：
      同时去掉空页自身的起始边界与紧随其后的那一页起始边界，使前后两页
      合并（例如 ``[0,3,3,7]`` → ``[0,7]``）；
    * 尾随 ``== order_len`` 的断点（尾随空页）直接丢弃。

    返回归一化后的断点列表；若原输入不含空页则基本原样返回。
    """
    total = order_len
    raw = [int(x) for x in breaks if 0 <= int(x) <= total]
    if not raw or raw[0] != 0:
        raw = [0] + [b for b in raw if b != 0]
    # 出现多次的断点值 = 夹出空页的边界 → 全部删掉（含其后的页起始边界）。
    cnt = Counter(raw)
    out = [b for b in raw if cnt[b] == 1]
    # 兜底：去掉尾随等于 total 的断点（尾随空页）。
    out = [b for b in out if b < total]
    if not out or out[0] != 0:
        out = [0] + [b for b in out if b != 0]
    return sorted(set(out))
