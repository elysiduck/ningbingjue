"""Headless logic test for the 4 new features (no live window shown).

Run with the bundled Python. Uses the offscreen Qt platform.
"""
import os
import sys
import time as _t

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication, QWidget

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from ningbingjue.ui.gamepad import GamepadState, MockBackend
from ningbingjue.ui.interaction import InteractionController
from ningbingjue.ui.keyutil import key_token_from_event
from ningbingjue.core.shortcuts import (
    ActionSpec, Shortcut, ShortcutStore, ShortcutEngine,
    hotkey_from_token, _DEFAULT_SYSTEM as _DEF_SYS,
)
VK_B = hotkey_from_token("B")[1]

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)


app = QApplication.instance() or QApplication(sys.argv)


class _Win(QObject):
    def __init__(self):
        super().__init__()
        self.active = True

    def isActiveWindow(self):
        return self.active


def S(**kw):
    return GamepadState(connected=True, **kw)


host = QWidget()
win = _Win()
mb = MockBackend()
ctrl = InteractionController(win, host, gamepad=mb, config=None)

dispatched = []
flags = {"toggle": 0, "focus": 0}
ctrl.on_toggle_layout = lambda: flags.__setitem__("toggle", flags["toggle"] + 1)
ctrl.on_focus_window = lambda: flags.__setitem__("focus", flags["focus"] + 1)
# Record dispatched actions; only actually run the real handler for the two
# shell actions (so their callbacks fire) — the HUD nav paths are skipped to
# avoid driving real UI in this headless harness.
_orig_dispatch = ctrl._dispatch_gamepad_action


def _rec(a):
    dispatched.append(a)
    if a in ("focus", "layout_toggle"):
        _orig_dispatch(a)


ctrl._dispatch_gamepad_action = _rec

# -- 1) GamepadState exposes select / start ----------------------------
check("GamepadState has select", hasattr(GamepadState(), "select"))
check("GamepadState has start", hasattr(GamepadState(), "start"))

# -- 2) Active mode: select -> layout_toggle, start -> focus ----------
mb.state = S()
ctrl._gp_prev = None
ctrl._poll_gamepad()

mb.state = S(select=True, start=True)
ctrl._poll_gamepad()
check("select dispatches layout_toggle", "layout_toggle" in dispatched)
check("start dispatches focus", "focus" in dispatched)
check("on_toggle_layout called", flags["toggle"] == 1)
check("on_focus_window called", flags["focus"] == 1)

# -- 3) Background mode: only focus + shortcuts, no HUD nav -----------
win.active = False
dispatched.clear()
flags["toggle"] = flags["focus"] = 0
mb.state = S()
ctrl._poll_gamepad()

mb.state = S(a=True, start=True)
ctrl._poll_gamepad()
check("background: A(confirm) suppressed", "confirm" not in dispatched)
check("background: start still focuses", "focus" in dispatched)
check("background: select suppressed", "layout_toggle" not in dispatched)

# -- 4) Shortcut engine: gamepad trigger (long & short) ---------------
store = ShortcutStore()
store.add(Shortcut(
    id="s1", label="t", gamepad="x",
    short=ActionSpec("inapp", {"command": "next_page"}),
    long=ActionSpec("inapp", {"command": "prev_page"})))
engine = ShortcutEngine(store)
hits = []
engine.set_inapp_handler(lambda c: hits.append(c))
ctrl.shortcut_engine = engine

win.active = True
dispatched.clear()
mb.state = S()
ctrl._poll_gamepad()

mb.state = S(x=True)
ctrl._poll_gamepad()
check("gamepad shortcut consumed (no dispatch)", "delete" not in dispatched)
mb.state = S()
ctrl._poll_gamepad()
check("gamepad shortcut SHORT -> next_page", hits == ["next_page"])

hits.clear()
mb.state = S(x=True)
ctrl._poll_gamepad()
_t.sleep(0.6)
mb.state = S()
ctrl._poll_gamepad()
check("gamepad shortcut LONG -> prev_page", hits == ["prev_page"])

# -- 5) key_token_from_event ------------------------------------------
e_a = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier)
check("token plain A", key_token_from_event(e_a) == "A")
e_ca = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
check("token CTRL+A", key_token_from_event(e_ca) == "CTRL+A")
e_f5 = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_F5, Qt.KeyboardModifier.NoModifier)
check("token F5", key_token_from_event(e_f5) == "F5")
e_cb = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_B,
                 Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
check("token CTRL+SHIFT+B", key_token_from_event(e_cb) == "CTRL+SHIFT+B")

# -- 6) engine reserves select/start ----------------------------------
check("engine rejects select trigger", not engine.is_gamepad_trigger("select"))
check("engine rejects start trigger", not engine.is_gamepad_trigger("start"))
check("engine accepts x trigger", engine.is_gamepad_trigger("x"))

# -- 7) shortcuts.json round-trip -------------------------------------
import tempfile
fd, p = tempfile.mkstemp(suffix=".json")
os.close(fd)
store.save(p)
store2 = ShortcutStore(p)
store2.load()
check("shortcuts.json round-trip", len(store2.shortcuts) == 1
      and store2.shortcuts[0].gamepad == "x")
os.remove(p)

# -- 8) system bindings customization (Select/Start rebindable) --------
from ningbingjue.core.shortcuts import hotkey_from_token

# store seeds system defaults on creation
fresh = ShortcutStore()
check("system default: open_layout_editor=select",
      fresh.system["open_layout_editor"]["gamepad"] == "select")
check("system default: focus_window=start",
      fresh.system["focus_window"]["gamepad"] == "start")
check("system default: bring_to_front=Win+Ctrl+F8",
      fresh.system["bring_to_front"]["keyboard"] == "Win+Ctrl+F8")

# Rebind focus_window to gamepad "b" (free; "x" is already a user shortcut
# and would be consumed by the engine first) -> Start no longer focuses, B does.
ctrl.set_system_bindings({
    "open_layout_editor": {"gamepad": "select", "keyboard": None},
    "focus_window": {"gamepad": "b", "keyboard": None},
    "bring_to_front": {"gamepad": None, "keyboard": "Win+Ctrl+F8"},
})
dispatched.clear()
flags["toggle"] = flags["focus"] = 0
mb.state = S(start=True)
ctrl._poll_gamepad()
check("rebind: Start no longer focuses", "focus" not in dispatched)
mb.state = S(b=True)
ctrl._poll_gamepad()
check("rebind: B now focuses", "focus" in dispatched)
# restore defaults for any later reuse
ctrl.set_system_bindings({k: dict(v) for k, v in _DEF_SYS.items()})

# -- 9) hotkey token parsing (global bring-to-front) -------------------
hk = hotkey_from_token("Win+Ctrl+F8")
check("hotkey Win+Ctrl+F8 parsed", hk == (0x8 | 0x2, 0x77))
check("hotkey F5 parsed", hotkey_from_token("F5") == (0, 0x74))
check("hotkey CTRL+SHIFT+B parsed",
      hotkey_from_token("CTRL+SHIFT+B") == (0x2 | 0x4, VK_B))
check("hotkey empty -> None", hotkey_from_token("") is None)
check("hotkey no main key -> None", hotkey_from_token("CTRL+SHIFT") is None)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
