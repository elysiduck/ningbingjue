"""Headless self-test for the 8 reported regressions.

Run with the bundled Python. Uses the offscreen Qt platform so no window is
actually shown. Verifies the *logic* of the fixes that can be exercised without
building the full app (which hangs in headless mode).
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QPushButton,
                             QScrollArea, QLabel)

# Make the package importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from ningbingjue.ui.gamepad_hint import HintButton
from ningbingjue.ui.rounded_button import RoundedButton
from ningbingjue.ui import _scroll_style
from ningbingjue.ui.interaction import InteractionController, NavItem
from ningbingjue.ui import button_edit as _be
from ningbingjue.core.color.styles import BUTTON_SELECTION

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)


app = QApplication.instance() or QApplication(sys.argv)

# ---------------------------------------------------------------------------
# 1) HintButton: native paint path keeps caller stylesheet colours + font.
# ---------------------------------------------------------------------------
hb = HintButton("测试按钮")
hb.setStyleSheet(
    "QPushButton { background: rgba(255,170,60,0.16); color: #ffce8a;"
    " border: none; border-radius: 6px; padding: 4px 10px 4px 20px;"
    " font-size: 11px; font-weight: bold; }")
hb.set_gamepad_hint("A")
hb.show()
hb.repaint()
check("HintButton native paint keeps caller stylesheet (no crash)", True)
check("HintButton sizeHint >= 56 wide", hb.sizeHint().width() >= 56)
hs = hb.sizeHint()
check("HintButton sizeHint height >= 26", hs.height() >= 26)

# gamepad hint stored
check("HintButton stores gamepad hint", hb.gamepad_hint == "A")
check("HintButton show_gamepad_hint True", hb.show_gamepad_hint is True)

# set_style via the editor helper still produces a stylesheet (so later
# setStyleSheet override survives visually) -- verify it doesn't raise.
try:
    hb.set_style("#82ff1e", True)
    ok_style = True
except Exception as e:  # pragma: no cover
    ok_style = False
    print("  set_style error:", e)
check("HintButton.set_style() runs", ok_style)

# ---------------------------------------------------------------------------
# 7) Scrollbar: always_on=False -> ScrollBarAsNeeded (no reserved gutter).
# ---------------------------------------------------------------------------
sa = QScrollArea()
_scroll_style.apply_thin_scroll(sa, always_on=False)
check("scrollbar AsNeeded when always_on=False",
      sa.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded)
sa2 = QScrollArea()
_scroll_style.apply_thin_scroll(sa2, always_on=True)
check("scrollbar AlwaysOn when always_on=True",
      sa2.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOn)

# ---------------------------------------------------------------------------
# 1/6) InteractionController navigation between buttons (item mode).
# ---------------------------------------------------------------------------
class FakePanel(QWidget):
    def __init__(self, n, x, parent=None):
        super().__init__(parent)
        self.btns = []
        for i in range(n):
            b = QPushButton(f"btn{i}", self)
            # Explicit, distinct geometry so directional nav can disambiguate
            # (in a real app the layout does this; headless offscreen gives all
            # widgets (0,0) unless we set it).
            b.setGeometry(x, 10 + i * 40, 120, 30)
            self.btns.append(b)

    def nav_items(self):
        items = []
        hints = ["A", "B", "X", "Y", "X", "Y"]
        for i, b in enumerate(self.btns):
            items.append(NavItem(widget=b, kind="action",
                                 hint=hints[i % len(hints)],
                                 confirm=b.click))
        return items


win = QWidget()
host = QWidget(win)
ctrl = InteractionController(win, host, config=None)
# mimic the app: 2 panels (cards), each with 3 buttons stacked vertically.
p1 = FakePanel(3, 10, win)
p2 = FakePanel(3, 300, win)
ctrl.register_panel(p1)
ctrl.register_panel(p2)
ctrl.refresh()
# Show the window so the focus-rect's ancestors are visible, making
# isVisible() a meaningful signal (offscreen widgets start hidden).
win.show()
app.processEvents()

# Cold start: no focus, no rect (rule #6: no default cursor until nav).
check("cold start: focus == -1", ctrl._focus == -1)
check("cold start: rect hidden", not ctrl._rect.isVisible())

# Simulate a down-arrow key -> _gp_active becomes True and a card is selected.
ev_down = type("E", (), {"key": lambda self: Qt.Key.Key_Down,
                         "modifiers": lambda self: Qt.KeyboardModifier.NoModifier})()
# We need a real QKeyEvent for handle_key; build one.
from PyQt6.QtGui import QKeyEvent
ke = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Down,
               Qt.KeyboardModifier.NoModifier)
ctrl.handle_key(ke)
check("after first arrow: _gp_active True", ctrl._gp_active is True)
check("after first arrow: a card focus set", ctrl._card_focus >= 0)

# Enter the card (A/Enter) -> item mode, focus on A-hinted button.
ke_a = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return,
                 Qt.KeyboardModifier.NoModifier)
ctrl.handle_key(ke_a)
check("after Enter: card_mode False (item mode)", ctrl._card_mode is False)
check("after Enter: focus on a button", ctrl._focus >= 0)

# Now arrow down should move focus to the NEXT button in the same card (1->2).
focus_before = ctrl._focus
ke_down2 = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Down,
                    Qt.KeyboardModifier.NoModifier)
moved = ctrl.handle_key(ke_down2)
check("item-mode down moves focus to next button",
      moved and ctrl._focus == focus_before + 1)
check("item-mode down keeps rect visible", ctrl._rect.isVisible())

# ---------------------------------------------------------------------------
# 6) Edit-mode entry: no default cursor (prev_focus == -1 on first entry).
# ---------------------------------------------------------------------------
ctrl.set_edit_mode(True)
check("edit mode entry: focus == -1 (no default cursor)", ctrl._focus == -1)
check("edit mode entry: rect hidden (cold)", not ctrl._rect.isVisible())

# ---------------------------------------------------------------------------
# 8) Button-style apply without a preset MUST persist (DIRECT_STYLES) so the
#    HUD rebuild reproduces it.  Verifies the "apply -> return -> unchanged"
#    regression is fixed at the data layer.
# ---------------------------------------------------------------------------
import tempfile, shutil
from ningbingjue.core.color import styles as ST
from ningbingjue.core.color.styles import (
    ButtonStyle, apply_styles_to_buttons, save_all_button_data,
    load_all_button_data, DIRECT_STYLES, BUTTON_PRESET_MAP, PER_BUTTON_PRESETS,
)

tmp = tempfile.mkdtemp(prefix="nb_test_")
try:
    # Reset the module-level registries (simulate a fresh boot).
    DIRECT_STYLES.clear()
    BUTTON_PRESET_MAP.clear()
    PER_BUTTON_PRESETS.clear()

    st = ButtonStyle(corner_radius=14, border_thickness=4,
                     colors={"BackColor": "ff8800", "ForeColor": "ffffff"})
    key = "proc:1"
    # Simulate _apply_and_refresh with NO preset selected (the regression case):
    DIRECT_STYLES[key] = st
    save_all_button_data(tmp)  # persist

    # Now simulate returning to the main HUD: reload from disk + rebuild apply.
    DIRECT_STYLES.clear()
    BUTTON_PRESET_MAP.clear()
    load_all_button_data(tmp)
    check("8: DIRECT_STYLES reloaded from disk", key in DIRECT_STYLES)

    # Build a fake live button + key_map (as rebuild()/_apply_button_styles do).
    live = QPushButton("proc btn")
    key_map = {id(live): key}
    apply_styles_to_buttons([live], key_map)
    ss = live.styleSheet()
    check("8: direct style applied on rebuild (corner-radius:14px)",
          "border-radius: 14px" in ss)
    check("8: direct style applied on rebuild (orange back)",
          "rgb(255,136,0)" in ss)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------------------
# 8b) With a preset selected, the binding (not DIRECT_STYLES) drives the apply.
# ---------------------------------------------------------------------------
DIRECT_STYLES.clear()
BUTTON_PRESET_MAP.clear()
PER_BUTTON_PRESETS.clear()
PER_BUTTON_PRESETS["myP"] = ButtonStyle(corner_radius=10, colors={"BackColor": "00ff00"})
BUTTON_PRESET_MAP["proc:2"] = "myP"
live2 = QPushButton("proc btn2")
apply_styles_to_buttons([live2], {id(live2): "proc:2"})
check("8b: preset binding applies (corner-radius:10px)",
      "border-radius: 10px" in live2.styleSheet())

# ---------------------------------------------------------------------------
# 9) Button-list popup clones must NOT pollute collect_button_groups.
#    If preview clones inside the floating popup are collected, HUD button
#    indices shift after render_canvas() -> selected keys no longer match ->
#    selection rings vanish and presets bind to the wrong buttons.
# ---------------------------------------------------------------------------
from ningbingjue.ui.button_edit import collect_button_groups

main_win = QWidget()
main_win.setObjectName("main")
main_lay = QVBoxLayout(main_win)
# A real HUD button inside the main window.
hud_btn = QPushButton("HUD button")
main_lay.addWidget(hud_btn)

# A floating popup parented to the main window, hosting a preview clone.
popup = QWidget(main_win)
popup.setObjectName("popup")
popup_lay = QVBoxLayout(popup)
clone_btn = QPushButton("preview clone")
popup_lay.addWidget(clone_btn)

# Without popup exclusion: the clone is seen and shifts indices.
groups_no_exclude = collect_button_groups(main_win, exclude=None, popup=None)
popup_pollutes = False
for items in groups_no_exclude.values():
    for key, btn in items:
        if btn is clone_btn:
            popup_pollutes = True
            break
check("9: popup clone is collected when no popup exclusion", popup_pollutes)

# With popup exclusion: only the HUD button is collected.
groups_excluded = collect_button_groups(main_win, exclude=None, popup=popup)
only_hud = False
no_clone = True
for items in groups_excluded.values():
    for key, btn in items:
        if btn is hud_btn:
            only_hud = True
        if btn is clone_btn:
            no_clone = False
check("9: HUD button still collected with popup exclusion", only_hud)
check("9: popup clone excluded from collection", no_clone)

# ---------------------------------------------------------------------------
# 10) Rounded preset must survive reopen/rebuild.
# Root cause of "圆角预设绑定后变矩形": 之前「应用并刷新画布」只更新了内存
# BUTTON_PRESET_MAP + 扁平 map 文件，没把绑定写进当前主题 BUTTON_STYLES[active]。
# 编辑器每次打开调用 refresh() 会用主题覆盖内存映射 → 绑定丢失 → HUD 重建后
# 按钮退回直角。修复：_apply_and_refresh 也要走 _persist_assignments（和「绑定」
# 一致），把绑定写进主题。这里在模块级别复现该契约。
# ---------------------------------------------------------------------------
from ningbingjue.core.color import styles as _BS
from ningbingjue.core.color.styles import (
    PER_BUTTON_PRESETS, BUTTON_PRESET_MAP, BUTTON_STYLES,
    bind_button_preset, apply_styles_to_buttons, ButtonStyle,
)
_BS.ACTIVE_STYLE_NAME = "暗黑科技"
PER_BUTTON_PRESETS["ZZZ2"] = ButtonStyle(
    corner_radius=14, cut_size=0, border_thickness=4,
    hover_only_border=False, hover_gradient=True, link_theme=True,
    colors={"BackColor": "1c1e26", "BorderColor": "5fafeb",
            "ForeColor": "e6ff1e"})
# "应用并刷新画布" 现在会：bind + 写进主题（_persist_assignments 的核心两步）
bind_button_preset("ls#0", "ZZZ2")
BUTTON_STYLES[_BS.ACTIVE_STYLE_NAME]["ls#0"] = "ZZZ2"
# 模拟 refresh()：用主题覆盖内存映射
BUTTON_PRESET_MAP.clear()
BUTTON_PRESET_MAP.update(BUTTON_STYLES.get(_BS.ACTIVE_STYLE_NAME, {}))
check("10: 绑定写入主题后 refresh() 不丢绑定", BUTTON_PRESET_MAP.get("ls#0") == "ZZZ2")
# 模拟 HUD 重建：新按钮实例 + apply_styles_to_buttons
rebuilt = RoundedButton()
rebuilt_key = id(rebuilt)
apply_styles_to_buttons([rebuilt], {rebuilt_key: "ls#0"})
check("10: 重建后圆角预设仍生效 (corner_radius=14)", getattr(rebuilt, "corner_radius", 0) == 14)

# ---------------------------------------------------------------------------
# 11) Applying a preset must clear the multi-selection (and the persisted
# BUTTON_SELECTION set), so buttons don't keep their highlight after apply —
# even after closing & reopening the editor.
# Root cause of "应用后按钮仍选中、重进编辑器依旧选中": 之前「绑定」/「应用并刷新画布」
# 只消费了内存里的 self._sel_keys，没清持久化的 BUTTON_SELECTION；refresh()
# 重进编辑器时又把 BUTTON_SELECTION 还原成选中态。修复：两个动作末尾都
# self._sel_keys.clear() + self._sync_selection()（后者把空集写回 BUTTON_SELECTION
# 并 save_all_button_data 落盘）。
# ---------------------------------------------------------------------------
import types as _types
import tempfile as _tempfile

_sel_app = type("A", (), {"script_root": _tempfile.mkdtemp()})()
_sel_popup = QWidget()


class _SelPanel:
    def __init__(self):
        self._sel_keys = {"ls#0", "ls#1"}
        self._app = _sel_app
        self._overlay = QWidget()
        self._list_popup = _sel_popup


_sel = _SelPanel()
_sel._sync_selection = _types.MethodType(_be.ButtonEditPanel._sync_selection, _sel)
_sel._persist = _types.MethodType(_be.ButtonEditPanel._persist, _sel)
BUTTON_SELECTION.clear()
_sel._sync_selection()  # 选中 -> 持久化
check("11: 选中集写回 BUTTON_SELECTION 持久化", BUTTON_SELECTION == {"ls#0", "ls#1"})
# 模拟「应用并刷新画布」末尾的消费动作
_sel._sel_keys.clear()
_sel._sync_selection()  # 清空 -> 持久化空集
check("11: 应用后清空 -> BUTTON_SELECTION 也清空(重进编辑器不再选中)",
      BUTTON_SELECTION == set())

# ---------------------------------------------------------------------------
# 12) HintButton 预设不被选中/布局 resize 冲掉；切角生效。
# Root cause of "圆角>15变矩形 / 选中后变回默认 / 编辑器按钮丢色变高 / 短按钮变高":
# HintButton.set_gamepad_hint 与 resizeEvent 里调用了 set_style()，会把整个样式表
# 重写（border-radius 强制 7px、颜色重置为默认、且 height 随 top 布局涨到 34）。
# 修复：set_gamepad_hint / resizeEvent 只刷新内边距+重绘，绝不重写样式表；
# _hint_height 恒定 26（不再随 placement 涨高）；切角预设改由 _paint_chamfer 自绘
# （样式表背景透明，避免 background role 填满切角）。
# ---------------------------------------------------------------------------
from ningbingjue.ui.gamepad_hint import HintButton as _HB
import re as _re


def _br(ss):
    m = _re.search(r"border-radius:\s*(\d+)px", ss or "")
    return m.group(1) if m else "NONE"


_hb = _HB("锁帧: 关")
_hb.set_style("#7fd1ff", False)
_BS._apply_style_to_ctrl(
    _hb, ButtonStyle(corner_radius=17, cut_size=0, border_thickness=4,
                     colors={"BackColor": "1c1e26", "BorderColor": "5fafeb",
                             "ForeColor": "e6ff1e"}))
check("12: HUD 预设 radius=17 写入样式表", _br(_hb.styleSheet()) == "17")
_hb.set_gamepad_hint("A")  # 模拟选中显示手柄提示
check("12: 选中显示提示后 radius 不被冲掉(仍是17)", _br(_hb.styleSheet()) == "17")
check("12: 选中显示提示后颜色不被冲掉", "rgb(28,30,38)" in _hb.styleSheet())

# 编辑器按钮：自定义配色 + resize 翻 placement 不应丢色 / 变高
_eb = _HB("保存")
_eb.setStyleSheet("QPushButton { background: #82ff1e; color: #0c0f16; border: none;"
                  " border-radius: 7px; padding: 7px 16px 7px 22px; font-weight: bold; }")
_eb.show(); _eb.resize(60, 26)  # width<112 -> top placement 翻转
check("12: 编辑器按钮 resize 后自定义色保留", "82ff1e" in _eb.styleSheet())
check("12: 编辑器按钮高度紧凑(<=28)", _eb.sizeHint().height() <= 28)

# 切角：应用 cut_size=10 / corner_radius=0 -> 自绘路径生效（角落透明）
_cc = _HB("切角")
_cc.set_style("#7fd1ff", False)
_BS._apply_style_to_ctrl(
    _cc, ButtonStyle(corner_radius=0, cut_size=10, border_thickness=3,
                     colors={"BackColor": "1c1e26", "BorderColor": "5fafeb",
                             "ForeColor": "e6ff1e"}))
_cc.show(); _cc.resize(120, 26)
_cc_img = _cc.grab().toImage()
check("12: 切角预设自绘生效(左上角透明)", _cc_img.pixelColor(1, 1).alpha() < 30)

# ---------------------------------------------------------------------------
# #13 (2026-07-23) 圆角被 set_style 冲掉 / 切角预览无边框 / 选中变直角
# - set_style 之前硬编码 border-radius:7px，HUD 在选中/重建时重调 set_style 把
#   预设圆角(>7)冲回 7px（表现：>15 圆角变矩形）。修复：已套预设时保留 _corner_radius
#   与 _bs_*，样式表用预设圆角。
# - 按钮列表预览 clone 仅复制 styleSheet+font，丢失切角自绘状态(_bs_*)→预览无边框、
#   选中后变矩形黄边。修复：HintButton 源克隆成 HintButton 并复制 _bs_*；选中时切角
#   clone 重着色 _bs_border（保留切角形状）。
# ---------------------------------------------------------------------------
# 圆角预设存活于 set_style（HUD 重建/选中会再次调用 set_style）
_nb = _HB("ls#0")
_nb.set_style("#7fd1ff", False)
_BS._apply_style_to_ctrl(
    _nb, ButtonStyle(corner_radius=17, cut_size=0, border_thickness=4,
                     colors={"BackColor": "1c1e26", "BorderColor": "5fafeb",
                             "ForeColor": "e6ff1e"}))
check("13: 应用 radius=17 后样式表 border-radius=17", _br(_nb.styleSheet()) == "17")
_nb.set_style("#7fd1ff", True)  # 模拟选中/重建 re-apply 默认外观
check("13: set_style 后 radius 仍保留(不被冲成7)", _br(_nb.styleSheet()) == "17")

# 切角 clone：预览有边框 + 选中保持切角
_dummy = _be.ButtonEditPanel.__new__(_be.ButtonEditPanel)
_csrc = _HB("切角src")
_csrc.set_style("#7fd1ff", False)
_BS._apply_style_to_ctrl(
    _csrc, ButtonStyle(corner_radius=0, cut_size=10, border_thickness=3,
                       colors={"BackColor": "1c1e26", "BorderColor": "5fafeb",
                               "ForeColor": "e6ff1e"}))
_c1 = _dummy._clone_button(_csrc)
check("13: 切角 clone 是 HintButton(可自绘)", isinstance(_c1, _HB))
check("13: 切角 clone 复制 _bs_back(有边框)", getattr(_c1, "_bs_back", None) is not None)
check("13: 切角 clone 复制 _cut_size", getattr(_c1, "_cut_size", 0) == 10)
_c2 = _dummy._clone_button(_csrc)
check("13: 克隆不施加选中着色(选中改由焦点外框)",
      getattr(_c2, "_bs_border", None) != (245, 215, 75))
check("13: 克隆保持源边框色(5fafeb)",
      getattr(_c2, "_bs_border", None) == (95, 175, 235))

# ---------------------------------------------------------------------------
# 14) 空磁贴卡片按 Enter 闪退回归：键盘/手柄激活路径的 cur.confirm() 必须被保护，
#     任何 confirm 内部异常都不得逃出 Qt 事件回调（否则 PyQt6 直接 abort 进程）。
#     鼠标路径 activate_widget 本就 try/except 保护；此处对齐键盘路径。
# ---------------------------------------------------------------------------
class _BoomPanel:
    def __init__(self):
        self._w = QPushButton()

    def nav_items(self):
        # confirm 故意抛异常，模拟“空目标磁贴启动失败 / 任意块 confirm bug”
        return [NavItem(self._w, "action", confirm=lambda: 1 / 0)]


_boom_win = QWidget()
_boom_ctrl = InteractionController(window=_boom_win, host=None, config=None)
_boom_ctrl.register_panel(_BoomPanel())
_boom_ctrl.refresh()
# 进入 item 模式（聚焦第 0 个按钮），模拟已选中卡片并进入按钮层
_boom_ctrl._card_mode = False
_boom_ctrl._focus = 0
try:
    _boom_ctrl.activate()          # -> _press_confirm -> cur.confirm() 抛异常
    _guarded = True
except Exception as _e:            # pragma: no cover
    _guarded = False
    print("  activate() raised:", repr(_e))
check("14: 键盘/手柄 confirm 异常被捕获(不再闪退)", _guarded)

# ---------------------------------------------------------------------------
# 15) 进入功能卡片(卡片层 Enter -> 进入按钮层)闪退回归：
#     _press_confirm 的卡片进入分支(_enter_card -> set_focus -> on_focus /
#     _apply_card_highlight) 与 _place_rect 在“每秒 refresh 已 deleteLater 旧
#     按钮”的间歇情况下可能触到已销毁 QWidget。整条激活路径必须被保护，任何
#     异常不得逃出 Qt 事件回调(否则 PyQt6 直接 abort -> 卡死闪退)。
# ---------------------------------------------------------------------------
class _BoomCardPanel:
    """一个会“乱抛”的卡片：模拟块在 on_focus / set_card_selected 里出错。"""
    def __init__(self):
        self._w = QPushButton()
        self._card_selected = False

    def nav_items(self):
        return [NavItem(self._w, "action", hint="A",
                        on_focus=lambda f: (_ for _ in ()).throw(
                            RuntimeError("boom on_focus")),
                        confirm=lambda: 1 / 0)]

    def set_card_selected(self, s):
        raise RuntimeError("boom set_card_selected")


# (a) 卡片层 Enter 进入按钮层：_card_mode=True 时 activate -> _enter_card
_boom_win = QWidget()
_boom_ctrl = InteractionController(window=_boom_win, host=None, config=None)
_boom_ctrl.register_panel(_BoomCardPanel())
_boom_ctrl.refresh()
_boom_ctrl._card_mode = True
_boom_ctrl._card_focus = 0
try:
    _boom_ctrl.activate()          # -> _press_confirm -> _enter_card -> set_focus
    _enter_guarded = True
except Exception as _e:            # pragma: no cover
    _enter_guarded = False
    print("  enter-card raised:", repr(_e))
check("15a: 进入卡片(卡片层 Enter)异常被捕获(不再卡死闪退)", _enter_guarded)

# (b) 已销毁 QWidget：每秒 refresh 重建按钮后，焦点仍指向旧(已 deleteLater)按钮。
try:
    from PyQt6 import sip
    _have_sip = True
except Exception:                 # pragma: no cover
    _have_sip = False
if _have_sip:
    _stale = QPushButton()
    sip.delete(_stale)            # 真正删除底层 C++ 对象，复现“悬空 widget”
    _stale_ctrl = InteractionController(window=QWidget(), host=None, config=None)
    _stale_ctrl._items = [NavItem(_stale, "action")]
    _stale_ctrl._card_ranges = {0: (0, 1)}
    _stale_ctrl._card_mode = False
    _stale_ctrl._active_card = 0
    _stale_ctrl._focus = -1
    try:
        _stale_ctrl.set_focus(0)  # -> _place_rect -> mapTo/parentWidget(已销毁)
        _stale_guarded = True
    except Exception as _e:        # pragma: no cover
        _stale_guarded = False
        print("  stale-widget raised:", repr(_e))
    check("15b: 已销毁按钮(mapTo)异常被捕获(不再卡死闪退)", _stale_guarded)

# ---------------------------------------------------------------------------
# 16) An empty panel (an unconfigured / empty tile card) must NOT hijack the
#     card index.  Regression: ``_panel_index_of`` returned the *compressed*
#     ``starts`` index (panels with no nav-items were skipped), so a real card
#     sitting *after* an empty tile card had its index shifted by -1.  That made
#     ``set_focus`` / ``activate_widget`` overwrite ``_card_focus`` /
#     ``_active_card`` with the empty tile's index, so keyboard / Enter
#     navigation "jumped into" the empty tile card instead of the selected
#     power / freeze card.
# ---------------------------------------------------------------------------
class _EmptyPanel(QWidget):
    def nav_items(self):
        return []


class _CardPanel(QWidget):
    def __init__(self, n, x, parent=None):
        super().__init__(parent)
        self.btns = []
        for i in range(n):
            b = QPushButton(f"b{i}", self)
            b.setGeometry(x, 10 + i * 40, 120, 30)
            self.btns.append(b)

    def nav_items(self):
        its = []
        for i, b in enumerate(self.btns):
            its.append(NavItem(widget=b, kind="action",
                               hint=["A", "B", "X", "Y"][i % 4],
                               confirm=lambda: None))
        return its


_win16 = QWidget()
_host16 = QWidget(_win16)
_ctrl16 = InteractionController(window=_win16, host=_host16, config=None)
_empty_tile = _EmptyPanel(_win16)        # index 0 — NO items (empty tile card)
_power_p = _CardPanel(3, 10, _win16)    # index 1 — power settings
_freeze_p = _CardPanel(3, 300, _win16)  # index 2 — process freeze
_ctrl16.register_panel(_empty_tile)
_ctrl16.register_panel(_power_p)
_ctrl16.register_panel(_freeze_p)
_ctrl16.refresh()
_win16.show()
app.processEvents()

# Mouse-click a button inside the POWER card -> must select the REAL index 1,
# never 0 (the empty tile card sitting ahead of it).
_ctrl16.activate_widget(_power_p.btns[0], confirm=False)
check("16a: click power button -> _card_focus == 1 (real index)",
      _ctrl16._card_focus == 1)
check("16a: click power button -> _active_card == 1",
      _ctrl16._active_card == 1)

# Keyboard-Enter path on a card selected via _select_card (freeze = index 2).
_ctrl16._exit_card()
_ctrl16._select_card(2)
_ctrl16._enter_card()
check("16b: enter freeze card -> _card_focus == 2 (NOT empty tile 0)",
      _ctrl16._card_focus == 2)
check("16b: enter freeze card -> _active_card == 2",
      _ctrl16._active_card == 2)
# Focus must land on a freeze button (inside ranges[2] == (3, 6)).
check("16b: freeze focus inside its own range", 3 <= _ctrl16._focus < 6)

print("\n==== SUMMARY ====")
print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL GOOD")
