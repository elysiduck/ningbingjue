"""页面布局编辑 —— 不封顶（每页数量无上限）+ 卡片跨页移动 回归测试。

锁定本轮需求（2026-07-22 重做）：

* 每页卡片数量【不封顶】：没有自动拆分，一个页面可以容纳任意数量的卡片；
  只有当用户显式插入分页断点时才会分出新页。最少每页至少 1 张卡。
* 每个功能卡片在页面布局编辑里新增两个三角形按钮：
  ▲ 移动到上一页 / ▼ 移动到下一页（不封顶后不再有「上一页/下一页已满」
  的容量阻塞，唯一失败原因是「没有上一页 / 没有下一页」）。
* 空页（0 张卡）的断点在归一化 / 保存时被彻底删除——把某页卡片全挪走，
  该页分隔条立即消失，保存后配置里也不残留空页。
* 每个功能卡片的按钮排布（顺序固定）：
  页内上移(↑) · 页内下移(↓) · ▲移动到上一页 · ▼移动到下一页 · 卡片编辑(编辑项)。

纯分页逻辑（_normalize_self_breaks / _pages / _move*）只依赖
``self.order`` 与 ``self._page_breaks``，故测试直接构造 order 以
保证确定性与不依赖默认配置的卡片数量。
"""

from __future__ import annotations

import json

import pytest
from PyQt6.QtWidgets import QApplication, QLabel

from ningbingjue.app import Application
from ningbingjue.core.config import Tile


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _seed(tmp_path, tiles):
    cfg = tmp_path / "layout.json"
    from ningbingjue.core.config import AppConfig
    ac = AppConfig()
    ac.tiles = tiles
    cfg.write_text(json.dumps(ac.to_dict(), ensure_ascii=False), encoding="utf-8")
    return cfg


def _panel(tmp_path, qapp, n_cards=14):
    """Boot a real app + layout panel, then overwrite ``order`` with ``n_cards``
    synthetic (registry-less) card keys so pagination is fully deterministic."""
    cfg = _seed(tmp_path, [Tile(name="A", launch_path="C:/a", launch_type="FILE")])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    panel = app._window._edit_overlay._layout_panel
    panel.order = [f"k{i}" for i in range(n_cards)]
    panel._page_breaks = [0]
    panel._render_rows()
    return app, panel


def _cards(panel):
    """Collect the rendered card widgets (those carrying ``_pos``)."""
    out = []
    for i in range(panel._list_lay.count()):
        w = panel._list_lay.itemAt(i).widget()
        if w is not None and hasattr(w, "_pos"):
            out.append(w)
    return out


# --------------------------------------------------------------------------
# 需求 1：不封顶（不再自动按 6 拆分）
# --------------------------------------------------------------------------
def test_uncapped_single_page_when_no_breaks(tmp_path, qapp):
    _app, panel = _panel(tmp_path, qapp, n_cards=14)
    segs = panel._pages()
    # 不封顶：14 张、无断点 → 整段为一页（不会自动拆成 6/6/2）。
    assert len(segs) == 1, f"不封顶时不应自动拆分，实际 {segs}"
    assert (segs[0][1] - segs[0][0]) == 14
    # 每页至少 1 张、总数守恒
    assert all((e - s) >= 1 for s, e in segs)
    assert sum(e - s for s, e in segs) == 14
    _app._window.close()
    qapp.processEvents()


def test_single_page_under_cap(tmp_path, qapp):
    _app, panel = _panel(tmp_path, qapp, n_cards=4)
    segs = panel._pages()
    assert len(segs) == 1
    assert (segs[0][1] - segs[0][0]) == 4
    _app._window.close()
    qapp.processEvents()


def test_page_separator_shows_count(tmp_path, qapp):
    _app, panel = _panel(tmp_path, qapp, n_cards=14)
    labels = [panel._list_lay.itemAt(i).widget()
              for i in range(panel._list_lay.count())]
    seps = [w for w in labels if isinstance(w, QLabel)
            and "第" in (w.text() or "")]
    # 不封顶 → 14 张只有 1 页 → 1 个分隔条，且显示本页卡片数（14 张）。
    assert len(seps) == 1
    assert "第 1 页" in (seps[0].text() or "")
    assert "14 张" in (seps[0].text() or "")
    _app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# 需求 3：按钮排布顺序（↑ · ↓ · ▲ · ▼ · 编辑项）
# --------------------------------------------------------------------------
def test_card_button_order_and_triangle_buttons(tmp_path, qapp):
    _app, panel = _panel(tmp_path, qapp, n_cards=8)  # 单页（不封顶，无显式断点不自动拆分）
    cards = _cards(panel)
    assert cards, "应有渲染出的卡片行"
    card0 = cards[0]
    # 按钮顺序：up, down, prev(▲), next(▼), edit(编辑项)
    row_lay = card0.layout()
    kinds = []
    for i in range(row_lay.count()):
        w = row_lay.itemAt(i).widget()
        if w is None:
            continue
        kinds.append(w)
    # 末位是 编辑项（仅 base_type in _EDITABLE 才有；合成 key 无 → 可能没有）
    # 关键：存在 ▲/▼ 三角形按钮，且排在上移/下移之后。
    texts = [w.text() for w in kinds]
    assert "↑" in texts and "↓" in texts
    assert "▲" in texts and "▼" in texts
    # ▲ 必须在 ↑/↓ 之后、编辑项之前（位置顺序）
    i_up = texts.index("↑")
    i_down = texts.index("↓")
    i_prev = texts.index("▲")
    i_next = texts.index("▼")
    assert i_up < i_prev and i_down < i_prev, "三角形按钮应排在上移/下移之后"
    assert i_prev < i_next, "上一页按钮应在下一页按钮之前"
    _app._window.close()
    qapp.processEvents()


def test_triangle_tooltips_explain_page_move(tmp_path, qapp):
    _app, panel = _panel(tmp_path, qapp, n_cards=8)  # 单页
    cards = _cards(panel)
    c = cards[0]
    assert "移动到上一页" in (c._prev.toolTip() or "")
    assert "移动到下一页" in (c._next.toolTip() or "")
    _app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# 需求 3（续）：页内上移/下移 不跨页边界
# --------------------------------------------------------------------------
def test_within_page_move_stays_in_page(tmp_path, qapp):
    # 不封顶：需显式设置分页断点才有两页。这里 [0,6) / [6,8) 两页。
    _app, panel = _panel(tmp_path, qapp, n_cards=8)
    panel._page_breaks = [0, 6]
    panel._render_rows()
    before = list(panel.order)
    # k0 在第一页页首：上移应被边界挡住（不变）
    panel._move(0, -1)
    assert panel.order == before
    # k5 是第一页页尾：下移会越过边界到第二页 → 拦住（不变）
    panel._move(5, 1)
    assert panel.order == before
    # k1 ↔ k2 同页内交换 → 允许
    panel._move(1, 1)
    assert panel.order[1] == "k2" and panel.order[2] == "k1"
    # 页内上移后可还原
    panel._move(2, -1)
    assert panel.order == before
    _app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# 需求 2 + 3：▲ 移动到上一页 / ▼ 移动到下一页（不封顶后不再被容量阻塞）
# --------------------------------------------------------------------------
def test_move_to_prev_page_relocates_card(tmp_path, qapp):
    # 8 张，强制 [0,3) / [3,8) 两页；第二页首张 k3 移到上一页。
    _app, panel = _panel(tmp_path, qapp, n_cards=8)
    panel._page_breaks = [0, 3]
    panel._render_rows()
    assert panel._page_index_of(3)[0] == 1  # k3 在第二页
    panel._move_to_prev_page(3)
    # k3 现在应在第一页（页码 0）
    idx = panel.order.index("k3")
    assert panel._page_index_of(idx)[0] == 0
    # 每页至少 1 张（不封顶，不再有 ≤6 限制）
    assert all((e - s) >= 1 for s, e in panel._pages())
    _app._window.close()
    qapp.processEvents()


def test_move_to_next_page_relocates_card(tmp_path, qapp):
    # 8 张，[0,3) / [3,8)；第一页的 k1 移到下一页。
    _app, panel = _panel(tmp_path, qapp, n_cards=8)
    panel._page_breaks = [0, 3]
    panel._render_rows()
    assert panel._page_index_of(1)[0] == 0  # k1 在第一页
    panel._move_to_next_page(1)
    idx = panel.order.index("k1")
    assert panel._page_index_of(idx)[0] == 1  # 现在第二页
    assert all((e - s) >= 1 for s, e in panel._pages())
    _app._window.close()
    qapp.processEvents()


def test_move_to_prev_succeeds_into_big_page(tmp_path, qapp):
    # 不封顶：8 张，[0,6) / [6,8)；第二页首张 k6 移到「已满」的上一页
    # 也应成功（不再有「上一页已满」的阻塞）。
    _app, panel = _panel(tmp_path, qapp, n_cards=8)
    panel._page_breaks = [0, 6]
    panel._render_rows()
    before = list(panel.order)
    panel._move_to_prev_page(6)
    assert panel.order != before, "不封顶后上一页再满也能接收"
    assert panel._page_index_of(panel.order.index("k6"))[0] == 0
    _app._window.close()
    qapp.processEvents()


def test_move_to_next_succeeds_into_big_page(tmp_path, qapp):
    # 不封顶：14 张，[0,6)/[6,12)/[12,14)；把第一页 k0 下移到「已满」的
    # 第二页也应成功。
    _app, panel = _panel(tmp_path, qapp, n_cards=14)
    panel._page_breaks = [0, 6, 12]
    panel._render_rows()
    before = list(panel.order)
    panel._move_to_next_page(0)
    assert panel.order != before, "不封顶后下一页再满也能接收"
    assert panel._page_index_of(panel.order.index("k0"))[0] == 1
    _app._window.close()
    qapp.processEvents()


def test_move_to_prev_blocked_on_first_page(tmp_path, qapp):
    _app, panel = _panel(tmp_path, qapp, n_cards=8)
    panel._page_breaks = [0, 3]
    panel._render_rows()
    before = list(panel.order)
    panel._move_to_prev_page(1)  # k1 在第一页 → 没有上一页
    assert panel.order == before
    _app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# 空页（0 张卡）自动消失
# --------------------------------------------------------------------------
def test_empty_page_vanishes_after_draining(tmp_path, qapp):
    # 8 张，[0,4)/[4,8) 两页；反复把「最后一页的最后一个卡片」上移到
    # 上一页，直到第二页被掏空 → 分隔条立即消失（所见即所得）。
    _app, panel = _panel(tmp_path, qapp, n_cards=8)
    panel._page_breaks = [0, 4]
    panel._render_rows()
    assert len(panel._pages()) == 2
    guard = 0
    while len(panel._pages()) > 1 and guard < 50:
        segs = panel._pages()
        last_pos = segs[-1][1] - 1  # 最后一页的最后一个卡片索引
        panel._move_to_prev_page(last_pos)
        guard += 1
    # 现在只剩 1 页、且包含全部 8 张。
    segs = panel._pages()
    assert len(segs) == 1, f"空页应已消失，实际 {segs}"
    assert sum(e - s for s, e in segs) == 8
    _app._window.close()
    qapp.processEvents()


# --------------------------------------------------------------------------
# 三角形按钮在不可用时禁用（仅边页：没有上一页 / 没有下一页）
# --------------------------------------------------------------------------
def test_triangle_buttons_disabled_at_edges(tmp_path, qapp):
    # 单页（不封顶）：首卡无上一页、末卡无下一页 → 边页三角形按钮禁用。
    _app, panel = _panel(tmp_path, qapp, n_cards=8)
    cards = _cards(panel)
    first = cards[0]      # 第一页页首 → ▲ 应禁用（没有上一页）
    last = cards[-1]       # 第二页页尾 → ▼ 应禁用（没有下一页）
    assert first._prev.isEnabled() is False
    assert last._next.isEnabled() is False
    _app._window.close()
    qapp.processEvents()


def test_triangle_buttons_into_full_page_not_blocked(tmp_path, qapp):
    # 不封顶：8 张，[0,6)/[6,8)；第二页首张 k6 的 ▲ 入口（上一页「已满」）
    # 仍应可用——只有「没有上一页」才禁用。▼ 因已是最后一页而禁用。
    _app, panel = _panel(tmp_path, qapp, n_cards=8)
    panel._page_breaks = [0, 6]
    panel._render_rows()
    cards = _cards(panel)
    k6_card = next(c for c in cards if c._pos == 6)
    assert k6_card._prev.isEnabled() is True   # 上一页存在，不封顶不阻塞
    assert k6_card._next.isEnabled() is False  # 已经是最后页
    _app._window.close()
    qapp.processEvents()
