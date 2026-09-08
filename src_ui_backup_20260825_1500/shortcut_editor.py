"""Standalone shortcut-button editor (gamepad / keyboard, long & short press).

A *separate top-level window* (decoupled from the in-window layout editor and
the button-*style* editor).  It is created with ``Qt.Window`` + a real title bar
and close button, so it is always closable and never traps the user; it is
positioned beside the main HUD so it does not cover the function cards, and its
width is hard-capped so no control can ever spill outside the window.

Each shortcut card lets the user pick an optional gamepad button and/or a
keyboard key, then assign a *short-press* action and a *long-press* action —
each one of four kinds:

* 启动程序 (launch)  — run an external EXE / open a file.
* 运行脚本 (script)  — run a command line.
* 发送按键 (macro)   — inject a keystroke sequence into the foreground window.
* App 内功能 (inapp) — invoke an in-app command.

Triggers are *recorded* live: click 「录制」 and the next gamepad button / key
combo you press (within a 3-second countdown) becomes the trigger.  Keyboard
combos are captured as a whole (modifiers + final key, e.g. ``Ctrl+F8``) and can
also be typed directly (e.g. ``Win+Ctrl+F8``) for chords the OS swallows.

A separate **system** section lets the user rebind the three always-on shell
behaviours (open layout editor / focus window / bring window to front).

All edits persist live to ``shortcuts.json`` via the shared :class:`ShortcutStore`;
the running engine + global hotkey are re-synced on every change (no restart).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.shortcuts import (
    ACTION_KINDS,
    GAMEPAD_RESERVED,
    ActionSpec,
    Shortcut,
    ShortcutEngine,
    ShortcutStore,
)
from .keyutil import key_token_from_event

# In-app command choices: (token, display label).
_INAPP_CHOICES = [
    ("next_page", "下一页"),
    ("prev_page", "上一页"),
    ("focus", "聚焦本窗口"),
    ("open_layout_editor", "打开布局编辑"),
    ("open_shortcut_editor", "打开本编辑器"),
    ("minimize", "最小化到托盘"),
]

_KIND_LABELS = {
    "launch": "启动程序",
    "script": "运行脚本",
    "macro": "发送按键",
    "inapp": "App 内功能",
}

# Logical system actions shown in the (non-deletable) system section.
_SYSTEM_ITEMS = [
    ("open_layout_editor", "打开布局编辑 (原 Select)"),
    ("focus_window", "聚焦本窗口 (原 Start)"),
    ("bring_to_front", "后台召唤/置顶 (原 Win+Ctrl+F8)"),
]

# Tokens that are *modifiers only* — they can never be a standalone trigger.
_MOD_ONLY = ("CTRL", "CONTROL", "ALT", "SHIFT", "WIN")

# Shared widget styling so every control inside the editor looks consistent.
_BTN_STYLE = (
    "QPushButton { background: rgba(127,209,255,0.14); color: #cfe8ff;"
    " border: 1px solid rgba(127,209,255,0.26); border-radius: 6px;"
    " padding: 4px 12px; }"
    "QPushButton:hover { background: rgba(127,209,255,0.24); }"
    "QPushButton:disabled { background: rgba(127,209,255,0.06);"
    " color: #6f8298; border-color: rgba(127,209,255,0.10); }"
)
_DANGER_BTN_STYLE = (
    "QPushButton { background: rgba(255,90,90,0.16); color: #ff9b9b;"
    " border: 1px solid rgba(255,90,90,0.30); border-radius: 6px;"
    " padding: 3px 10px; }"
    "QPushButton:hover { background: rgba(255,90,90,0.26); }"
)
_REC_BTN_STYLE = (
    "QPushButton { background: rgba(255,196,90,0.16); color: #ffe0a3;"
    " border: 1px solid rgba(255,196,90,0.34); border-radius: 6px;"
    " padding: 4px 12px; min-width: 84px; }"
    "QPushButton:hover { background: rgba(255,196,90,0.28); }"
)
_ADD_BTN_STYLE = (
    "QPushButton { background: rgba(120,255,170,0.14); color: #b6ffd2;"
    " border: 1px solid rgba(120,255,170,0.28); border-radius: 7px;"
    " padding: 6px; }"
    "QPushButton:hover { background: rgba(120,255,170,0.24); }"
)


class _ActionGroup(QWidget):
    """Edits one ActionSpec (kind + kind-specific params)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._kind = QComboBox()
        for k in ACTION_KINDS:
            self._kind.addItem(_KIND_LABELS.get(k, k), k)
        self._kind.setStyleSheet(
            "QComboBox { background: rgba(255,255,255,0.07); color: #eaf2ff;"
            " border: 1px solid rgba(255,255,255,0.14); border-radius: 6px;"
            " padding: 3px 8px; }")
        self._kind.currentIndexChanged.connect(self._on_kind)
        lay.addWidget(self._kind)

        self._stack = QStackedWidget()
        # launch
        launch = QWidget()
        ll = QVBoxLayout(launch)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(4)
        self._exe = QLineEdit()
        self._exe.setPlaceholderText("程序路径 / 文件，如 C:\\Windows\\notepad.exe")
        browse = QPushButton("浏览…")
        browse.setStyleSheet(_BTN_STYLE)
        browse.clicked.connect(self._browse_exe)
        hl = QHBoxLayout()
        hl.addWidget(self._exe, 1)
        hl.addWidget(browse)
        self._args = QLineEdit()
        self._args.setPlaceholderText("参数（可选，空格分隔）")
        self._cwd = QLineEdit()
        self._cwd.setPlaceholderText("工作目录（可选）")
        ll.addLayout(hl)
        ll.addWidget(self._args)
        ll.addWidget(self._cwd)
        # script
        script = QWidget()
        sl = QVBoxLayout(script)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(4)
        self._cmd = QPlainTextEdit()
        self._cmd.setPlaceholderText("命令行，如 powershell -Command Get-Date")
        self._scwd = QLineEdit()
        self._scwd.setPlaceholderText("工作目录（可选）")
        sl.addWidget(self._cmd)
        sl.addWidget(self._scwd)
        # macro
        macro = QWidget()
        ml = QVBoxLayout(macro)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(4)
        self._keys = QLineEdit()
        self._keys.setPlaceholderText("如 ctrl+c   alt+tab   a b c")
        ml.addWidget(QLabel("按键序列（空格分隔组合，+ 连接同时按下）"))
        ml.addWidget(self._keys)
        # inapp
        inapp = QWidget()
        il = QVBoxLayout(inapp)
        il.setContentsMargins(0, 0, 0, 0)
        self._cmd_sel = QComboBox()
        self._cmd_sel.setStyleSheet(
            "QComboBox { background: rgba(255,255,255,0.07); color: #eaf2ff;"
            " border: 1px solid rgba(255,255,255,0.14); border-radius: 6px;"
            " padding: 3px 8px; }")
        for token, label in _INAPP_CHOICES:
            self._cmd_sel.addItem(label, token)
        il.addWidget(self._cmd_sel)

        self._stack.addWidget(launch)
        self._stack.addWidget(script)
        self._stack.addWidget(macro)
        self._stack.addWidget(inapp)
        lay.addWidget(self._stack)

    def _browse_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择程序", "",
                                              "程序 (*.exe *.bat *.cmd *.ps1);;所有文件 (*.*)")
        if path:
            self._exe.setText(path)

    def _on_kind(self) -> None:
        self._stack.setCurrentIndex(max(0, self._kind.currentIndex()))

    def set_action(self, spec: ActionSpec) -> None:
        idx = ACTION_KINDS.index(spec.kind) if spec.kind in ACTION_KINDS else 0
        self._kind.setCurrentIndex(idx)
        self._stack.setCurrentIndex(idx)
        p = spec.params or {}
        self._exe.setText(str(p.get("exe", "")))
        self._args.setText(str(p.get("args", "")))
        self._cwd.setText(str(p.get("cwd", "")))
        self._cmd.setPlainText(str(p.get("cmd", "")))
        self._scwd.setText(str(p.get("cwd", "")))
        self._keys.setText(str(p.get("keys", "")))
        ci = self._cmd_sel.findData(str(p.get("command", "")))
        if ci >= 0:
            self._cmd_sel.setCurrentIndex(ci)

    def get_action(self) -> ActionSpec:
        kind = ACTION_KINDS[max(0, self._kind.currentIndex())]
        params: dict[str, Any] = {}
        if kind == "launch":
            params = {
                "exe": self._exe.text().strip(),
                "args": self._args.text().strip(),
                "cwd": self._cwd.text().strip(),
            }
        elif kind == "script":
            params = {
                "cmd": self._cmd.toPlainText().strip(),
                "cwd": self._scwd.text().strip(),
            }
        elif kind == "macro":
            params = {"keys": self._keys.text().strip()}
        elif kind == "inapp":
            params = {"command": str(self._cmd_sel.currentData() or "")}
        return ActionSpec(kind=kind, params=params)


class _RecordButton(QPushButton):
    """A 'record' button that captures the next gamepad button / key chord.

    Clicking it starts a 3-second countdown; the first gamepad button pressed
    (or the final key of a keyboard chord) becomes the trigger.  For a keyboard
    chord we debounce ~350 ms after the last *real* key so the *whole* combo
    (e.g. ``Ctrl+F8``) is captured — modifier-only keystrokes never become the
    trigger, and the final event already encodes the held modifiers.
    """

    RECORD_SECS = 3
    _KB_DEBOUNCE_MS = 350

    def __init__(self, kind: str, controller: Any,
                 current_getter: Callable[[], Optional[str]],
                 on_captured: Callable[[Optional[str]], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = kind            # "gamepad" | "keyboard"
        self._ctrl = controller
        self._get_current = current_getter
        self._on_captured = on_captured
        self._recording = False
        self._secs = 0
        self._my_capture: Optional[Callable[[str], None]] = None
        self._pending_token = ""
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._kb_debounce = QTimer(self)
        self._kb_debounce.setSingleShot(True)
        self._kb_debounce.timeout.connect(self._finalize_keyboard)
        self.setStyleSheet(_REC_BTN_STYLE)
        self.clicked.connect(self.start_record)
        self._refresh_text()

    def _refresh_text(self) -> None:
        if self._recording:
            return
        if self._kind == "keyboard":
            self.setText("录制")
        else:
            cur = self._get_current()
            self.setText(cur or "录制手柄")

    def start_record(self) -> None:
        if self._recording:
            self._cancel()
            return
        self._recording = True
        self._secs = self.RECORD_SECS
        self.setText(f"录制 {self._secs}")
        if self._kind == "gamepad":
            def cb(btn: str) -> None:
                self._finalize_gamepad(btn)
            self._my_capture = cb
            if self._ctrl is not None:
                self._ctrl.set_gamepad_capture(cb)
        else:
            self.grabKeyboard()
            self._pending_token = ""
        self._timer.start(1000)

    def _tick(self) -> None:
        self._secs -= 1
        if self._secs <= 0:
            self._cancel()
        else:
            self.setText(f"录制 {self._secs}")

    def _disarm(self) -> None:
        self._timer.stop()
        self._kb_debounce.stop()
        self._recording = False
        if self._kind == "keyboard":
            try:
                self.releaseKeyboard()
            except Exception:
                pass
        if (self._kind == "gamepad" and self._ctrl is not None
                and self._my_capture is not None):
            if self._ctrl.get_gamepad_capture() is self._my_capture:
                self._ctrl.set_gamepad_capture(None)
        self._my_capture = None

    def _cancel(self) -> None:
        if not self._recording:
            return
        self._disarm()
        self._refresh_text()

    def _finalize_gamepad(self, btn: str) -> None:
        if not self._recording:
            return
        self._disarm()
        try:
            self._on_captured(btn)
        except Exception:
            pass
        self._refresh_text()

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        if self._recording and self._kind == "keyboard":
            tok = key_token_from_event(ev)
            if tok:
                parts = tok.split("+")
                # Only a *real* (non-modifier) key may become the trigger; a
                # modifier-only event (e.g. pressing Ctrl alone yields the token
                # "Control") must not overwrite a captured key.  Compare
                # case-insensitively because the key *name* keeps its original
                # case ("Control") while modifier prefixes are upper-cased.
                if parts[-1].upper() not in _MOD_ONLY:
                    self._pending_token = tok
                self._kb_debounce.start(self._KB_DEBOUNCE_MS)
            ev.accept()
            return
        super().keyPressEvent(ev)

    def _finalize_keyboard(self) -> None:
        if not self._recording:
            return
        self._disarm()
        tok = self._pending_token or None
        try:
            self._on_captured(tok)
        except Exception:
            pass
        self._refresh_text()


class _ShortcutCard(QWidget):
    """One editable user shortcut (label + triggers + short/long actions)."""

    def __init__(self, sc: Shortcut, store: ShortcutStore, controller: Any,
                 on_change: Callable[[], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sc = sc
        self._store = store
        self._ctrl = controller
        self._on_change = on_change
        self._sys_key: Optional[str] = None

        self.setStyleSheet(
            "QWidget { background: rgba(255,255,255,0.04);"
            " border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        # Header row: label + delete
        top = QHBoxLayout()
        self._label = QLineEdit(sc.label or "")
        self._label.setPlaceholderText("名称（如：截图 / 静音）")
        self._label.textChanged.connect(self._sync)
        top.addWidget(QLabel("名称"), 0)
        top.addWidget(self._label, 1)
        del_btn = QPushButton("删除")
        del_btn.setStyleSheet(_DANGER_BTN_STYLE)
        del_btn.clicked.connect(self._delete)
        top.addWidget(del_btn)
        lay.addLayout(top)

        # Gamepad trigger row
        gp = QHBoxLayout()
        gp.addWidget(QLabel("手柄"))
        self._gp_rec = _RecordButton(
            "gamepad", self._ctrl, lambda: self._sc.gamepad, self._on_gp_captured)
        gp.addWidget(self._gp_rec, 1)
        gp_clear = QPushButton("清除")
        gp_clear.setStyleSheet(_BTN_STYLE)
        gp_clear.clicked.connect(self._clear_gp)
        gp.addWidget(gp_clear)
        lay.addLayout(gp)

        # Keyboard trigger row (editable line + record, for Win chords)
        kb = QHBoxLayout()
        kb.addWidget(QLabel("键盘"))
        self._kb_line = QLineEdit(sc.keyboard or "")
        self._kb_line.setPlaceholderText("录制或手动输入，如 Win+Ctrl+F8")
        self._kb_line.textChanged.connect(self._on_kb_typed)
        kb.addWidget(self._kb_line, 1)
        self._kb_rec = _RecordButton(
            "keyboard", self._ctrl, lambda: "", self._on_kb_captured)
        kb.addWidget(self._kb_rec)
        kb_clear = QPushButton("清除")
        kb_clear.setStyleSheet(_BTN_STYLE)
        kb_clear.clicked.connect(self._clear_kb)
        kb.addWidget(kb_clear)
        lay.addLayout(kb)

        # Short press
        lay.addWidget(QLabel("短按"))
        self._short = _ActionGroup()
        self._short.set_action(sc.short)
        self._short._kind.currentIndexChanged.connect(self._sync)
        self._short._stack.currentChanged.connect(self._sync)
        lay.addWidget(self._short)
        # Long press
        lay.addWidget(QLabel("长按 (>500ms)"))
        self._long = _ActionGroup()
        self._long.set_action(sc.long)
        self._long._kind.currentIndexChanged.connect(self._sync)
        self._long._stack.currentChanged.connect(self._sync)
        lay.addWidget(self._long)

    # -- gamepad ---------------------------------------------------------
    def _on_gp_captured(self, btn: Optional[str]) -> None:
        if not btn:
            self._sc.gamepad = None
        elif btn.lower() in GAMEPAD_RESERVED:
            # select / start are shell-only; they cannot be a user shortcut.
            self._sc.gamepad = None
        elif self._gamepad_taken(btn):
            self._sc.gamepad = None
        else:
            self._sc.gamepad = btn
        self._on_change()

    def _gamepad_taken(self, btn: str) -> bool:
        b = (btn or "").lower()
        for s in self._store.shortcuts:
            if s is self._sc:
                continue
            if s.gamepad and s.gamepad.lower() == b:
                return True
        for k, v in self._store.system.items():
            if k == self._sys_key:
                continue
            if isinstance(v, dict) and (v.get("gamepad") or "").lower() == b:
                return True
        return False

    def _clear_gp(self) -> None:
        self._sc.gamepad = None
        self._gp_rec._refresh_text()
        self._on_change()

    # -- keyboard --------------------------------------------------------
    def _on_kb_typed(self, text: str) -> None:
        self._sc.keyboard = text.strip() or None
        self._on_change()

    def _on_kb_captured(self, token: Optional[str]) -> None:
        self._kb_line.blockSignals(True)
        self._kb_line.setText(token or "")
        self._kb_line.blockSignals(False)
        self._sc.keyboard = token or None
        self._on_change()

    def _clear_kb(self) -> None:
        self._kb_line.blockSignals(True)
        self._kb_line.setText("")
        self._kb_line.blockSignals(False)
        self._sc.keyboard = None
        self._on_change()

    # -- sync / lifecycle -----------------------------------------------
    def _sync(self, *_) -> None:
        self._sc.label = self._label.text()
        self._sc.short = self._short.get_action()
        self._sc.long = self._long.get_action()
        self._on_change()

    def _delete(self) -> None:
        self._store.remove(self._sc.id)
        self._on_change()
        self.deleteLater()


class _SystemCard(QWidget):
    """One non-deletable system (shell) binding: rebind its trigger(s)."""

    def __init__(self, key: str, label: str, store: ShortcutStore,
                 controller: Any, on_change: Callable[[], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sys_key = key
        self._store = store
        self._ctrl = controller
        self._on_change = on_change
        cur = store.system.get(key, {})
        self._gp = cur.get("gamepad")
        self._kb = cur.get("keyboard")

        self.setStyleSheet(
            "QWidget { background: rgba(127,209,255,0.05);"
            " border: 1px solid rgba(127,209,255,0.16); border-radius: 8px; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        lay.addWidget(QLabel(label))

        # Gamepad trigger
        gp = QHBoxLayout()
        gp.addWidget(QLabel("手柄"))
        self._gp_rec = _RecordButton(
            "gamepad", self._ctrl,
            lambda: self._gp, self._on_gp_captured)
        gp.addWidget(self._gp_rec, 1)
        gp_clear = QPushButton("清除")
        gp_clear.setStyleSheet(_BTN_STYLE)
        gp_clear.clicked.connect(self._clear_gp)
        gp.addWidget(gp_clear)
        lay.addLayout(gp)

        # Keyboard trigger
        kb = QHBoxLayout()
        kb.addWidget(QLabel("键盘"))
        self._kb_line = QLineEdit(self._kb or "")
        self._kb_line.setPlaceholderText("录制或手动输入，如 Win+Ctrl+F8")
        self._kb_line.textChanged.connect(self._on_kb_typed)
        kb.addWidget(self._kb_line, 1)
        self._kb_rec = _RecordButton(
            "keyboard", self._ctrl, lambda: "", self._on_kb_captured)
        kb.addWidget(self._kb_rec)
        kb_clear = QPushButton("清除")
        kb_clear.setStyleSheet(_BTN_STYLE)
        kb_clear.clicked.connect(self._clear_kb)
        kb.addWidget(kb_clear)
        lay.addLayout(kb)

    def _on_gp_captured(self, btn: Optional[str]) -> None:
        if not btn or self._gamepad_taken(btn):
            self._gp = None
        else:
            self._gp = btn
        self._store.system[self._sys_key]["gamepad"] = self._gp
        self._on_change()

    def _gamepad_taken(self, btn: str) -> bool:
        b = (btn or "").lower()
        for s in self._store.shortcuts:
            if s.gamepad and s.gamepad.lower() == b:
                return True
        for k, v in self._store.system.items():
            if k == self._sys_key:
                continue
            if isinstance(v, dict) and (v.get("gamepad") or "").lower() == b:
                return True
        return False

    def _clear_gp(self) -> None:
        self._gp = None
        self._store.system[self._sys_key]["gamepad"] = None
        self._gp_rec._refresh_text()
        self._on_change()

    def _on_kb_typed(self, text: str) -> None:
        self._kb = text.strip() or None
        self._store.system[self._sys_key]["keyboard"] = self._kb
        self._on_change()

    def _on_kb_captured(self, token: Optional[str]) -> None:
        self._kb_line.blockSignals(True)
        self._kb_line.setText(token or "")
        self._kb_line.blockSignals(False)
        self._kb = token or None
        self._store.system[self._sys_key]["keyboard"] = self._kb
        self._on_change()

    def _clear_kb(self) -> None:
        self._kb_line.blockSignals(True)
        self._kb_line.setText("")
        self._kb_line.blockSignals(False)
        self._kb = None
        self._store.system[self._sys_key]["keyboard"] = None
        self._on_change()


class ShortcutEditor(QWidget):
    """Top-level shortcut-button editor window (independent, always closable)."""

    FIXED_W = 500

    def __init__(self, app: Any, store: ShortcutStore, engine: ShortcutEngine,
                 controller: Any, on_change: Callable[[], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._app = app
        self._store = store
        self._engine = engine
        self._ctrl = controller
        self._on_change = on_change

        # Independent, framed window: a real title bar + OS close button means
        # the user can ALWAYS dismiss it (no more "cannot exit").  Non-modal so
        # the main HUD keeps working beside it.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint)
        self.setWindowModality(Qt.WindowModality.NonModal)
        # 辅助浮窗不应触发 app 退出。本窗口有 parent + 非 Tool，Qt 默认
        # WA_QuitOnClose=False，此处显式声明，防止未来重构改 window flags 时
        # 不慎回归成 True（一旦 True，主窗口 hide 到托盘后关本窗 → app.quit()）。
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setWindowTitle("快捷按钮")
        self.setFixedWidth(self.FIXED_W)
        self.setMinimumHeight(560)
        self.setStyleSheet(
            "ShortcutEditor { background: #0e1117; color: #dfe6f0; font-size: 12px; }"
            "QLabel { color: #aeb9c8; }"
            "QLineEdit, QPlainTextEdit { background: rgba(255,255,255,0.07);"
            " color: #eaf2ff; border: 1px solid rgba(255,255,255,0.14);"
            " border-radius: 6px; padding: 4px 8px; }"
            "QPushButton { background: rgba(127,209,255,0.14); color: #cfe8ff;"
            " border: 1px solid rgba(127,209,255,0.26); border-radius: 6px;"
            " padding: 4px 12px; }"
            "QPushButton:hover { background: rgba(127,209,255,0.24); }")

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("快捷键")
        title.setStyleSheet("font-weight: bold; font-size: 14px; color: #eaf2ff;")
        head.addWidget(title, 1)
        hint = QLabel("点「录制」后 3 秒内按手柄键 / 键盘组合")
        hint.setStyleSheet("color: #7e8aa0; font-size: 11px;")
        head.addWidget(hint)
        close = QPushButton("关闭")
        close.setStyleSheet(_BTN_STYLE)
        close.clicked.connect(self.close)
        head.addWidget(close)
        root.addLayout(head)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list = QWidget()
        self._list_lay = QVBoxLayout(self._list)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(10)
        self._scroll.setWidget(self._list)
        root.addWidget(self._scroll, 1)

        add = QPushButton("＋ 添加快捷按钮")
        add.setStyleSheet(_ADD_BTN_STYLE)
        add.clicked.connect(self._add)
        root.addWidget(add)

        self._rebuild()

    # -- layout / placement ---------------------------------------------
    def _clamp_inner_width(self) -> None:
        # Hard-cap the scrollable content to the window's inner width so nothing
        # can ever spill outside the fixed-width window (no more "page wider
        # than window").  Base it on the window width minus the root-layout
        # margins (12px each side) rather than the live viewport width, which
        # is only valid once the window is actually laid out / shown.
        inner = max(0, self.width() - 24)
        self._list.setMaximumWidth(inner)
        self._list.setMinimumWidth(0)

    def resizeEvent(self, ev: Any) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self._clamp_inner_width()

    def showEvent(self, ev: Any) -> None:  # noqa: N802
        super().showEvent(ev)
        self._place_beside_parent()
        self._clamp_inner_width()

    def _place_beside_parent(self) -> None:
        """Position the editor beside the main window so it never covers the
        HUD function cards; fall back to the right / left / centre of the screen
        if there is no room."""
        scr = self.screen() or None
        avail = scr.availableGeometry() if scr is not None else None
        pw = self.parentWidget()
        # Walk up to the real top-level window (the editor's parent may itself
        # be a child widget of the on-screen overlay window).
        ref = pw.window() if (pw is not None and hasattr(pw, "window")) else pw
        w = self.width()
        h = self.height()
        x = y = None
        if ref is not None:
            pg = ref.geometry()
            # Prefer to the right of the main window.
            rx = pg.x() + pg.width() + 10
            if avail is None or rx + w <= avail.right():
                x, y = rx, pg.y()
            else:
                # Else to the left.
                lx = pg.x() - w - 10
                if lx >= (avail.left() if avail else 0):
                    x, y = lx, pg.y()
        if x is None:
            if avail is not None:
                x = max(avail.left(), (avail.width() - w) // 2)
                y = max(avail.top(), (avail.height() - h) // 2)
            else:
                x, y = 40, 40
        if avail is not None:
            x = max(avail.left(), min(x, avail.right() - w))
            y = max(avail.top(), min(y, avail.bottom() - h))
        self.move(x, y)
        # Hard ceiling: never taller than the usable screen height, so the
        # window can't spill off the bottom on small screens — the inner
        # QScrollArea scrolls the shortcut list instead.  The floor is also
        # lowered when the screen itself is shorter than 560px so we never
        # force a window taller than the display.
        if avail is not None:
            max_h = max(200, avail.height() - 16)
            self.setMaximumHeight(max_h)
            self.setMinimumHeight(min(560, max_h))

    def keyPressEvent(self, ev: Any) -> None:  # noqa: N802
        # Esc closes the editor (independent window, so this never clashes with
        # the main HUD's Esc handling).
        if ev.key() == Qt.Key.Key_Escape:
            self.close()
            ev.accept()
            return
        super().keyPressEvent(ev)

    # -- content ---------------------------------------------------------
    def _rebuild(self) -> None:
        for i in reversed(range(self._list_lay.count())):
            w = self._list_lay.itemAt(i).widget()
            if w is not None:
                w.deleteLater()
        sep = QLabel("系统功能（可自定义触发键）")
        sep.setStyleSheet("color: #8fb6d8; font-weight: bold;")
        self._list_lay.addWidget(sep)
        for key, label in _SYSTEM_ITEMS:
            self._list_lay.addWidget(
                _SystemCard(key, label, self._store, self._ctrl, self._on_change))
        sep2 = QLabel("自定义快捷按钮")
        sep2.setStyleSheet("color: #8fb6d8; font-weight: bold;")
        self._list_lay.addWidget(sep2)
        for sc in self._store.shortcuts:
            self._list_lay.addWidget(
                _ShortcutCard(sc, self._store, self._ctrl, self._on_change))
        self._list_lay.addStretch(1)

    def _add(self) -> None:
        import time as _t

        sid = f"sc_{int(_t.time() * 1000)}"
        sc = Shortcut(id=sid, label="新快捷按钮",
                      short=ActionSpec("launch", {"exe": ""}),
                      long=ActionSpec("inapp", {"command": "next_page"}))
        self._store.add(sc)
        self._on_change()
        self._rebuild()
