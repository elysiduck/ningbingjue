"""Shortcut-button model + executor (gamepad / keyboard, long-press & short-press).

A *shortcut button* is a user-defined trigger that fires an *action*.  Each
shortcut supports two independent triggers:

* a **gamepad** button (SDL physical name, e.g. ``"a"``, ``"x"``) — works
  globally while the app keeps a lightweight gamepad listener alive in the
  background (so you can one-tap launch things while a game is in front);
* a **keyboard** key (a canonical token, e.g. ``"F5"``, ``"CTRL+A"``) — fired
  only while the app itself is the foreground window (no global keyboard hook,
  so we never steal keystrokes from other programs).

Both triggers resolve to **two actions** — a *short* press and a *long* press
(held past :data:`LONG_PRESS_MS`).  Each action is one of four kinds:

* ``launch`` — start an external program / open a file or URL;
* ``script`` — run a command line / script (PowerShell, cmd, …);
* ``macro``  — inject a keystroke sequence into the foreground window;
* ``inapp``  — invoke an in-app command via a callback wired by the host.

The module is deliberately **PyQt-free** so it can be unit-tested headless.
The host (``app_window``) owns all Qt glue: it builds the key tokens, feeds
press/release events to :class:`ShortcutEngine`, and supplies the in-app
handler.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
LONG_PRESS_MS = 500
"""Hold duration (ms) that distinguishes a long press from a short press."""

GAMEPAD_RESERVED = ("select", "start")
"""Gamepad buttons reserved by the shell (Select = toggle layout editor,
Start = focus the window).  They may NOT be reused as shortcut triggers, so
those two always-on behaviours never get shadowed by a user binding."""

ACTION_KINDS = ("launch", "script", "macro", "inapp")
"""The four supported action kinds."""

_UNKNOWN_ACTION = "inapp"  # fallback if a stored kind is unrecognised

# Default *system* (shell) bindings — the three always-on behaviours the user
# asked to make customizable.  Each maps a logical system action to an optional
# gamepad button and/or an optional keyboard token.  The defaults mirror the
# original hard-coded behaviour (Select -> layout editor, Start -> focus,
# Win+Ctrl+F8 -> bring-to-front); the editor lets the user rebind any of them.
_DEFAULT_SYSTEM: dict[str, dict[str, Optional[str]]] = {
    "open_layout_editor": {"gamepad": "select", "keyboard": None},
    "focus_window": {"gamepad": "start", "keyboard": None},
    "bring_to_front": {"gamepad": None, "keyboard": "Win+Ctrl+F8"},
}

# Win32 modifier flags for parsing a hotkey token (mirrors the app_window
# constants so the host can register a global hotkey from a stored token).
_MODMAP = {"WIN": 0x8, "CTRL": 0x2, "CONTROL": 0x2, "ALT": 0x1, "SHIFT": 0x4}


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class ActionSpec:
    """One action resolved from a (short or long) press."""

    kind: str = "launch"
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, d: Any) -> "ActionSpec":
        d = d or {}
        kind = str(d.get("kind", "launch")).lower()
        if kind not in ACTION_KINDS:
            kind = _UNKNOWN_ACTION
        return cls(kind=kind, params=dict(d.get("params", {}) or {}))


@dataclass
class Shortcut:
    """A single shortcut button definition."""

    id: str
    label: str = ""
    gamepad: Optional[str] = None   # SDL button name, or None
    keyboard: Optional[str] = None  # key token string, or None
    short: ActionSpec = field(default_factory=ActionSpec)
    long: ActionSpec = field(default_factory=ActionSpec)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "gamepad": self.gamepad,
            "keyboard": self.keyboard,
            "short": self.short.to_dict(),
            "long": self.long.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: Any) -> "Shortcut":
        d = d or {}
        return cls(
            id=str(d.get("id", "")) or f"sc_{id(d)}",
            label=str(d.get("label", "")),
            gamepad=(str(d["gamepad"]).lower() if d.get("gamepad") else None),
            keyboard=(str(d["keyboard"]) if d.get("keyboard") else None),
            short=ActionSpec.from_dict(d.get("short")),
            long=ActionSpec.from_dict(d.get("long")),
        )


class ShortcutStore:
    """Loads / saves the shortcut list (and system bindings) from
    ``shortcuts.json``.

    The on-disk format is a dict ``{"shortcuts": [...], "system": {...}}``.  An
    older list-only file is still accepted (system falls back to defaults).
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path
        self.shortcuts: list[Shortcut] = []
        self.system: dict[str, dict[str, Optional[str]]] = dict(_DEFAULT_SYSTEM)

    def load(self, path: Optional[str] = None) -> list[Shortcut]:
        path = path or self.path
        self.shortcuts = []
        self.system = dict(_DEFAULT_SYSTEM)
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8-sig") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self.shortcuts = [Shortcut.from_dict(x)
                                      for x in (data.get("shortcuts") or [])]
                    sys_in = data.get("system") or {}
                    if isinstance(sys_in, dict):
                        for k, d in _DEFAULT_SYSTEM.items():
                            cur = sys_in.get(k)
                            if isinstance(cur, dict):
                                self.system[k] = {
                                    "gamepad": cur.get("gamepad", d["gamepad"]),
                                    "keyboard": cur.get("keyboard", d["keyboard"]),
                                }
                            else:
                                self.system[k] = dict(d)
                elif isinstance(data, list):
                    self.shortcuts = [Shortcut.from_dict(x) for x in data]
            except Exception:
                self.shortcuts = []
                self.system = dict(_DEFAULT_SYSTEM)
        if path:
            self.path = path
        return self.shortcuts

    def save(self, path: Optional[str] = None) -> None:
        path = path or self.path
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "shortcuts": [s.to_dict() for s in self.shortcuts],
                        "system": self.system,
                    },
                    fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def by_id(self, sid: str) -> Optional[Shortcut]:
        for s in self.shortcuts:
            if s.id == sid:
                return s
        return None

    def add(self, sc: Shortcut) -> None:
        self.shortcuts.append(sc)

    def remove(self, sid: str) -> None:
        self.shortcuts = [s for s in self.shortcuts if s.id != sid]


# --------------------------------------------------------------------------
# Engine — routes live press/release events to actions
# --------------------------------------------------------------------------
class ShortcutEngine:
    """Holds the live shortcut store and turns press/release edges into actions.

    Long vs short press is decided on *release*: a press starts a timer; on
    release, if the hold exceeded :data:`LONG_PRESS_MS` the ``long`` action
    fires, otherwise the ``short`` action fires.  This is unambiguous and needs
    no extra "key released" animation.
    """

    def __init__(self, store: ShortcutStore, long_press_ms: int = LONG_PRESS_MS) -> None:
        self._store = store
        self.long_press_ms = long_press_ms
        self._inapp: Optional[Callable[[str], None]] = None
        # press-start timestamps: key = ("gp", btn) or ("kb", token)
        self._press_at: dict[tuple[str, str], float] = {}

    # -- wiring ----------------------------------------------------------
    def set_inapp_handler(self, cb: Callable[[str], None]) -> None:
        self._inapp = cb

    # -- trigger queries -------------------------------------------------
    def gamepad_triggers(self) -> set[str]:
        return {s.gamepad for s in self._store.shortcuts if s.gamepad}

    def keyboard_triggers(self) -> set[str]:
        return {s.keyboard for s in self._store.shortcuts if s.keyboard}

    def is_gamepad_trigger(self, btn: str) -> bool:
        if not btn or btn.lower() in GAMEPAD_RESERVED:
            return False
        return btn.lower() in self.gamepad_triggers()

    def is_keyboard_trigger(self, token: str) -> bool:
        if not token:
            return False
        return token in self.keyboard_triggers()

    # -- live events -----------------------------------------------------
    def handle_gamepad_press(self, btn: str) -> None:
        if not self.is_gamepad_trigger(btn):
            return
        self._press_at[("gp", btn.lower())] = time.monotonic()

    def handle_gamepad_release(self, btn: str) -> None:
        key = ("gp", btn.lower())
        t0 = self._press_at.pop(key, None)
        if t0 is None:
            return
        long = (time.monotonic() - t0) * 1000.0 >= self.long_press_ms
        self._fire_gamepad(btn.lower(), long)

    def handle_key_press(self, token: str) -> None:
        if not self.is_keyboard_trigger(token):
            return
        self._press_at[("kb", token)] = time.monotonic()

    def handle_key_release(self, token: str) -> None:
        key = ("kb", token)
        t0 = self._press_at.pop(key, None)
        if t0 is None:
            return
        long = (time.monotonic() - t0) * 1000.0 >= self.long_press_ms
        self._fire_keyboard(token, long)

    # -- firing ----------------------------------------------------------
    def _matches_gamepad(self, btn: str) -> list[Shortcut]:
        return [s for s in self._store.shortcuts if s.gamepad == btn]

    def _matches_keyboard(self, token: str) -> list[Shortcut]:
        return [s for s in self._store.shortcuts if s.keyboard == token]

    def _fire_gamepad(self, btn: str, long: bool) -> None:
        for s in self._matches_gamepad(btn):
            self._run(s.long if long else s.short)

    def _fire_keyboard(self, token: str, long: bool) -> None:
        for s in self._matches_keyboard(token):
            self._run(s.long if long else s.short)

    def _run(self, action: ActionSpec) -> None:
        try:
            if action.kind == "launch":
                _run_launch(action.params)
            elif action.kind == "script":
                _run_script(action.params)
            elif action.kind == "macro":
                send_keys(str(action.params.get("keys", "")))
            elif action.kind == "inapp":
                if self._inapp is not None:
                    self._inapp(str(action.params.get("command", "")))
        except Exception:
            # Never let a bad shortcut crash the input loop.
            pass


# --------------------------------------------------------------------------
# Action executors
# --------------------------------------------------------------------------
def _win_create_no_window() -> int:
    """CREATE_NO_WINDOW (0x08000000) — keep child processes windowless."""
    return 0x08000000


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = v.strip()
        return v.split() if v else []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


def _run_launch(params: dict[str, Any]) -> None:
    exe = str(params.get("exe", "") or "").strip()
    if not exe:
        return
    args = _as_list(params.get("args"))
    cwd = str(params.get("cwd", "") or "").strip() or None
    flags = 0
    if os.name == "nt":
        flags = _win_create_no_window()
    subprocess.Popen([exe, *args], cwd=cwd,
                     creationflags=flags, shell=False)


def _run_script(params: dict[str, Any]) -> None:
    cmd = str(params.get("cmd", "") or "").strip()
    if not cmd:
        return
    cwd = str(params.get("cwd", "") or "").strip() or None
    flags = 0
    if os.name == "nt":
        flags = _win_create_no_window()
    subprocess.Popen(cmd, cwd=cwd, shell=True,
                     creationflags=flags)


# --------------------------------------------------------------------------
# Keyboard macro sender (Windows only)
# --------------------------------------------------------------------------
_VK_COMMON = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "space": 0x20, "spacebar": 0x20,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12,
    "shift": 0x10, "capslock": 0x14, "numlock": 0x90,
    "scrolllock": 0x91, "printscreen": 0x2C, "pause": 0x13,
}


def _vk_for_token(part: str) -> Optional[int]:
    p = part.strip().lower()
    if not p:
        return None
    if p in _VK_COMMON:
        return _VK_COMMON[p]
    if p.startswith("f") and p[1:].isdigit():
        n = int(p[1:])
        if 1 <= n <= 24:
            return 0x70 + (n - 1)
    if len(p) == 1:
        try:
            import ctypes

            vk = ctypes.windll.user32.VkKeyScanW(ord(p)) & 0xFF
            return vk or None
        except Exception:
            return None
    return None


def hotkey_from_token(token: str) -> Optional[tuple[int, int]]:
    """Parse a key token (e.g. ``"Win+Ctrl+F8"``) into ``(mods, vk)`` Win32
    values for :func:`ctypes.windll.user32.RegisterHotKey`, or ``None`` if it
    cannot be resolved (no main key / unknown token).

    Used by the host to register the customizable global "bring to front"
    hotkey from a user-editable binding instead of a hard-coded combo.
    """
    if not token:
        return None
    mods = 0
    vk: Optional[int] = None
    for part in str(token).upper().split("+"):
        part = part.strip()
        if not part:
            continue
        if part in _MODMAP:
            mods |= _MODMAP[part]
        else:
            v = _vk_for_token(part)
            if v is not None:
                vk = v
    if vk is None:
        return None
    return (mods, vk)


def send_keys(text: str) -> None:
    """Inject a keystroke sequence into the foreground window.

    Whitespace-separated *combos*; within a combo, ``+`` joins keys held
    together, e.g. ``"ctrl+c alt+tab a"`` sends Ctrl+C, then Alt+Tab, then A.
    No-op on non-Windows platforms or if ``user32`` is unavailable.
    """
    if not text or os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class INPUT(ctypes.Structure):
            _fields_ = [
                ("type", wintypes.DWORD),
                ("ki", KEYBDINPUT),
                ("padding", ctypes.c_ubyte * 16),
            ]

        KEYEVENTF_KEYUP = 0x0002

        def _send(vk: int, up: bool) -> None:
            inp = INPUT()
            inp.type = 1  # INPUT_KEYBOARD
            inp.ki.wVk = vk
            inp.ki.wScan = 0
            inp.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
            inp.ki.time = 0
            inp.ki.dwExtraInfo = None
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

        for combo in str(text).split():
            parts = combo.split("+")
            vks = []
            for part in parts:
                vk = _vk_for_token(part)
                if vk is not None:
                    vks.append(vk)
            if not vks:
                continue
            for vk in vks:
                _send(vk, False)
            for vk in reversed(vks):
                _send(vk, True)
    except Exception:
        pass


if __name__ == "__main__":
    # Tiny smoke check (no side effects): model round-trips + engine gating.
    import tempfile

    store = ShortcutStore()
    store.add(Shortcut(id="s1", label="Test", gamepad="x",
                       short=ActionSpec("launch", {"exe": "notepad.exe"}),
                       long=ActionSpec("inapp", {"command": "next_page"})))
    fd, p = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    store.save(p)
    store2 = ShortcutStore(p)
    store2.load()
    assert len(store2.shortcuts) == 1
    eng = ShortcutEngine(store2)
    assert eng.is_gamepad_trigger("x")
    assert not eng.is_gamepad_trigger("select")  # reserved
    assert not eng.is_gamepad_trigger("start")
    # macro token resolution
    assert _vk_for_token("F5") == 0x74
    assert _vk_for_token("a") is not None
    print("shortcuts.py self-check OK")
    os.remove(p)
