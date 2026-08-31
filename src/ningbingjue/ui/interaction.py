"""Unified interaction layer for the HUD.

One controller owns the selection / focus model for *every* interactive control
across all blocks, pages, buttons, sliders and inputs — replacing the previous
per-panel signal soup (each panel wiring its own ``clicked`` / ``valueChanged``
lambdas) with a single, consistent navigation + activation logic.

Interaction model (mirrors the original PS1 control-focus + matrix navigation):

* A single focus rectangle (``_FocusRect``) is drawn over the focused control,
  identical in style for buttons, sliders and inputs — so there is exactly one
  "you are here" indicator for the whole HUD.
* Arrow keys move the focus through a 2-D matrix of all controls in visual
  order.  When the focused control is a **slider**, the arrows instead nudge its
  value (the original "you are on the slider → arrows adjust it" behaviour);
  only when the slider is already at its bound do the arrows fall back to
  ordinary navigation.
* ``Tab`` / ``Shift+Tab`` step focus linearly; ``Enter`` / ``Space`` confirm the
  focused control; ``Esc`` clears focus; ``PageUp`` / ``PageDown`` jump between
  panels (zones / pages).
* Mouse clicks focus **and** confirm the same control, so keyboard and mouse
  share one activation path.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QPoint, Qt, QTimer, pyqtSignal, QEvent
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QFrame, QScrollArea, QWidget
from PyQt6 import sip

from .gamepad_hint import HINT_COLORS, draw_hint_auto
from ..core.shortcuts import _DEFAULT_SYSTEM, LONG_PRESS_MS
from ..core import profile  # opt-in perf profiler (NBJ_PROFILE); no-op when disabled

# Focus indicator: solid border + tinted background so the focused control is
# unmistakably highlighted.  The rect is raised above sibling widgets so it
# renders ON TOP of buttons (not below).  Colour + mask opacity are now
# user-configurable (AppConfig.focus_color / focus_opacity) instead of the
# hard-coded lime-yellow below — see :func:`build_focus_style`.
def build_focus_style(color_hex: str, opacity: int) -> str:
    """Build the focus-rectangle stylesheet from a ``rrggbb`` colour and a
    0..100 fill opacity.

    The border stays solid (full alpha) so the selected element is always
    framed; ``opacity`` only controls the tinted *mask* that sits inside the
    frame.  ``opacity == 0`` → no mask, just the border (the "only a frame"
    look the user asked for).
    """
    op = max(0, min(100, int(opacity)))
    # P0-1 fix: validate hex legality, not just length.  A 6-char-but-non-hex
    # string (e.g. "gghhii" / "12 34" / "zzzzzz") used to crash int(..., 16)
    # here, and since build_focus_style runs in _FocusRect.__init__ and
    # InteractionController.__init__ (both without try/except), the exception
    # bubbled up to AppWindow construction and killed the entire app startup.
    # Now: any malformed hex falls back to the default lime-yellow, and the
    # app always boots.
    hexv = (str(color_hex).strip() or "e6ff1e")[:6].lower()
    if len(hexv) != 6 or any(c not in "0123456789abcdef" for c in hexv):
        hexv = "e6ff1e"
    alpha = op / 100.0
    r = int(hexv[0:2], 16)
    g = int(hexv[2:4], 16)
    b = int(hexv[4:6], 16)
    return (
        f"QFrame {{ background: rgba({r},{g},{b},{alpha:.3f});"
        f" border: 2px solid #{hexv}; border-radius: 12px; }}"
    )


# Default focus indicator (used as a fallback before config loads, and as the
# historical lime-yellow look).  Replaced at runtime by InteractionController
# from AppConfig.
_FOCUS_STYLE = build_focus_style("e6ff1e", 25)


def _widget_alive(w: Any) -> bool:
    """Return True only if ``w`` is a still-living QWidget.

    A widget that was removed by a layout rebuild / page switch is
    ``deleteLater()``'d: its C++ object can be torn down on the next event
    loop tick.  Touching such a widget (``mapTo`` / ``setGeometry`` /
    ``parentWidget``) is undefined behaviour in PyQt6 and aborts the whole
    process with a native access violation — there is no Python traceback and
    no way to catch it with ``try/except``.  The gamepad poll runs on the main
    thread but still races the per-second refresh() and the synchronous
    rebuild() a page switch triggers, so every widget touch on the navigation
    path must be guarded with this check first.
    """
    if w is None or not isinstance(w, QWidget):
        return False
    try:
        return not sip.isdeleted(w)
    except Exception:
        return False


class NavItem:
    """A single navigable control wrapped for the unified interaction model.

    ``confirm`` runs when the control is activated (Enter / click).  ``nudge``
    (sliders / steppers) changes the value by ``delta`` sign and returns
    ``True`` if the value actually changed.  ``on_focus`` is an optional hook
    fired with the focus state so a block can keep its own highlight in sync.
    """

    __slots__ = ("widget", "kind", "confirm", "nudge", "on_focus", "hint",
                 "row", "col", "card_token", "action_y", "action_y_gp",
                 "action_y_dbl", "action_x", "no_bulk_hint")

    def __init__(
        self,
        widget: QWidget,
        kind: str,
        confirm: Callable[[], None] | None = None,
        nudge: Callable[[int], bool] | None = None,
        on_focus: Callable[[bool], None] | None = None,
        hint: str = "",
        row: int | None = None,
        col: int | None = None,
        card_token: str | None = None,
        action_y: Callable[[], None] | None = None,
        action_y_gp: Callable[[], None] | None = None,
        action_y_dbl: Callable[[], None] | None = None,
        action_x: Callable[[], None] | None = None,
        no_bulk_hint: bool = False,
    ) -> None:
        self.widget = widget
        self.kind = kind  # 'action' | 'toggle' | 'select' | 'slider' | 'input'
        self.confirm = confirm or (lambda: None)
        self.nudge = nudge
        self.on_focus = on_focus
        # Optional *per-item* secondary action fired by the Y button / W key
        # when this item is the focused one (e.g. freeze candidate: Y = 标为游戏).
        # Distinct from ``hint`` (which drives the card-level shortcut scan):
        # an item with ``action_y`` is meant to be triggered only while focused,
        # never by a Y press landed on a sibling control in the same card.
        #  - ``action_y``   : plain toggle (keyboard W always uses this).
        #  - ``action_y_gp``: gamepad Y single press (defaults to ``action_y``
        #                    when None).  Lets the gamepad Y behave differently
        #                    from W — e.g. freeze rows: single Y = 加入, 双击 Y = 移出.
        #  - ``action_y_dbl``: gamepad Y *double* press on the same focused item
        #                    (see InteractionController._fire_hint).  Optional.
        self.action_y = action_y
        self.action_y_gp = action_y_gp
        self.action_y_dbl = action_y_dbl
        # Optional *per-item* action fired by the X button / Backspace key when
        # this item is the focused one (e.g. freeze row: X = 关闭该进程).  Like
        # ``action_y`` it is only reachable while the item is focused and never
        # by an X press landed on a sibling control.  The global X meaning
        # (card-level "delete" hint scan) still applies to items that expose no
        # ``action_x`` — see InteractionController._fire_hint.
        self.action_x = action_x
        # Gamepad-hint badge letter (A/B/X/Y) shown on the focus rectangle when
        # this control is focused — exactly the original's "show hint on focus"
        # behaviour.  Empty string means no badge.
        self.hint = hint
        # Optional 2-D matrix coordinates (row, col).  When BOTH are not None
        # the controller routes arrow keys through the explicit matrix
        # (rows = cards, cols = button slots) instead of the geometric nearest
        # neighbour fallback.  ``None`` = not part of a grid (default).
        self.row = row
        self.col = col
        # Stable card identity (block-type token) used by the layout editor so
        # the navigation cursor can follow a card across a reorder (a move swaps
        # two rows, which would otherwise leave the cursor on the wrong card).
        self.card_token = card_token
        # When True, the InteractionController must NOT push this item's hint
        # onto its widget in bulk — the owning panel manages the hint itself
        # (e.g. freeze process buttons show Y only on the selected row).
        self.no_bulk_hint = no_bulk_hint


class _FocusRect(QFrame):
    """The single HUD focus indicator, overlaid on the focused control.

    It is a child of the scrollable content widget (not the top-level window),
    so it scrolls together with the HUD and never paints over a modal dialog.
    It is also transparent to mouse events so it never steals clicks.

    When the focused control carries a gamepad hint (``NavItem.hint`` in
    A/B/X/Y), the same badge is painted on this rect — using the identical
    auto layout as ``RoundedButton`` (:func:`draw_hint_auto`) so the two never
    double up and the badge always matches the button underneath.
    """

    def __init__(self, parent: QWidget, color_hex: str = "e6ff1e",
                 opacity: int = 25) -> None:
        super().__init__(parent)
        self.setStyleSheet(build_focus_style(color_hex, opacity))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._hint = ""
        self.hide()

    def set_hint(self, hint: str) -> None:
        if hint != self._hint:
            self._hint = hint
            self.update()

    def paintEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        super().paintEvent(ev)
        if self._hint not in HINT_COLORS:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Same auto layout as RoundedButton → always matches the button beneath.
        draw_hint_auto(p, self._hint, self.width(), self.height())
        p.end()


class InteractionController(QObject):
    # Gamepad requests the overlay (edit mode) can fulfil: save / cancel / delete.
    request_save = pyqtSignal()
    request_cancel = pyqtSignal()
    request_delete = pyqtSignal()

    def __init__(self, window: QWidget, host: QWidget | None = None,
                 gamepad: Any = None, config: Any = None) -> None:
        super().__init__(window)
        self._win = window
        # Shared AppConfig (layout.json).  Used to seed + live-update the focus
        # rectangle's colour/opacity.  May be None in headless/test contexts.
        self._cfg = config
        # ``host`` is the scrollable content widget.  The focus rectangle is a
        # child of it, so it scrolls *with* the content (never desyncs when the
        # page moves) and naturally sits *below* any top-level modal dialog
        # (which used to paint over it).  Falls back to the window itself.
        # When the in-window edit overlay is open we switch the host to the
        # overlay's content widget so the same focus rect tracks edit controls
        # too — see :meth:`set_host`.
        self._host = host if host is not None else window
        self._panels: list[Any] = []
        self._items: list[NavItem] = []
        self._panel_starts: list[int] = []  # per REAL panel index: first nav-item index, or -1 if that panel has no items
        self._focus: int = -1
        # 焦点框"待重渲染"标志：由 _schedule_focus_sync() 置位，下一帧统一
        # 执行一次 _sync_focus_rect()（见该方法的说明）。
        self._focus_sync_pending: bool = False
        # 2-D matrix navigation (layout editor).  ``_grid_rows[row][col]`` maps
        # to a nav-item index; rebuilt every refresh() from items that carry
        # both (row, col).  Empty for panels that don't use the matrix.
        self._grid_rows: dict[int, dict[int, int]] = {}
        self._grid_max_row: int = -1
        fc = getattr(config, "focus_color", "e6ff1e") if config else "e6ff1e"
        fo = getattr(config, "focus_opacity", 25) if config else 25
        self._rect = _FocusRect(self._host, fc, fo)
        # 已挂滚动追踪的滚动条集合（按 id 去重），避免重复 connect。
        # 焦点框默认是滚动内容 host 的子 widget，理论上随内容一起滚；但编辑器
        # 内存在嵌套 QScrollArea，聚焦控件在嵌套滚动区里时，其相对 host 的位置会
        # 随嵌套滚动改变，而 rect（挂在 host 上）不会自动跟随 → 滚动时脱节/滞后
        # 直到下一次 1s refresh 才自我纠正。故把聚焦控件所在的所有滚动条的
        # valueChanged 连到 _reposition_on_scroll，滚动时连续重摆 rect（见
        # _connect_scroll_tracking / _reposition_on_scroll）。
        self._tracked_scrolls: set = set()

        # -- edit mode (in-window overlay) --------------------------------
        self._edit_overlay: Any = None
        self._edit_mode: bool = False

        # -- gamepad -------------------------------------------------------
        self._gamepad = gamepad
        self._gp_timer = QTimer(self)
        self._gp_timer.timeout.connect(self._poll_gamepad)
        self._gp_prev: Any = None  # previous GamepadState (edge detection)
        self._gp_dir: tuple[int, int] = (0, 0)
        self._gp_repeat_at: float = 0.0
        # 键盘方向键自动重复节流（与手柄 _gp_repeat_at 同思路，详见 handle_key）。
        # 长按方向键时 OS 以 ~30 次/秒投递 keyPressEvent，每个都独立移动 →
        # UI 线程事件堆积 → 光标掉帧。限到 ~11 次/秒，首按立即移动、轻点不吞。
        self._kb_repeat_at: float = 0.0
        self._KB_REPEAT_INTERVAL_S: float = 0.09
        # 布局编辑页两层导航：是否已进入卡片内层（位置按钮 ↑/↓/▲/▼）。
        # False = 外层（焦点锁在整行 col 0，方向键只在卡片间上下切）。
        self._edit_inner: bool = False
        # 设备互斥（底部提示）：当前输入源，mouse=鼠标/键盘、gamepad=手柄。
        # 默认 mouse（桌面最常用），任意手柄动作切成 gamepad。
        self._input_mode: str = "mouse"
        self._gp_active: bool = False  # True after FIRST gamepad input
        self._gp_connected_prev: bool = False  # for connection-status edges
        self._gp_had_disconnect: bool = False  # a real disconnect happened since last connect
        self._gp_power_prev: int = -1  # last SDL power tier (edge-detect recolor)
        # Rebuild guard: set True while a page / panel rebuild is deleting and
        # recreating widgets.  The 30ms gamepad poll and the keyboard handler
        # early-return while this is set, so a rebuild's delete -> create ->
        # repaint of the translucent frameless window can never be touched
        # mid-flight (the "switch page via gamepad -> silent crash" race).
        # Owned by the host window's rebuild() via set_rebuilding().
        self._rebuilding: bool = False
        # Per-action dispatch timestamps (monotonic seconds).  Used to coalesce
        # a single physical press that the SDL backend surfaces as two rising
        # edges — e.g. Bluetooth/RF button chatter, or a controller that
        # re-indexes (hot-plug blip) while a button is held.  See
        # :meth:`_gp_action_allowed`.  A deliberate second press is always
        # >100 ms apart, so a 50 ms window never bites a human.
        self._gp_last_action_at: dict[str, float] = {}
        self._GP_ACTION_DEBOUNCE_S: float = 0.05
        # Gamepad-Y double-press detection (for per-item ``action_y_dbl``).
        # Two Y presses within this window on the *same* focused NavItem fire
        # ``action_y_dbl`` (e.g. freeze row: 移出白名单) instead of re-firing the
        # single-press action.  Must exceed ``_GP_ACTION_DEBOUNCE_S`` (50 ms) so a
        # real human double-click (always >100 ms apart) is never coalesced away.
        self._Y_DOUBLE_S: float = 0.4
        self._last_y_item: Any = None
        self._last_y_t: float = 0.0
        # 布局编辑页 Y/X 长按分流状态。``_gp_long_press[btn] = {"t": 按下时刻(单调秒),
        # "fired": 长按是否已触发}``。仅 _edit_mode 下使用：Y 短按=进入该行编辑项、
        # Y 长按=保存布局（全局）、X 长按=隐藏该行卡片、X 短按=无动作。阈值复用
        # core.shortcuts.LONG_PRESS_MS(500ms)。
        self._gp_long_press: dict[str, dict] = {}
        # Optional callbacks wired by the host window:
        #   on_connection_changed(connected: bool, name: str) -> update the
        #     HUD's gamepad-status indicator / tray tooltip.
        #   on_goto_page(index: int) -> jump to an absolute top-level page
        #     (driven by number keys 1..N on the keyboard).
        self.on_connection_changed: Callable[..., None] | None = None
        self.on_goto_page: Callable[[int], None] | None = None
        #   on_gamepad_battery(level: int) -> SDL power tier changed; the HUD
        #     recolours the gamepad glyph green -> red.  Fires only on change.
        self.on_gamepad_battery: Callable[..., None] | None = None
        # Fired when the focused feature card (panel) changes on the main page
        # (old_panel, new_panel).  The host uses it to dismiss a per-card help
        # popup so it never lingers after the user moves to another card.
        self.on_card_focus_change: Callable[[int, int], None] | None = None

        # -- two-layer card / button navigation (HUD mode) --------------
        #   card_mode=True  -> arrows / D-pad move between FUNCTION CARDS
        #                      (BlockPanels); the selected card shows its
        #                      theme-colour border (set_card_selected).
        #   card_mode=False -> arrows move between the selected card's buttons;
        #                      the focused button shows the focus rectangle.
        #   A / Enter  -> enter the card (focus its A-hinted button) / confirm.
        #   B / Esc    -> exit back to card mode.
        #   Y / X      -> direct shortcuts to the card's hinted buttons.
        #   LB / RB    -> switch top-level page.
        self._card_mode: bool = True
        self._card_focus: int = -1          # index into self._panels (selected card)
        self._active_card: int = -1         # card whose buttons / hints are active
        self._card_ranges: dict[int, tuple[int, int]] = {}
        # Slider double-confirm: two A / Enter presses before arrows slide it.
        self._slider_press_count: int = 0
        self._slider_armed: bool = False
        # Page-switch callbacks (wired by the host window to app.switch_page).
        self.on_prev_page: Callable[[], None] | None = None
        self.on_next_page: Callable[[], None] | None = None

        # Optional help callback: fired on the L3 (left stick press) rising edge
        # so the HUD can pop its "疑惑解答" without the controller knowing about
        # the FAQ module — keeps the two decoupled.  Set by _OverlayWindow:
        #   self._controller.on_help = self._faq.toggle_for_current
        self.on_help: Callable[[], None] | None = None

        # Shell-level gamepad actions wired by the host window:
        #   on_toggle_layout -> Select toggles the layout editor in/out.
        #   on_focus_window  -> Start raises + focuses the app window.
        self.on_toggle_layout: Callable[[], None] | None = None
        self.on_focus_window: Callable[[], None] | None = None
        # Optional shortcut-button engine (gamepad + keyboard triggers, long/
        # short press).  Wired by the host; may stay None (no shortcuts).
        self.shortcut_engine: Any = None
        # User-customizable SYSTEM (shell) bindings: which gamepad button opens
        # the layout editor / focuses the window / brings the window to front.
        # Seeded with the original hard-coded defaults (Select / Start /
        # Win+Ctrl+F8); the host overrides them from ShortcutStore.system.
        self._sys_bindings: dict[str, dict[str, Optional[str]]] = {
            k: dict(v) for k, v in _DEFAULT_SYSTEM.items()
        }
        # Gamepad "record" capture callback (armed by the shortcut editor while
        # the user is recording a gamepad button).  When set, the next pressed
        # button is handed to it and normal dispatch is skipped for that poll.
        self._gp_capture: Optional[Callable[[str], None]] = None

    # -- registration ----------------------------------------------------
    def register_panel(self, panel: Any) -> None:
        if panel not in self._panels:
            self._panels.append(panel)

    def set_edit_overlay(self, overlay: Any) -> None:
        """Register the in-window edit overlay as the navigation source while
        edit mode is active (it replaces the 6 block panels)."""
        self._edit_overlay = overlay

    def set_host(self, widget: QWidget) -> None:
        """Reparent the focus rectangle so it tracks the currently-visible
        content (scroll area in HUD mode, edit overlay in edit mode)."""
        old = self._host
        self._host = widget
        # 根治：reparent 前先把焦点框彻底归零（隐藏 + 几何 0 + 清 hint），
        # 这样即使后续 refresh()/_place_rect 因竞态在错位宿主坐标系上计算，
        # 也只会拿到 0 几何，绝不残留旧页长条黄框。
        self._hide_focus_rect()
        self._rect.setParent(widget)
        # 在宿主上安装事件过滤器：宿主一发生布局请求 / 尺寸变化 / 重新显示，
        # 就自动标脏并在下一帧重渲染焦点框（见 eventFilter）。这样任何未来
        # 新增的功能路径都自动获得"布局变了焦点框就跟"的兜底，无需作者记得
        # 手动同步 —— 这是彻底终结"越修越多"的关键一环。
        try:
            if old is not None and old is not widget:
                try:
                    old.removeEventFilter(self)
                except Exception:
                    pass
            widget.installEventFilter(self)
        except Exception:
            pass
        # 半透明无边框窗口上 update() 只调度下一次绘制，DWM 已合成的当前帧
        # 仍含旧黄框——对旧宿主 repaint() 擦掉旧父控件上的像素，再对新宿主
        # repaint() 把整块区域立刻重绘，避免「页面已切/编辑器已退出，黄框还在」。
        try:
            if old is not None and old is not widget:
                old.repaint()
            widget.repaint()
        except Exception:
            pass

    def _hide_focus_rect(self) -> None:
        """唯一、彻底的焦点框清理：隐藏 + 几何归零 + 清提示文案。

        所有「结构变化」（切页 / 进出编辑器 / reparent / 重建 / 退出条目模式）
        都应调它而非裸 ``self._rect.hide()``，确保焦点框**永不保留可显示的
        旧几何**——旧尺寸正是「退出编辑器后页面上出现长条黄框」的根因。"""
        try:
            self._rect.hide()
            self._rect.setGeometry(0, 0, 0, 0)
            self._rect.set_hint("")
        except Exception:
            pass

    # -- system (shell) bindings -----------------------------------------
    def set_system_bindings(self, bindings: dict) -> None:
        """Replace the system-binding table (gamepad button per system action).

        ``bindings`` is the ShortcutStore.system dict: keys are logical actions
        (``open_layout_editor`` / ``focus_window`` / ``bring_to_front``), values
        are ``{"gamepad": ..., "keyboard": ...}`` dicts.  The outer dict is
        copied so later in-place edits to the store's inner dicts still take
        effect (the inner dicts are shared references)."""
        if isinstance(bindings, dict):
            self._sys_bindings = {k: (dict(v) if isinstance(v, dict) else {})
                                  for k, v in bindings.items()}

    def _sys_gamepad(self, action: str) -> Optional[str]:
        v = self._sys_bindings.get(action)
        if isinstance(v, dict):
            return v.get("gamepad")
        return None

    # -- gamepad record capture ------------------------------------------
    def set_gamepad_capture(self, cb: Optional[Callable[[str], None]]) -> None:
        """Arm (or clear, with ``None``) a one-shot callback that receives the
        next pressed gamepad button.  Used by the shortcut editor's 'record'
        mode.  The callback fires at most once and auto-clears."""
        self._gp_capture = cb

    def get_gamepad_capture(self) -> Optional[Callable[[str], None]]:
        return self._gp_capture

    def set_focus_style(self, color_hex: str, opacity: int) -> None:
        """Live-update the focus rectangle's colour + mask opacity and persist
        them onto the shared config object (so a later ``save()`` writes them)."""
        if self._cfg is not None:
            self._cfg.focus_color = str(color_hex).strip() or "e6ff1e"
            self._cfg.focus_opacity = int(opacity)
        self._rect.setStyleSheet(build_focus_style(color_hex, opacity))
        self._rect.update()

    def refresh_focus_style(self) -> None:
        """Re-read the focus colour/opacity from the shared config (e.g. after a
        ``layout.json`` reload) and re-apply to the live rectangle."""
        if self._cfg is None:
            return
        self._rect.setStyleSheet(
            build_focus_style(self._cfg.focus_color, self._cfg.focus_opacity))

    def set_edit_mode(self, active: bool) -> None:
        """Toggle edit-mode aggregation (overlay only vs the 6 block panels)."""
        self._edit_mode = active
        # 进入/退出编辑都强制回到外层（整行）视角：防止上次在内层(col 1..4)退出后
        # 残留 _edit_inner=True，导致下次进入编辑时外层左右方向键直接跳进位置按钮、
        # 破坏「必须先 A/Enter 才进内层」的两层导航。
        self._edit_inner = False
        self._focus = -1
        self._hide_focus_rect()
        self.refresh()

    def start_gamepad(self) -> None:
        """Begin polling the gamepad backend (no-op if none supplied)."""
        if self._gamepad is not None and not self._gp_timer.isActive():
            self._gp_timer.start(30)

    def stop_gamepad(self) -> None:
        """Stop the gamepad poll timer (called on window close / app quit)."""
        if self._gp_timer.isActive():
            self._gp_timer.stop()

    def reinit_gamepad(self) -> None:
        """Force the gamepad backend to drop stale handles and re-enumerate
        controllers.  Called by the host after the system suspends/resumes
        (hibernation) so the gamepad works again without restarting the app.

        The backend's :meth:`SDLGamepadBackend.reinit` closes every SDL
        controller handle and re-initializes the gamecontroller subsystem; until
        the OS re-enumerates the device, :meth:`poll` reports ``connected=False``
        and the existing disconnect/reconnect edge logic in :meth:`_poll_gamepad`
        re-seeds cleanly.  We also clear the edge-detection snapshot here so a
        button that was physically held across the suspend can never surface as
        a spurious action on the first post-resume poll.
        """
        gp = self._gamepad
        if gp is None:
            return
        fn = getattr(gp, "reinit", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
        # Drop edge-detection state so the next poll re-seeds from scratch and
        # never fires a spurious action from the pre-suspend button snapshot.
        self._gp_prev = None
        self._gp_connected_prev = False
        self._gp_had_disconnect = True

    def rebind(self) -> None:
        """Re-register panels after a layout rebuild.

        Only rebuilds the panel list (``self._panels``) and resets focus — the
        nav-item list (``self._items``) is refreshed by the sync bridge
        (``_SyncBridge.sync_all`` -> ``refresh``), which always runs right after a
        switch/edit, and continuously via the 1 s display timer.  Dropping the
        inline ``refresh()`` here collapsed the old triple nav-item rebuild
        (rebind + rebuild's finally + sync_all) down to a single one per switch.
        """
        self._panels = []
        for p in getattr(self._win, "panels", []) or []:
            self.register_panel(p)
        self._focus = -1
        # 切页后必须把导航状态拉回卡片模式并清除陈旧的活跃卡片索引。
        # 否则若用户切页前处于条目模式(_card_mode=False)，refresh() 的条目模式分支
        # 会用旧页 _active_card 索引去查新页卡片范围，把焦点框画到错误卡片上
        # （表现为「旧页延迟消失 + 新页越界残留」）。这等价于一次静默的 _exit_card，
        # 但走 rebind() 这一确切的切页重建收口点，避免与 nav.exit_item_mode 状态错配。
        self._card_mode = True
        self._active_card = -1
        self._card_focus = -1
        self._hide_focus_rect()
        self._apply_card_highlight()

    def set_rebuilding(self, flag: bool) -> None:
        """Block all navigation while a page / panel rebuild is in flight.

        The host window's :meth:`rebuild` sets this around the synchronous
        delete+create of every panel.  While True, :meth:`_poll_gamepad_impl`
        and :meth:`handle_key` return immediately — so the 30ms gamepad timer
        can never repaint (or touch) a half-built translucent frameless window,
        which is exactly the native-access-violation race that made page
        switches crash silently.  This is the deterministic counterpart to the
        ``singleShot(0)`` deferral already used in ``_on_page_switched``.
        """
        self._rebuilding = bool(flag)

    # -- aggregation ------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild the flat item list from every panel's ``nav_items()``.

        Focus is preserved by widget identity so dynamic lists (freeze /
        procmon / ls / tile) don't lose the selection on every repaint.

        IMPORTANT: this is a *content* refresh (the sync bridge calls it every
        second).  It must NOT fire ``on_focus`` callbacks — doing so would, for
        example, clear the freeze block's pending two-press ``highlight_key``
        on every tick and make freezing impossible.  ``on_focus`` only fires on
        a *real* focus change (arrow navigation / click), handled in
        :meth:`set_focus`.

        While the in-window edit overlay is active, only the overlay's items are
        aggregated (the 6 block panels are hidden behind it).
        """
        prev_widget: Optional[QWidget] = None
        if 0 <= self._focus < len(self._items):
            prev_widget = self._items[self._focus].widget
        prev_focus = self._focus  # index, kept across rebuilds for edit mode
        prev_items = self._items  # captured BEFORE reassignment below

        items: list[NavItem] = []
        starts: list[int] = []
        ranges: dict[int, tuple[int, int]] = {}  # panel index -> (start, end) in items

        if self._edit_mode and self._edit_overlay is not None:
            fn = getattr(self._edit_overlay, "nav_items", None)
            if callable(fn):
                sub = fn()
                if sub:
                    starts.append(0)
                    items.extend(sub)
        else:
            # NOTE: ``starts`` is keyed by the *real* panel index (one slot per
            # panel, -1 when that panel contributes no nav-items).  This keeps it
            # aligned with ``self._panels`` / ``self._card_ranges`` / the
            # ``_card_focus`` / ``_active_card`` fields — all of which use the
            # REAL panel index.  A previous version compacted ``starts`` (skipping
            # empty panels), which made ``_panel_index_of`` return a *different*
            # index space and sent keyboard / gamepad navigation into the wrong
            # card (e.g. an empty tile card sitting ahead of a real one).
            starts = [-1] * len(self._panels)
            for pi, p in enumerate(self._panels):
                # A page-switch rebuild that threw mid-way can leave a deleted
                # panel in ``_panels`` (rebind skipped).  Calling ``nav_items``
                # on it dereferences its child widgets -> native crash that no
                # try/except can catch, so skip dead widgets here.  Non-QWidget
                # panels (test fakes) are always safe.
                if isinstance(p, QWidget) and not _widget_alive(p):
                    ranges[pi] = (len(items), len(items))
                    continue
                fn = getattr(p, "nav_items", None)
                sub = fn() if callable(fn) else None
                if not sub:
                    sub = []
                ranges[pi] = (len(items), len(items) + len(sub))
                if sub:
                    starts[pi] = len(items)
                items.extend(sub)

        self._items = items
        self._panel_starts = starts
        self._card_ranges = ranges

        # Rebuild the 2-D matrix lookup for explicit grid navigation.  Only
        # items carrying both (row, col) coordinates participate; panels that
        # don't use the matrix (HUD, every other editor) simply leave
        # ``_grid_rows`` empty and arrow keys fall back to geometric nearest
        # neighbour in ``_move``.
        self._grid_rows = {}
        for _i, _it in enumerate(items):
            if _it.row is not None and _it.col is not None:
                self._grid_rows.setdefault(_it.row, {})[_it.col] = _i
        self._grid_max_row = max(self._grid_rows) if self._grid_rows else -1

        # Clamp card focus / active card to the (re)built panel set.
        if self._card_focus >= len(self._panels):
            self._card_focus = len(self._panels) - 1
        if self._active_card >= len(self._panels):
            self._active_card = len(self._panels) - 1

        # Push each item's gamepad hint onto its widget, but ONLY for the
        # active (selected) card — so the HUD shows the current card's button
        # shortcuts (A=加载 / Y=保存 / X=删除 …), not every card at once.
        # Gated on ``_gp_active`` so keyboard-only sessions stay clean.
        gated_hint = self._gp_active
        active = (self._card_focus
                  if (self._card_mode and not self._edit_mode) else self._active_card)
        arng = ranges.get(active) if active >= 0 else None
        for idx, it in enumerate(items):
            w = it.widget
            if not it.hint or not hasattr(w, "set_gamepad_hint") or it.no_bulk_hint:
                continue
            try:
                in_active = arng is not None and arng[0] <= idx < arng[1]
                wanted = it.hint if (gated_hint and in_active) else ""
                if getattr(w, "gamepad_hint", "") != wanted:
                    w.set_gamepad_hint(wanted)
            except Exception:
                pass

        # Re-apply the selected-card theme border after the (re)build.
        self._apply_card_highlight()

        if self._edit_mode:
            # Editor: a reorder (move up/down/cross-page) rebuilds the card rows,
            # so the flat focus index now points at a *different* card — the one
            # that slid into the old slot.  That is exactly the "cursor stays
            # put, gets replaced by the pushed-down card" bug.  Re-resolve the
            # focus by (card_token, col) so the cursor rides with the SAME card
            # it was on, keeping the same button slot (e.g. pressing ↑ again
            # keeps moving it up).  Header/footer buttons carry no card_token, so
            # they fall back to the flat-index restore — correct, since they
            # never reorder.  The host also calls refresh() on every panel sync,
            # so this keeps the cursor where the user left it between syncs too.
            new_focus = -1
            if 0 <= prev_focus < len(prev_items):
                prev_item = prev_items[prev_focus]
                tok = getattr(prev_item, "card_token", None)
                if tok is not None:
                    pcol = prev_item.col
                    for i, it in enumerate(items):
                        if getattr(it, "card_token", None) == tok and it.col == pcol:
                            new_focus = i
                            break
            if new_focus < 0 and 0 <= prev_focus < len(items):
                new_focus = prev_focus  # fallback: header/footer (no token)
            # Cold-start seed: entering the editor leaves _focus == -1 (set by
            # set_edit_mode), and edit-mode _apply_direction() bails out for
            # EVERY directional press while cur is None — so the first gamepad /
            # keyboard input does nothing and the focus cursor never appears
            # until a mouse click sets a focus (the bug the user reported).
            # Seed the first card (matrix row 1, col 0; falls back to index 0)
            # so navigation has a starting point.  Mirrors item mode's
            # `new_focus = rng[0]` seeding (interaction.py:647).  The cold-start
            # gate (`not self._gp_active`) still keeps the rectangle hidden until
            # the user actually navigates, so a pure-mouse session stays clean.
            if new_focus < 0 and self._items:
                new_focus = self._grid_rows.get(1, {}).get(0, 0)
            self._focus = new_focus
            # 显隐 + 几何统一交给唯一渲染出口（声明式推导，见 _sync_focus_rect）
            self._sync_focus_rect()
        elif self._card_mode:
            # Card mode: the selected card is highlighted by its border, not a
            # focus rectangle, so there is no single focused button here.
            self._focus = -1
            self._sync_focus_rect()
        else:
            # Item mode: keep focus on a button within the active card.
            rng = ranges.get(self._active_card) if self._active_card >= 0 else None
            new_focus = -1
            if rng is not None and prev_widget is not None:
                for i in range(rng[0], rng[1]):
                    if items[i].widget is prev_widget:
                        new_focus = i
                        break
            if new_focus < 0 and rng is not None and rng[1] > rng[0]:
                new_focus = rng[0]
            self._focus = new_focus
            self._sync_focus_rect()

        # ---- COLD-START SAFETY NET ----
        # If the user has never interacted via gamepad/keyboard, force
        # everything invisible regardless of which branch above ran.
        # This catches any path that might have slipped a show() past the
        # per-branch gates (e.g. async callback, timer, mouse event).
        if not self._gp_active:
            self._hide_focus_rect()
            # Also clear any card selection border that may have been applied.
            for p in self._panels:
                fn = getattr(p, "set_card_selected", None)
                if callable(fn):
                    try:
                        fn(False)
                    except Exception:
                        pass

        # ---- 最终渲染（唯一出口）----
        # 上面的分支只负责**算出焦点索引**，显隐与几何全部由 _sync_focus_rect()
        # 这一个出口根据当前真实状态推导：索引有效 + 控件存活 + 可见 + 属于
        # 当前宿主 + 用户确实用键盘/手柄 才显示，否则一律归零清理。
        #
        # 这取代了原先写在这里的手写兜底逻辑（那段代码与 _sync_focus_rect 完全
        # 等价，保留只会造成"两处真相源"）。同时因为宿主上装有事件过滤器
        # （set_host -> installEventFilter），任何布局变化都会在下一帧自动触发
        # 这里重渲染 —— 新增功能路径无需记得手动同步，也不可能残留。
        self._sync_focus_rect()

    # -- focus helpers ----------------------------------------------------
    def _current(self) -> Optional[NavItem]:
        if 0 <= self._focus < len(self._items):
            return self._items[self._focus]
        return None

    def _panel_index_of(self, item_index: int) -> int:
        # ``self._card_ranges`` is keyed by the *real* panel index (the same
        # index used by ``self._panels`` / ``_card_focus`` / ``_active_card``),
        # so invert it to recover that real index for an item position.  Never
        # use ``self._panel_starts`` here: that list is per-real-panel (-1 for
        # empty cards), and its *value* is a position, not an index — confusing
        # the two sent keyboard / gamepad navigation into the wrong card (e.g.
        # an empty tile card sitting ahead of a real one).
        for pi, rng in self._card_ranges.items():
            if rng[0] <= item_index < rng[1]:
                return pi
        return -1

    def _notify_card_focus_change(self, old_panel: int, new_panel: int) -> None:
        """Fire ``on_card_focus_change`` when the selected feature card actually
        moves to a *different* panel.

        Centralised so every card-selection path converges here: ``set_focus``
        (item-mode button navigation + mouse click on a card's button),
        ``_select_card`` (mouse click on a card body) and ``_move_card``
        (keyboard / gamepad arrow navigation between cards).  Guarded so the
        host never gets spammed while the in-window edit overlay is open.
        """
        if self._edit_mode or self.on_card_focus_change is None:
            return
        if old_panel < 0 or new_panel < 0 or new_panel == old_panel:
            return
        try:
            self.on_card_focus_change(old_panel, new_panel)
        except Exception:
            pass

    def set_focus(self, index: int, force_rect: bool = False) -> None:
        self._reset_slider()
        if index == self._focus:
            # 焦点未变：只需按最新几何重摆。仍走唯一渲染出口 —— _place_rect
            # 已不再负责 show()，若这里直接调它，手柄用户点击已聚焦项时焦点
            # 框会不显示（回归）。force 传给出口以便鼠标点击也能强制点亮。
            self._sync_focus_rect(keep_visible=True, force=force_rect)
            return
        old = self._current()
        old_panel = self._panel_index_of(self._focus)
        if old is not None and old.on_focus is not None:
            try:
                old.on_focus(False)
            except Exception:
                pass
        self._focus = index
        cur = self._current()
        if cur is not None and cur.on_focus is not None:
            try:
                cur.on_focus(True)
            except Exception:
                pass
        # 主页面：焦点切换到另一个功能卡片时，关闭其他卡片的疑惑解答弹窗，
        # 避免它显示旧卡片的内容。编辑覆盖层模式下不触发（由 _on_page_switched 处理）。
        new_panel = self._panel_index_of(self._focus)
        # Keep the highlighted block in sync with the focused interactive
        # element.  Every item-focus path (mouse click, keyboard, gamepad,
        # page/panel jump, ...) converges in set_focus, so this is the single
        # place that guarantees the block focus border (thickening) follows the
        # selected control into its owning card — even when the cursor jumps
        # straight across cards (e.g. PageUp/PageDown in item mode) and the
        # yellow focus rectangle would otherwise outrun the block border.
        if (new_panel >= 0 and not self._card_mode and not self._edit_mode
                and new_panel != self._active_card):
            self._active_card = new_panel
            self._card_focus = new_panel
            self._apply_card_highlight()
        self._notify_card_focus_change(old_panel, new_panel)
        # 编辑模式：键盘/手柄导航（非鼠标 force_rect）移动时清掉鼠标选中行高亮，
        # 只留 _FocusRect 这一唯一光标（① 单光标）。鼠标单击走 force_rect，保留选中。
        if self._edit_mode and not force_rect and self._edit_overlay is not None:
            try:
                self._edit_overlay._set_sel(None)
            except Exception:
                pass
        # 显隐 + 几何统一交给唯一渲染出口 _sync_focus_rect()。
        # ``force=force_rect``：鼠标点击选中行也强制显示，使纯鼠标用户能看到
        # 光标；手柄/键盘用户则由 _gp_active 决定（冷启动不显示光标）。
        self._sync_focus_rect(keep_visible=True, force=force_rect)

    def _place_rect(self, keep_visible: bool = False) -> None:
        """Reposition the focus rectangle onto the current control.

        ``keep_visible`` controls whether the page is *forced* to scroll so the
        focused control becomes visible.  Navigation (keyboard / gamepad /
        mouse ``set_focus``) passes ``True`` so the selected item is always on
        screen.  Passive repositioning (the per-second ``refresh()`` and the
        ``set_rect_visible`` re-show) passes ``False`` (or relies on the
        default) so scrolling the viewport away no longer yanks the page back
        to the focused control's height every snapshot.
        """
        try:
            cur = self._current()
            if cur is None:
                self._hide_focus_rect()
                return
            w = cur.widget
            if not _widget_alive(w):
                # The focused widget was deleted by a page switch / refresh
                # before this placement ran.  Touching it (mapTo / setGeometry)
                # would be a native crash, so just hide the rect and bail.
                self._hide_focus_rect()
                return
            # 归属校验（根治残留）：焦点控件必须属于当前宿主坐标系（祖先链含
            # self._host）。退出编辑器后若 _focus 仍指向旧 overlay 的控件，
            # mapTo 会算出错位长条——这里直接判死并清理，绝不画出旧几何。
            belongs = False
            p: Optional[QWidget] = w
            while p is not None:
                if p is self._host:
                    belongs = True
                    break
                p = p.parentWidget()
            if not belongs:
                self._hide_focus_rect()
                return
            # 挂上聚焦控件所在滚动区的滚动追踪：滚动时连续重摆 rect
            # （编辑器嵌套 QScrollArea 也能跟随，消除「滑动过程停滞」）。
            self._connect_scroll_tracking(w)
            # Only force the page to scroll when this placement originates from
            # an explicit navigation action.  The per-second refresh() must not
            # call this, otherwise scrolling the viewport away from the focused
            # card gets undone within ~1s.
            if keep_visible:
                self._ensure_visible(w)
            # Map into the scrollable content's coordinate space (the rect's
            # parent) so that, when the page scrolls, the rect moves with it.
            pos = w.mapTo(self._host, QPoint(0, 0))
            pad = 3
            self._rect.setGeometry(
                pos.x() - pad, pos.y() - pad, w.width() + 2 * pad, w.height() + 2 * pad
            )
            # Slider: until it is ARMED (two A / Enter), show an "A" prompt on
            # the rect so the user knows a second press is needed before it
            # slides.
            if cur.kind == "slider":
                # "A" = press to enter control mode; "B" = press to exit it.
                self._rect.set_hint("B" if self._slider_armed else "A")
            else:
                # Show the focused control's gamepad-hint badge.  If the control
                # already advertises its hint persistently (RoundedButton /
                # HintButton), the focus rectangle stays clean to avoid a
                # doubled badge.
                widget_shows = getattr(cur.widget, "show_gamepad_hint", False)
                self._rect.set_hint(cur.hint if (cur.hint and not widget_shows) else "")
            self._rect.raise_()  # ensure focus rect is painted ON TOP of sibling widgets
            # 注意：这里**不再** show()。显隐一律由唯一渲染出口
            # _sync_focus_rect() 决定；_place_rect 只负责算几何与提示徽章。
            # 这样滚动重摆（_reposition_on_scroll）不会把一个本该隐藏的框
            # 又点亮，也不会让"是否可见"散落在多个方法里。
        except Exception:
            # ``cur.widget`` can be a QWidget that the per-second refresh() has
            # already deleteLater()'d (e.g. a tile button rebuilt between the
            # refresh and this keypress).  Touching a destroyed widget
            # (mapTo / setGeometry) is undefined behaviour that can freeze or
            # abort PyQt6 — never let it escape the navigation call.
            try:
                self._hide_focus_rect()
            except Exception:
                pass

    def set_rect_visible(self, visible: bool) -> None:
        """Show / hide the focus rectangle (e.g. hide it while a modal
        layout-editor dialog is open so it never paints over the dialog)."""
        if visible:
            self._sync_focus_rect()
        else:
            self._hide_focus_rect()

    def _sync_focus_rect(self, keep_visible: bool = False,
                         force: bool = False) -> None:
        """焦点框的**唯一渲染出口**（声明式）。

        焦点框"该不该显示、显示在哪"完全由**当前真实状态推导**，而不是由调用
        方命令式地 show / hide：

            可见 = (手柄/键盘已激活 或 鼠标强制 force)
                  且 焦点索引有效
                  且 控件存活、可见、且属于当前宿主坐标系

        任何一条不满足 → 一律归零清理（隐藏 + 几何 0 + 清 hint）。因此
        **"残留"在架构上不可能存在**：新增任何功能路径（翻页、动画、滚动、
        新编辑器）都**不需要记得手动清理焦点框** —— 即便它什么都不做，
        下一帧也会被 _schedule_focus_sync 的自动钩子纠正（见下）。

        ============================ 约定 ============================
        除本函数与 _place_rect 内部以外，任何地方都不得直接操作
        self._rect 的 show()/hide()。要改变焦点框，就调用本函数。
        =============================================================
        """
        try:
            if not (self._gp_active or force) or self._host is None:
                self._hide_focus_rect()
                return
            if self._focus < 0:
                self._hide_focus_rect()
                return
            cur = self._current()
            if cur is None:
                self._hide_focus_rect()
                return
            w = cur.widget
            if not _widget_alive(w) or not w.isVisible():
                self._hide_focus_rect()
                return
            # 宿主归属：控件必须属于当前宿主坐标系（祖先链含 self._host），
            # 否则 mapTo 会算出错位几何（退出编辑器后指向旧 overlay 控件时）。
            belongs = False
            p: Optional[QWidget] = w
            while p is not None:
                if p is self._host:
                    belongs = True
                    break
                p = p.parentWidget()
            if not belongs:
                self._hide_focus_rect()
                return
            self._place_rect(keep_visible=keep_visible)
            self._rect.show()
        except Exception:
            self._hide_focus_rect()

    def _schedule_focus_sync(self) -> None:
        """标记焦点框需要重新渲染，并在**下一帧**统一执行一次。

        这是"漏不掉"的关键兜底：任何会改变布局 / 导航 / 焦点的路径都可以
        （也应当）只调这里标脏，而不必关心焦点框该怎么画。即便某条新路径
        完全忘了调用，宿主上的事件过滤器（见 eventFilter）也会捕获布局变化
        并自动标脏，从而让焦点框在下一帧自我纠正。
        """
        if getattr(self, "_focus_sync_pending", False):
            return
        self._focus_sync_pending = True
        try:
            QTimer.singleShot(0, self._run_pending_focus_sync)
        except Exception:
            self._focus_sync_pending = False
            self._sync_focus_rect()

    def _run_pending_focus_sync(self) -> None:
        if not getattr(self, "_focus_sync_pending", False):
            return
        self._focus_sync_pending = False
        self._sync_focus_rect()

    def eventFilter(self, obj, event):  # noqa: N802 - Qt camelCase
        """监听宿主的布局变化，让焦点框自动自愈。

        宿主（主滚动区 / 编辑层）一旦发生布局请求、尺寸变化或重新显示，
        里面所有控件的几何都可能变（甚至被销毁重建）。这里捕获这些事件并
        标脏，使焦点框在下一帧按最新几何重新渲染 —— 任何未来新增的功能
        路径都自动获得这个兜底，无需作者记得手动同步焦点框。
        """
        try:
            if obj is self._host:
                et = event.type()
                if et in (QEvent.Type.LayoutRequest, QEvent.Type.Resize,
                          QEvent.Type.Show, QEvent.Type.ShowToParent):
                    self._schedule_focus_sync()
        except Exception:
            pass
        return False

    def _connect_scroll_tracking(self, widget: QWidget) -> None:
        """Connect every QScrollArea that contains ``widget`` to the focus-rect
        repositioner, so scrolling continuously tracks the focused control.

        The rect is a child of the scrollable content (``self._host``) and is
        therefore positioned in host coordinates.  When the focused widget lives
        inside a *nested* scroll area (the layout editor / sub-panels each wrap
        their body in their own QScrollArea), scrolling that nested area shifts
        the widget relative to the host — but the rect does not move with it,
        so it visibly lags until the next 1 s ``refresh()`` snaps it back.  By
        wiring each containing scroll bar's ``valueChanged`` to
        :meth:`_reposition_on_scroll`, the rect follows on every scroll tick.

        Connections are deduplicated by scroll-bar identity (``id()``) so calling
        this repeatedly (it runs on every placement) is cheap.
        """
        p: Optional[QWidget] = widget
        seen = set()
        while p is not None:
            if isinstance(p, QScrollArea):
                for bar in (p.verticalScrollBar(), p.horizontalScrollBar()):
                    if bar is None or id(bar) in self._tracked_scrolls:
                        continue
                    try:
                        bar.valueChanged.connect(self._reposition_on_scroll)
                        self._tracked_scrolls.add(id(bar))
                    except Exception:
                        pass
            # Don't scan past the scrollable content host: ancestors above it
            # (window / overlay frame) can never scroll the focused widget.
            if p is self._host:
                break
            nxt = p.parentWidget()
            if id(nxt) in seen:
                break
            seen.add(id(p))
            p = nxt

    def _reposition_on_scroll(self, _value: int = 0) -> None:
        """Scroll-event handler: re-pin the focus rect to the focused control.

        Passes ``keep_visible=False`` so scrolling the viewport never fights the
        user by yanking the page back to the focused control's height.
        """
        if self._focus < 0:
            return
        try:
            # 走唯一渲染出口而非 _place_rect：已可见时跟随滚动更新几何；若焦点框
            # 因故处于隐藏状态，只要条件重新满足（控件回到可视状态、用户仍用
            # 手柄/键盘），滚动就会把它重新点亮 —— 自愈，无需等 1 秒的 refresh。
            self._sync_focus_rect(keep_visible=False)
        except Exception:
            pass

    def _ensure_visible(self, w: QWidget) -> None:
        if not _widget_alive(w):
            return
        p = w.parentWidget()
        while p is not None:
            if isinstance(p, QScrollArea):
                p.ensureWidgetVisible(w, 6, 6)
                return
            p = p.parentWidget()

    # -- navigation -------------------------------------------------------
    def _apply_direction(self, dx: int, dy: int) -> bool:
        """Move focus (arrows / D-pad) or, when the focused control is a slider
        / the tile grid, nudge its value / position instead.

        In card mode (HUD) the arrows move between function cards instead of
        buttons.  A slider only nudges once it is ARMED (two A / Enter).

        Shared by the keyboard handler and the gamepad poll so both produce
        identical navigation.
        """
        # Card mode (HUD): arrows move between cards, not buttons.
        if not self._edit_mode and self._card_mode:
            return self._move_card(dx, dy)
        cur = self._current()
        if cur is not None and cur.nudge is not None and cur.kind in ("slider", "grid"):
            if cur.kind == "grid":
                # Grid nudge takes (dr, dc); only cardinal directions apply.
                moved = False
                if dx != 0 and dy == 0:
                    moved = cur.nudge(0, dx)
                elif dy != 0 and dx == 0:
                    moved = cur.nudge(dy, 0)
                if moved:
                    return True
                # at an edge (or diagonal) -> fall through to ordinary nav
            else:
                # Slider: arrows only nudge once it is ARMED (double-confirm).
                if self._slider_armed:
                    sign = 1 if (dx > 0 or dy > 0) else -1
                    if cur.nudge(sign):
                        return True
                # not armed -> fall through to ordinary navigation
        # Explicit 2-D matrix navigation (layout editor): if the focused item
        # carries (row, col) coordinates AND a grid was built, route arrows
        # through the matrix so they follow rows=cards / cols=buttons instead
        # of the flat 1-D sequence order.
        if (cur is not None and cur.row is not None and cur.col is not None
                and self._grid_rows):
            moved = self._apply_grid(dx, dy)
            if moved:
                return True
            if self._edit_mode:
                # 编辑模式纯矩阵导航：外层模式左右被锁、到边界都算「已处理」，
                # 绝不回退到几何 _move（否则左右会跳到相邻的位置按钮，破坏两层导航）。
                return False
            return self._move(dx, dy)
        # 矩阵网关未进入（焦点项无 row/col，或网格尚未构建）。
        # 编辑模式下：仅当「确实存在矩阵」（grid 非空）才吞掉方向键以锁定两层
        # 导航；若整个编辑器是扁平动作列表（如页面布局编辑），网格恒为空，
        # 原逻辑会把所有方向键都吞掉 -> 键盘/手柄完全无法移动焦点（用户报告的
        # 根因：所有编辑器的 nav_items 都未赋 (row,col)，网格从未构建）。此时
        # 必须回退几何 _move。HUD/项目模式始终几何回退。
        if self._edit_mode and self._grid_rows:
            return False
        return self._move(dx, dy)

    def _center(self, it: NavItem) -> QPoint:
        w = it.widget
        if not _widget_alive(w):
            # Widget was deleted by a rebuild / page switch — never touch a
            # dead widget (mapTo is a native crash).  Return a neutral origin;
            # callers only use it for relative direction scoring, so this is
            # harmless and keeps navigation alive.
            return QPoint(0, 0)
        pos = w.mapTo(self._win, QPoint(0, 0))
        return QPoint(pos.x() + w.width() // 2, pos.y() + w.height() // 2)

    def _move(self, dx: int, dy: int) -> bool:
        cur = self._current()
        # In item mode, only navigate within the active card's buttons.
        scope = None
        if not self._edit_mode and not self._card_mode and self._active_card >= 0:
            rng = self._card_ranges.get(self._active_card)
            if rng is not None:
                scope = range(rng[0], rng[1])
        if cur is None:
            if scope is not None:
                self.set_focus(scope[0])
                return True
            if self._items:
                self.set_focus(0)
                return True
            return False
        c0 = self._center(cur)
        best = -1
        best_score: Optional[float] = None
        for i, it in enumerate(self._items):
            if scope is not None and i not in scope:
                continue
            if i == self._focus:
                continue
            c = self._center(it)
            if dx > 0 and c.x() <= c0.x():
                continue
            if dx < 0 and c.x() >= c0.x():
                continue
            if dy > 0 and c.y() <= c0.y():
                continue
            if dy < 0 and c.y() >= c0.y():
                continue
            main = (c.x() - c0.x()) * dx + (c.y() - c0.y()) * dy
            perp = abs((c.x() - c0.x()) * dy - (c.y() - c0.y()) * dx)
            score = main + perp * 3.0
            if best_score is None or score < best_score:
                best_score = score
                best = i
        if best < 0:
            return False
        self.set_focus(best)
        return True

    def _apply_grid(self, dx: int, dy: int) -> bool:
        """Explicit 2-D matrix navigation used by the layout editor.

        Rows are cards (plus a header banner at row 0 and a footer banner at the
        last row); columns are the button slots within a row
        (编辑项 / ↑ / ↓ / ▲ / ▼, or the page-header / page-footer buttons).
        Arrow keys move by index math: Down/Up change the row keeping the same
        column, Left/Right change the column within the same row.  Rows may be
        jagged (the footer banner has fewer columns) — when moving vertically the
        target column is clamped to the destination row's width.  Diagonals are
        rejected (cardinal directions only).
        """
        cur = self._current()
        if cur is None or cur.row is None or cur.col is None:
            return False
        if dx == 0 and dy == 0:
            return False
        if dx != 0 and dy != 0:  # cardinal directions only
            return False
        # 布局编辑页外层（未进入卡片）：一律锁定在整行 col 0，方向键只能上下
        # 切行（页眉行0 → 卡片行1..N → 按钮/页脚行），左右全锁（用户 2026-08-30
        # 拍板「严格锁，贴近事故前」）——必须先按 A/Enter（或点行内按钮）进内层。
        # 落点即该行 col0 按钮（页眉=第1个动作钮、页脚=保存），A 可直接执行。
        if self._edit_mode and not self._edit_inner:
            if dx != 0:
                # 横幅按钮行（页眉 / 页脚 / 保存·取消 / 抽屉 等无 card_token 的行）：
                # 允许左右在同行按钮间移动焦点——这正是文档描述的「non-card 横幅
                # 特例」。此前被外层严格锁错误地一并锁死，导致页脚 主题与蒙版 /
                # 快捷键 / 排版效果 / 更多 等按钮手柄 / 键盘左右无法选中。
                # 卡片整行（带 card_token）仍严格锁左右，必须先 A/Enter 进内层
                # 才能选 ↑/↓/▲/▼，避免破坏两层导航。
                if cur.card_token is None:
                    tr = cur.row
                    tc = cur.col + dx
                    row_cols = self._grid_rows.get(tr)
                    if not row_cols or tc not in row_cols:
                        return False
                    idx = row_cols[tc]
                    if idx == self._focus:
                        return False
                    self.set_focus(idx)
                    return True
                return False
            tr = cur.row + dy
            tc = 0
            if tr < 0 or tr > self._grid_max_row:
                return False
            row_cols = self._grid_rows.get(tr)
            if not row_cols or 0 not in row_cols:
                return False
            idx = row_cols[0]
            if idx == self._focus:
                return False
            self.set_focus(idx)
            return True
        tr = cur.row + dy
        tc = cur.col + dx
        if tr < 0 or tr > self._grid_max_row:
            return False
        row_cols = self._grid_rows.get(tr)
        if not row_cols:
            return False
        if dy != 0:
            # Vertical move: clamp the column into the destination row's width
            # so crossing between jagged rows never overshoots.
            max_c = max(row_cols)
            if tc > max_c:
                tc = max_c
            if tc < 0:
                return False
        if tc not in row_cols:
            return False
        idx = row_cols[tc]
        if idx == self._focus:
            return False
        self.set_focus(idx)
        return True

    def _tab(self, forward: bool) -> bool:
        if not self._items:
            return False
        # Card mode: Tab steps between cards (vertical).  Item mode: Tab steps
        # within the active card's buttons.
        if not self._edit_mode:
            if self._card_mode:
                return self._move_card(0, 1 if forward else -1)
            if self._active_card >= 0:
                rng = self._card_ranges.get(self._active_card)
                if rng is not None and rng[1] > rng[0]:
                    seq = list(range(rng[0], rng[1]))
                    if self._focus in seq:
                        idx = seq.index(self._focus)
                        self.set_focus(seq[(idx + (1 if forward else -1)) % len(seq)])
                    else:
                        self.set_focus(seq[0])
                    return True
        n = len(self._items)
        nxt = (self._focus + (1 if forward else -1)) % n
        self.set_focus(nxt)
        return True

    def _jump_panel(self, direction: int) -> bool:
        # Jump between *cards that actually have buttons*, in real panel order.
        # ``having`` holds real panel indices of navigable cards (skips empty
        # ones such as an unconfigured tile card).  ``_panel_starts`` is now
        # real-panel-indexed (-1 = no items), so it is safe to index by a real
        # panel index here.
        having = [pi for pi in range(len(self._panels))
                  if pi < len(self._panel_starts) and self._panel_starts[pi] >= 0]
        if not having:
            return False
        cur = self._panel_index_of(self._focus)
        if cur not in having:
            # (re)start from the first navigable card when focus is lost or sits
            # on a card with no buttons.
            cur = having[0]
        else:
            pos = having.index(cur)
            cur = having[(pos + direction) % len(having)]
        self.set_focus(self._panel_starts[cur])
        return True

    # -- activation -------------------------------------------------------
    # -- two-layer card / button navigation ----------------------------
    def _reset_slider(self) -> None:
        self._slider_press_count = 0
        self._slider_armed = False

    def _card_centers(self) -> list:
        out: list = []
        for p in self._panels:
            try:
                if not _widget_alive(p):
                    # A panel removed by a rebuild / page switch — never call
                    # mapTo on a dead widget (native crash).  Use a neutral
                    # origin; it only affects relative card-move scoring.
                    out.append(QPoint(0, 0))
                else:
                    out.append(p.mapTo(self._win, QPoint(p.width() // 2, p.height() // 2)))
            except Exception:
                out.append(QPoint(0, 0))
        return out

    def _apply_card_highlight(self) -> None:
        """Show the theme-colour border on the selected card only."""
        # Gate behind _gp_active so no card border appears on cold start
        # (before the user has touched gamepad / arrow keys).
        for i, p in enumerate(self._panels):
            # A panel deleted by a page switch / rebuild can linger in
            # ``_panels`` if ``rebind()`` was skipped (an exception mid-rebuild).
            # ``set_card_selected`` ends in ``self.update()``, which is a native
            # crash on a deleted widget and cannot be caught by try/except — so
            # guard it with the same alive check the navigation paths use.
            # Non-QWidget panels (test fakes) have no C++ object to destroy and
            # are always safe to keep.
            if isinstance(p, QWidget) and not _widget_alive(p):
                continue
            sel = (self._gp_active and not self._edit_mode
                   and ((self._card_mode and i == self._card_focus)
                        or (not self._card_mode and i == self._active_card)))
            fn = getattr(p, "set_card_selected", None)
            if callable(fn):
                try:
                    fn(bool(sel))
                except Exception:
                    pass

    def _refresh_active_hints(self) -> None:
        """Re-push gamepad hints for the active card (on selection change)."""
        if not self._gp_active:
            return
        active = (self._card_focus
                  if (self._card_mode and not self._edit_mode) else self._active_card)
        arng = self._card_ranges.get(active) if active >= 0 else None
        for idx, it in enumerate(self._items):
            w = it.widget
            if not it.hint or not hasattr(w, "set_gamepad_hint") or it.no_bulk_hint:
                continue
            try:
                in_active = arng is not None and arng[0] <= idx < arng[1]
                wanted = it.hint if in_active else ""
                if getattr(w, "gamepad_hint", "") != wanted:
                    w.set_gamepad_hint(wanted)
            except Exception:
                pass

    def _active_card_items(self) -> list:
        active = (self._card_focus
                  if (self._card_mode and not self._edit_mode) else self._active_card)
        rng = self._card_ranges.get(active) if active >= 0 else None
        if rng is None:
            return []
        return self._items[rng[0]:rng[1]]

    def _fire_hint(self, letter: str) -> bool:
        """Trigger the active card's button whose hint == letter (direct
        gamepad shortcut, e.g. LS card: Y = 保存).  Returns True if fired.

        For the Y shortcut a focused *item* that carries its own ``action_y``
        (or ``action_y_gp``) takes priority over the card-level hint scan — so
        Y acts on the focused row (e.g. freeze candidate: 标为游戏) instead of a
        fixed card button.  Items that have ``action_y`` are skipped by the card
        scan below (they are only reachable while focused), which prevents a Y
        press aimed at one row from also firing a fixed card button sharing
        hint == "y".

        Gamepad-Y double-press: a second Y within ``_Y_DOUBLE_S`` on the *same*
        focused item fires its ``action_y_dbl`` (e.g. freeze row: 移出白名单)
        instead of re-firing the single-press action.  Keyboard W always uses
        the plain ``action_y`` toggle and is unaffected by this window.
        """
        self._gp_active = True
        if letter.lower() == "y":
            cur = self._current()
            if cur is not None and (cur.action_y is not None
                                    or cur.action_y_gp is not None
                                    or cur.action_y_dbl is not None):
                now = time.monotonic()
                is_dbl = (self._last_y_item is cur
                          and (now - self._last_y_t) < self._Y_DOUBLE_S)
                self._last_y_item = cur
                self._last_y_t = now
                if is_dbl and cur.action_y_dbl is not None:
                    try:
                        cur.action_y_dbl()
                    except Exception:
                        pass
                    return True
                cb_single = (cur.action_y_gp
                             if cur.action_y_gp is not None else cur.action_y)
                if cb_single is not None:
                    try:
                        cb_single()
                    except Exception:
                        pass
                    return True
                # Item only exposes a double handler: first press is a no-op.
                return True
        if letter.lower() == "x":
            # Focused-item priority for X (mirrors the Y branch above): an item
            # that exposes ``action_x`` (e.g. a freeze row: 关闭该进程) acts on
            # the focused row instead of a fixed card button.  Items with
            # ``action_x`` are only reachable here while focused, so X never
            # double-fires a different action for a row that also carries an
            # unrelated card-level hint.
            cur = self._current()
            if cur is not None and cur.action_x is not None:
                try:
                    cur.action_x()
                except Exception:
                    pass
                return True
        for it in self._active_card_items():
            if it.action_y is not None:
                continue
            if (it.hint or "").lower() == letter.lower():
                try:
                    it.confirm()
                except Exception:
                    # Same policy as activate_widget: surface the traceback
                    # instead of masking a failing confirm() as a silent
                    # black screen.
                    logging.getLogger(__name__).exception(
                        "hint activation: it.confirm() raised for %r",
                        getattr(it, "widget", None))
                return True
        return False

    def _move_card(self, dx: int, dy: int) -> bool:
        """Move the selected-card highlight between function cards."""
        n = len(self._panels)
        if n == 0:
            return False
        if self._card_focus < 0:
            # Nothing selected yet: pick first (or last for up / left).
            self._card_focus = n - 1 if (dx < 0 or dy < 0) else 0
            self._apply_card_highlight()
            self._refresh_active_hints()
            # 自动把页面滚到该卡完整显示（避免选中屏外卡片却看不见）。
            if 0 <= self._card_focus < len(self._panels):
                self._ensure_visible(self._panels[self._card_focus])
            return True
        centers = self._card_centers()
        c0 = centers[self._card_focus]
        best = -1
        best_score: Optional[float] = None
        for i in range(n):
            if i == self._card_focus:
                continue
            c = centers[i]
            if dx > 0 and c.x() <= c0.x():
                continue
            if dx < 0 and c.x() >= c0.x():
                continue
            if dy > 0 and c.y() <= c0.y():
                continue
            if dy < 0 and c.y() >= c0.y():
                continue
            main = (c.x() - c0.x()) * dx + (c.y() - c0.y()) * dy
            perp = abs((c.x() - c0.x()) * dy - (c.y() - c0.y()) * dx)
            score = main + perp * 3.0
            if best_score is None or score < best_score:
                best_score = score
                best = i
        if best < 0:
            return False
        old_card = self._card_focus
        self._card_focus = best
        self._apply_card_highlight()
        self._refresh_active_hints()
        self._notify_card_focus_change(old_card, best)
        # 自动把页面滚到该卡完整显示（避免选中屏外卡片却看不见）。
        if 0 <= self._card_focus < len(self._panels):
            self._ensure_visible(self._panels[self._card_focus])
        return True

    def _enter_card(self) -> None:
        """Enter the selected card: focus its A-hinted button (default first)."""
        if self._card_focus < 0 or self._card_focus >= len(self._panels):
            return
        rng = self._card_ranges.get(self._card_focus)
        if not rng or rng[0] >= rng[1]:
            # Card has no interactive buttons: keep it selected, no item mode.
            return
        self._card_mode = False
        self._active_card = self._card_focus
        self._reset_slider()
        self._apply_card_highlight()
        focus_idx = rng[0]
        for i in range(rng[0], rng[1]):
            if (self._items[i].hint or "").lower() == "a":
                focus_idx = i
                break
        self.set_focus(focus_idx)

    def _exit_card(self) -> None:
        if self._card_mode:
            return
        self._card_mode = True
        self._reset_slider()
        self._focus = -1
        self._hide_focus_rect()
        self._apply_card_highlight()
        self._refresh_active_hints()

    def _press_confirm(self) -> None:
        self._gp_active = True
        try:
            if self._edit_mode:
                cur = self._current()
                if cur is not None:
                    if self._edit_inner:
                        # 内层：Enter/Space = 执行当前聚焦的位置按钮（↑/↓/▲/▼ 移动）。
                        cur.confirm()
                    else:
                        tok = self._layout_focused_card()
                        if tok:
                            # 外层：Enter/Space = 进入卡片内层（位置按钮二级界面），
                            # 与手柄 A 完全一致；不再直接打开「编辑项」面板（那是 Y 短按的职责）。
                            self.enter_card_inner(tok, 1)
                        else:
                            # 焦点在非卡片项（页眉/页脚按钮），直接执行其 confirm。
                            cur.confirm()
                return
            if self._card_mode:
                # A / Enter selects the card's buttons: enter item mode (the
                # card's A-hinted button, e.g. 加载, is auto-focused so a second
                # A confirms it).  Y / X remain single-press direct shortcuts.
                self._enter_card()
                return
            # Item mode: confirm the focused button.  Sliders need a 2nd press.
            cur = self._current()
            if cur is None:
                return
            if cur.kind == "slider":
                # First A / Enter ARMS the slider (arrows then nudge the value);
                # a second A / Enter CONFIRMS (applies) and exits armed mode.
                # Previously this branch always re-armed and never confirmed, so
                # a slider's ``confirm`` was unreachable from the gamepad/keys.
                if not self._slider_armed:
                    self._slider_armed = True
                    self._place_rect(keep_visible=True)
                else:
                    self._reset_slider()
                    try:
                        cur.confirm()
                    except Exception:
                        pass
                return
            cur.confirm()
        except Exception:
            # An unhandled exception inside a Qt key / gamepad event handler is
            # FATAL to the whole HUD under PyQt6 (the C++ side aborts the
            # process — the user sees a freeze, then the app dies).  This covers
            # BOTH the item-mode confirm (a bad tile / block action) AND the
            # card-entry transition (_enter_card -> set_focus -> on_focus /
            # _place_rect), which runs block code that can trip on a stale or
            # destroyed widget left behind by the per-second refresh().  The
            # mouse path (activate_widget) already guards its confirm; keep the
            # keyboard / gamepad path symmetric so a single bad card can never
            # take down the app.
            pass

    def _press_cancel(self) -> None:
        self._gp_active = True
        if self._edit_mode:
            self.clear()
            self.request_cancel.emit()
            return
        if self._slider_armed:
            # In slider control mode: B / Esc just exits control mode (back to
            # the selection layer), it does NOT leave the card.
            self._slider_armed = False
            self._place_rect(keep_visible=True)
            return
        if not self._card_mode:
            self._exit_card()
            return
        # Card mode: nothing to cancel.

    def _press_help(self) -> None:
        self._gp_active = True
        if self.on_help is not None:
            try:
                self.on_help()
            except Exception:
                pass

    def _prev_page(self) -> None:
        self._gp_active = True
        if self.on_prev_page is not None:
            try:
                self.on_prev_page()
            except Exception:
                pass

    def _next_page(self) -> None:
        self._gp_active = True
        if self.on_next_page is not None:
            try:
                self.on_next_page()
            except Exception:
                pass

    def activate(self) -> None:
        self._press_confirm()

    def activate_widget(self, widget: QWidget, confirm: bool = True) -> None:
        """Mouse / direct entry-point: focus the control owning ``widget`` then
        (optionally) activate it.

        For a control inside a function card this also *enters* that card
        (button layer) so subsequent keyboard / gamepad input navigates within
        the card — exactly the two-layer model.  Both mouse and keyboard
        converge here, so there is a single activation path.
        """
        for i, it in enumerate(self._items):
            if it.widget is widget:
                # Flag this as a real interaction BEFORE the highlight so the
                # cold-start gate (_gp_active) is already open and the block
                # selection border actually paints on the very first click.
                self._gp_active = True
                if not self._edit_mode:
                    pi = self._panel_index_of(i)
                    if pi >= 0:
                        self._card_focus = pi
                        self._active_card = pi
                        self._card_mode = False
                        self._reset_slider()
                        self._apply_card_highlight()
                        self._refresh_active_hints()
                self.set_focus(i)
                if confirm:
                    try:
                        it.confirm()
                    except Exception:
                        # Do NOT swallow: a failing confirm() (e.g. a panel's
                        # refresh() throwing) used to surface only as a silent
                        # black screen. Log the traceback so the real error is
                        # visible instead of masked.
                        logging.getLogger(__name__).exception(
                            "activate_widget: it.confirm() raised for %r",
                            getattr(it, "widget", None))
                return

    def pick_widget(self, widget: QWidget) -> None:
        """Mouse entry-point for a non-button widget (slider / input / card
        body).

        * If ``widget`` is one of our nav controls, focus it (entering its
          card).  * Otherwise, if it lives inside a function card, select that
          card (card layer) so the keyboard / gamepad cursor starts there.
        """
        for i, it in enumerate(self._items):
            if it.widget is widget:
                self.activate_widget(widget, False)
                return
        p = widget
        while p is not None:
            if p in self._panels:
                self._select_card(self._panels.index(p))
                return
            p = p.parentWidget()

    def _select_card(self, pi: int) -> None:
        if pi < 0 or pi >= len(self._panels):
            return
        old_card = self._card_focus
        self._card_mode = True
        self._card_focus = pi
        self._active_card = pi
        self._gp_active = True
        self._reset_slider()
        self._focus = -1
        self._hide_focus_rect()
        self._apply_card_highlight()
        self._refresh_active_hints()
        self._notify_card_focus_change(old_card, pi)
        # 鼠标点卡片：自动把页面滚到该卡完整显示。
        if 0 <= pi < len(self._panels):
            self._ensure_visible(self._panels[pi])

    def clear(self) -> None:
        # In HUD item mode, Esc / B exits back to card mode (instead of
        # dropping the focus entirely).
        if not self._edit_mode and not self._card_mode:
            self._exit_card()
            return
        old = self._current()
        if old is not None and old.on_focus is not None:
            try:
                old.on_focus(False)
            except Exception:
                pass
        self._focus = -1
        self._hide_focus_rect()

    # -- keyboard ---------------------------------------------------------
    def handle_key(self, ev: Any) -> bool:
        # Rebuild guard (see set_rebuilding()): swallow input during a page
        # rebuild so we never act on a half-built UI.  Consume the key so it
        # cannot fall through to any default widget behaviour either.
        if self._rebuilding:
            return True
        # 键盘输入 = 鼠标/键盘设备模式（驱动底部提示互斥显示）。
        self.set_input_mode("mouse")
        key = ev.key()
        # Any real navigation key counts as "user has interacted" -> reveal the
        # card-selection border + ABXY shortcut capsules.  Previously only a
        # gamepad press set this flag, so keyboard / mouse users never saw them.
        if (key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left,
                    Qt.Key.Key_Right, Qt.Key.Key_Tab, Qt.Key.Key_Enter,
                    Qt.Key.Key_Return, Qt.Key.Key_Space, Qt.Key.Key_Escape,
                    Qt.Key.Key_PageUp, Qt.Key.Key_PageDown)
                or Qt.Key.Key_1 <= key <= Qt.Key.Key_9):
            self._gp_active = True
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right):
            # 键盘方向键自动重复节流：长按方向键时 OS 以 ~30 次/秒投递
            # keyPressEvent，每个都独立走一次完整移动；事件在 UI 线程堆积 →
            # 焦点光标跳变/掉帧。与手柄 _gp_repeat_at 同思路，把移动限到
            # ~11 次/秒。首按（非重复）立即移动；轻点（按下即抬起，
            # isAutoRepeat=False）立即移动一次、不吞；松手前 OS 持续重复使
            # 最后一次落点正确，不会漏掉目标卡。
            if ev.isAutoRepeat():
                now = time.monotonic()
                if now - self._kb_repeat_at < self._KB_REPEAT_INTERVAL_S:
                    return True
                self._kb_repeat_at = now
            else:
                # 首次真实按下也锚定节流窗口：防止极早到达的重复事件（理论上
                # OS 初始重复延迟 > 间隔，不会触发，但锚定后逻辑更稳）绕过闸门。
                # 非重复按键永远立即移动，不受间隔限制（快速连点两个独立按下）。
                self._kb_repeat_at = time.monotonic()
            dx = dy = 0
            if key == Qt.Key.Key_Up:
                dy = -1
            elif key == Qt.Key.Key_Down:
                dy = 1
            elif key == Qt.Key.Key_Left:
                dx = -1
            else:
                dx = 1
            return self._apply_direction(dx, dy)
        if key == Qt.Key.Key_Tab:
            return self._tab(not (ev.modifiers() & Qt.KeyboardModifier.ShiftModifier))
        if key in (Qt.Key.Key_Enter, Qt.Key.Key_Return, Qt.Key.Key_Space):
            self.activate()
            return True
        if key == Qt.Key.Key_Escape:
            self.clear()
            return True
        if key == Qt.Key.Key_PageUp:
            if (not self._edit_mode) and self._card_mode:
                return self._move_card(0, -1)
            return self._jump_panel(-1)
        if key == Qt.Key.Key_PageDown:
            if (not self._edit_mode) and self._card_mode:
                return self._move_card(0, 1)
            return self._jump_panel(1)
        # W = 标为游戏（HUD 下作用于当前聚焦、且挂了 action_y 的项，例如
        # 冻结候选）。编辑模式（含文本框输入）不拦截，避免吞键。
        if key == Qt.Key.Key_W and not self._edit_mode:
            cur = self._current()
            if cur is not None and cur.action_y is not None:
                self._gp_active = True
                try:
                    cur.action_y()
                except Exception:
                    pass
            return True  # HUD 无文本框，W 恒消费，避免误触
        # Backspace = 关闭选中进程（HUD 下作用于当前聚焦、且挂了 action_x 的项，
        # 例如冻结候选/已冻结）。编辑模式（含文本框输入）不拦截，避免吞键。
        if key == Qt.Key.Key_Backspace and not self._edit_mode:
            cur = self._current()
            if cur is not None and cur.action_x is not None:
                self._gp_active = True
                try:
                    cur.action_x()
                except Exception:
                    pass
            return True  # HUD 无文本框，Backspace 恒消费，避免误触
        # Number keys 1..9 jump to the absolute top-level page (mirrors the
        # page-bar buttons).  When a text input has focus it consumes the digit
        # itself, so this only fires for navigation, never while typing.
        if Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            if self.on_goto_page is not None:
                try:
                    self.on_goto_page(key - Qt.Key.Key_1)
                except Exception:
                    # Same fatal-class guard as the gamepad page switch: a page
                    # rebuild error must not abort the HUD from a key handler.
                    pass
                return True
            return False
        return False

    # -- gamepad ----------------------------------------------------------
    def _dispatch_gamepad_action(self, action: str) -> None:
        """Fire a logical gamepad action resolved from the binding map.

        In the editor (``_edit_mode``) the face buttons keep their global
        meaning (confirm / cancel / save / delete / help).  In the HUD they
        drive the two-layer card / button model: A = enter card / confirm,
        B = exit card, X / Y = direct shortcuts, LB / RB = switch page.

        Two *shell* actions are handled first and work in every mode (and even
        while the window is unfocused, when the host still listens to the pad):
        ``focus`` (Start) raises / focuses the app, ``layout_toggle`` (Select)
        toggles the layout editor.
        """
        # Shell-level actions — always available, mode-independent.
        if action == "focus":
            if self.on_focus_window is not None:
                try:
                    self.on_focus_window()
                except Exception:
                    pass
            return
        if action == "layout_toggle":
            if self.on_toggle_layout is not None:
                try:
                    self.on_toggle_layout()
                except Exception:
                    pass
            return

        self._gp_active = True
        if self._edit_mode:
            if action == "confirm":
                if self._edit_inner:
                    # 内层：A/Enter = 执行当前聚焦的位置按钮（↑/↓/▲/▼ 移动）。
                    self.activate()
                else:
                    # 外层：A/Enter = 卡片行进入内层（聚焦该卡第一个位置按钮）；
                    # 横幅按钮行（页眉/页脚，无 card_token）没有内层概念，
                    # 直接执行该按钮的 confirm。
                    tok = self._layout_focused_card()
                    if tok:
                        self.enter_card_inner(tok, 1)
                    else:
                        self.activate()
            elif action == "cancel":
                if self._edit_inner:
                    # 内层：B = 退回卡片外层（整行）。
                    self.exit_card_inner()
                else:
                    self.clear()
                    self.request_cancel.emit()
            elif action == "delete":
                self.request_delete.emit()
            elif action == "save":
                self.request_save.emit()
            elif action == "help":
                if self.on_help is not None:
                    try:
                        self.on_help()
                    except Exception:
                        pass
            elif action == "prev_panel":
                self._jump_panel(-1)
            elif action == "next_panel":
                self._jump_panel(1)
            return
        # HUD two-layer model.
        if action == "confirm":
            self._press_confirm()
        elif action == "cancel":
            self._press_cancel()
        elif action == "delete":
            self._fire_hint("x")
        elif action == "save":
            self._fire_hint("y")
        elif action == "help":
            self._press_help()
        elif action == "prev_panel":
            self._prev_page()
        elif action == "next_panel":
            self._next_page()

    # -- 布局编辑页 Y/X 长按分流的「行级」动作 ----------------------------
    def _layout_focused_card(self) -> "str | None":
        """返回当前手柄焦点所在行（NavItem）的卡片 token；焦点不在卡片行上
        （如在页眉/页脚按钮）时返回 None。行级动作（Y 短按进编辑项、X 长按隐藏）
        仅在返回非 None 时才执行。"""
        if 0 <= self._focus < len(self._items):
            tok = getattr(self._items[self._focus], "card_token", None)
            if tok:
                return tok
        return None

    def _layout_short_fire(self, btn: str) -> None:
        """Y 短按（松手早于阈值）= 进入当前焦点行的编辑项。X 短按无动作。"""
        if btn != "y":
            return
        tok = self._layout_focused_card()
        if not tok or self._edit_overlay is None:
            return
        try:
            fn = getattr(self._edit_overlay, "_edit_items", None)
            if callable(fn):
                fn(tok)
        except Exception:
            pass

    def _layout_long_fire(self, btn: str) -> None:
        """Y 长按 = 保存布局（全局，与焦点行无关）；X 长按 = 隐藏当前焦点行。"""
        if self._edit_overlay is None:
            return
        if btn == "y":
            try:
                if hasattr(self, "request_save"):
                    self.request_save.emit()
            except Exception:
                pass
        elif btn == "x":
            tok = self._layout_focused_card()
            if not tok:
                return
            try:
                fn = getattr(self._edit_overlay, "_toggle", None)
                if callable(fn):
                    fn(tok)
            except Exception:
                pass

    def focus_card(self, token: str) -> None:
        """布局编辑页：把焦点设到指定卡片 token 对应的「整行」NavItem。

        鼠标单击选中某行后调用，使手柄 Y 短按=编辑项 / X 长按=隐藏 也能瞄准
        该卡（长按分流读 ``self._items[self._focus].card_token``）。仅编辑模式有效。"""
        if not self._edit_mode or self._edit_overlay is None:
            return
        for i, it in enumerate(self._items):
            if getattr(it, "card_token", None) == token:
                self.set_focus(i, force_rect=True)
                return

    # -- 设备互斥（底部提示） --------------------------------------------
    def set_input_mode(self, mode: str) -> None:
        """切换当前输入源（``mouse`` / ``gamepad``），驱动布局编辑页底部提示
        互斥显示（鼠标芯片 ↔ 手柄键帽）。仅编辑模式有效，其它模式无需切换。"""
        if mode == self._input_mode:
            return
        self._input_mode = mode
        ov = self._edit_overlay
        if ov is not None and hasattr(ov, "_hint_set_input_mode"):
            try:
                ov._hint_set_input_mode(mode)
            except Exception:
                pass

    # -- 布局编辑页两层导航（outer=整行 / inner=位置按钮） ------------
    def enter_card_inner(self, token: str, col: int = 1) -> None:
        """进入卡片内层：焦点从整行（col 0）移到该卡的第 ``col`` 个位置按钮
        （↑/↓/▲/▼）。此后方向键可在位置按钮间横向移动、上下切卡。仅编辑模式。"""
        if not self._edit_mode:
            return
        self._edit_inner = True
        for i, it in enumerate(self._items):
            if getattr(it, "card_token", None) == token and it.col == col:
                self.set_focus(i, force_rect=(self._input_mode == "mouse"))
                return

    def exit_card_inner(self) -> None:
        """退回卡片外层：焦点回到当前卡整行（col 0），方向键重新只在卡片间切。"""
        if not self._edit_mode:
            return
        self._edit_inner = False
        cur = self._current()
        tok = getattr(cur, "card_token", None) if cur is not None else None
        if tok is None:
            return
        for i, it in enumerate(self._items):
            if getattr(it, "card_token", None) == tok and it.col == 0:
                self.set_focus(i, force_rect=(self._input_mode == "mouse"))
                return

    def _gp_action_allowed(self, action: str) -> bool:
        """Coalesce a single physical press that the backend reports twice.

        Mechanical / Bluetooth button chatter and a controller that re-indexes
        (hot-plug blip) can both surface as two rising edges for one press —
        which the user sees as "one click did two things" (e.g. two candidates
        toggled).  Suppress any logical *action* that repeats within
        ``_GP_ACTION_DEBOUNCE_S`` of the previous one.  A deliberate second
        press is always >100 ms apart, so this never bites a human but kills
        sub-50 ms double events.  ``focus`` (Start, raise window) is also
        covered — double-raising is harmless but the debounce keeps it tidy.
        """
        now = time.monotonic()
        last = self._gp_last_action_at.get(action, 0.0)
        if now - last < self._GP_ACTION_DEBOUNCE_S:
            return False
        self._gp_last_action_at[action] = now
        return True

    def _poll_gamepad(self) -> None:
        """Safe wrapper around :meth:`_poll_gamepad_impl`.

        A gamepad poll runs as a ``QTimer`` slot on the UI thread.  Under
        PyQt6 an *unhandled* exception raised inside a slot is fatal — the C++
        side aborts the whole process (the user sees an instant crash with no
        Python traceback).  A page switch's synchronous ``rebuild()`` can delete
        and recreate widgets between polls, so a subsequent poll that touches a
        just-deleted widget (``mapTo`` / ``setGeometry``) would raise and kill
        the app.  This wrapper guarantees a single bad poll can NEVER take down
        the HUD — navigation stays alive and the next poll simply retries.
        """
        profile.tick_gp_poll()
        try:
            self._poll_gamepad_impl()
        except Exception:
            # Swallow any residual error from one poll; the next 30 ms poll
            # retries cleanly.  Widget-touch native crashes are separately
            # prevented by _widget_alive() guards in the navigation helpers.
            pass

    def _poll_gamepad_impl(self) -> None:
        """Translate a gamepad poll into the same nav / activation calls the
        keyboard uses.  Triggered ~33×/s by :meth:`start_gamepad`.

        Face / shoulder / stick buttons are edge-triggered (fire once per
        physical press) and mapped to logical actions via the config-driven
        binding table (``AppConfig.input_config.bindings``) — so the same A/B/
        X/Y/LB/RB mean the same thing on every controller.  The stick / D-pad
        navigate with key-repeat style debouncing so a held direction sweeps
        the focus one step at a time instead of flying.
        """
        # Rebuild guard: never navigate (and thus never repaint) while a page
        # rebuild is deleting/recreating panels.  See set_rebuilding().
        if self._rebuilding:
            return
        if self._gamepad is None:
            return
        st = self._gamepad.poll()

        # Battery-tier edge -> recolor the HUD glyph (green -> red).  Fired only
        # when the SDL power tier changes, so a connected pad never repaints on
        # every 33 ms poll.
        if st.power_level != self._gp_power_prev:
            self._gp_power_prev = st.power_level
            if self.on_gamepad_battery is not None:
                try:
                    self.on_gamepad_battery(st.power_level)
                except Exception:
                    pass

        # Connection-status edge -> notify the host (HUD indicator / tray).
        if st.connected != self._gp_connected_prev:
            self._gp_connected_prev = st.connected
            if self._gp_connected_prev and self._gp_had_disconnect:
                # Genuine reconnect (after a real disconnect): the SDL backend
                # re-opened the controller, so any button still physically held
                # now reads as a *fresh* rising edge on this poll.  Seed
                # ``_gp_prev`` with the current state so the edge detector sees
                # no transition and does NOT fire a spurious second action for a
                # press that is still in progress.  The first-ever connect is
                # NOT a reconnect (``_gp_had_disconnect`` is False), so we do
                # not swallow the user's first press.
                self._gp_prev = st
                self._gp_last_action_at = {}
            self._gp_had_disconnect = False
            if self.on_connection_changed is not None:
                try:
                    self.on_connection_changed(st.connected, st.name or "", st.power_level)
                except Exception:
                    pass

        if not st.connected:
            self._gp_prev = None
            self._gp_dir = (0, 0)
            self._gp_repeat_at = 0.0
            self._gp_last_action_at = {}
            self._gp_had_disconnect = True
            return

        prev = self._gp_prev
        self._gp_prev = st

        # Config-driven button -> action map (reverse of the bindings table).
        # Defaults mirror the PS1 convention; a user remap in layout.json
        # overrides them without touching code.  select/start are added as
        # *shell* actions and ALWAYS resolve — they are merged on top of, never
        # replaced by, a custom bindings table.
        action_for = {
            "a": "confirm",
            "b": "cancel",
            "x": "delete",
            "y": "save",
            "l3": "help",
            "lb": "prev_panel",
            "rb": "next_panel",
        }
        bindings = getattr(self._cfg, "input_config", None) if self._cfg else None
        if bindings is not None:
            rev: dict[str, str] = {}
            for act, btn in bindings.bindings.items():
                rev[str(btn).lower()] = str(act).lower()
            if rev:
                action_for.update(rev)
        # System (shell) bindings are user-customizable: the gamepad button that
        # opens the layout editor / focuses the window is read from the store,
        # not hardcoded — so the user can rebind Select / Start to anything.
        _sys_action = {"open_layout_editor": "layout_toggle",
                       "focus_window": "focus"}
        for _sa, _act in _sys_action.items():
            _btn = self._sys_gamepad(_sa)
            if _btn:
                action_for[str(_btn).lower()] = _act

        # Is the app the foreground window?  When it is NOT (another app, e.g. a
        # game, is on top) we keep the gamepad listening but only act on the two
        # *shell* behaviours — Start (focus) and any user shortcut trigger — so
        # we never drive the hidden HUD behind someone's back.
        is_active = bool(getattr(self._win, "isActiveWindow", lambda: True)())

        # Edge detection across every known button (incl. select / start).
        ALL_BTNS = ("a", "b", "x", "y", "l3", "r3", "lb", "rb", "select", "start")
        engine = self.shortcut_engine
        pressed: list[str] = []
        released: list[str] = []
        for btn in ALL_BTNS:
            cur = getattr(st, btn, False)
            was = getattr(prev, btn, False) if prev is not None else False
            if cur and not was:
                pressed.append(btn)
            elif was and not cur:
                released.append(btn)

        # Gamepad "record" capture (editor): if the editor is arming a capture,
        # the FIRST pressed button is handed to it and normal dispatch is
        # skipped for this poll so we don't also fire the button's own action.
        if self._gp_capture is not None:
            for btn in pressed:
                cb = self._gp_capture
                self._gp_capture = None
                try:
                    cb(btn)
                except Exception:
                    pass
                return

        # Releases first (long-press timing resolves before any re-press).
        if engine is not None:
            for btn in released:
                try:
                    engine.handle_gamepad_release(btn)
                except Exception:
                    pass
            for btn in pressed:
                if engine.is_gamepad_trigger(btn):
                    try:
                        engine.handle_gamepad_press(btn)
                    except Exception:
                        pass
                    continue  # consumed by a shortcut; don't also drive the HUD
                action = action_for.get(btn)
                if action is None:
                    continue
                # 布局编辑页：Y/X 改走长按分流（见下方 _gp_long_press 处理），
                # 这里不再「按下即触发」，避免与长按判定重复派发。
                if self._edit_mode and btn in ("y", "x"):
                    continue
                if not is_active and action != "focus":
                    continue
                if self._gp_action_allowed(action):
                    self.set_input_mode("gamepad")
                    self._dispatch_gamepad_action(action)
        else:
            for btn in pressed:
                action = action_for.get(btn)
                if action is None:
                    continue
                # 同上：编辑页 Y/X 长按分流，不在此立即派发。
                if self._edit_mode and btn in ("y", "x"):
                    continue
                if not is_active and action != "focus":
                    continue
                if self._gp_action_allowed(action):
                    self.set_input_mode("gamepad")
                    self._dispatch_gamepad_action(action)

        # 布局编辑页 Y/X 长按分流：覆盖默认「按下即触发」的行为。
        #   Y 短按 = 进入当前焦点行的编辑项；Y 长按 = 保存并退出（全局）
        #   X 长按 = 隐藏当前焦点行的卡片；X 短按 = 无动作
        # 仅在 _edit_mode 下生效；其余模式 Y/X 仍走上面的 _dispatch_gamepad_action。
        # 关键不变量：长按必须「按住满阈值才触发」，不能「按下即触发」——否则长按
        # Y 会先把 mode 切出 layout、_gp_save 读到错误 mode 而静默写错布局。
        if self._edit_mode:
            self.set_input_mode("gamepad")
            _now = time.monotonic()
            for btn in pressed:
                if btn in ("y", "x"):
                    # 按下即计时，不立即派发（派发改到松手/长按判定处）。
                    self._gp_long_press[btn] = {"t": _now, "fired": False}
            for btn, info in list(self._gp_long_press.items()):
                if getattr(st, btn, False):
                    # 仍按住：满阈值则触发长按，标记已消费（防止松手再补短按）。
                    if _now - info["t"] >= (LONG_PRESS_MS / 1000.0) \
                            and not info["fired"]:
                        info["fired"] = True
                        self._layout_long_fire(btn)
                else:
                    # 已松开且未触发长按 → 视为短按（同轮 poll 内按下即松开也会走到这）。
                    if not info["fired"]:
                        self._layout_short_fire(btn)
                    self._gp_long_press.pop(btn, None)

        # Direction: stick / D-pad with repeat debounce.  Only when the app is
        # the foreground window — otherwise a game's stick would sweep the
        # hidden HUD focus.
        if not is_active:
            self._gp_dir = (0, 0)
            self._gp_repeat_at = 0.0
            return
        dx, dy = st.direction()
        now = time.monotonic()
        if (dx, dy) == (0, 0):
            self._gp_dir = (0, 0)
            self._gp_repeat_at = 0.0
        elif (dx, dy) != self._gp_dir:
            self._gp_active = True
            self.set_input_mode("gamepad")
            try:
                self._apply_direction(dx, dy)
            except Exception:
                # A navigation error must NEVER abort the HUD — the C++ side
                # treats an unhandled exception in a gamepad poll slot as
                # fatal.  Widget-touch native crashes are separately guarded by
                # _widget_alive(); this catches any residual Python error.
                pass
            self._gp_dir = (dx, dy)
            self._gp_repeat_at = now + 0.35
        elif now >= self._gp_repeat_at:
            try:
                self._apply_direction(dx, dy)
            except Exception:
                pass
            self._gp_repeat_at = now + 0.16