"""Gamepad backend for the HUD interaction layer.

The original ``凝冰结魄`` overlays a gamepad-hint badge (A/B/X/Y) on the focused
control.  This module makes that literal: a small poll loop reads a gamepad and
the :class:`InteractionController` translates the stick / D-pad / face buttons
into the *same* navigation + activation calls the keyboard uses — so a gamepad
and the keyboard share one interaction path.

Backend
-------
A single :class:`SDLGamepadBackend` reads any controller via SDL2's
``SDL_GameController`` API (loaded directly from the SDL2 DLL that ships with
``pygame`` — no extra dependency).  SDL's built-in mapping database means the
*same* logical buttons (A/B/X/Y/LB/RB/L3/D-pad) work on **Xbox, PlayStation
(DualShock/DualSense), Switch Pro, and generic USB HID pads** — the old XInput
backend only saw Xbox-family controllers, which is why non-Xbox pads were
completely unresponsive.

Mapping (mirrors the hint convention A=主操作 / Y=保存 / X=删除 / B=取消):

* Left stick (past dead-zone) **or** D-pad  -> arrow navigation
* **A**  -> confirm the focused control (== Enter)
* **B**  -> cancel (== Esc; in edit mode asks the editor to discard)
* **X**  -> delete request (editor removes the selected item)
* **Y**  -> save request (editor persists)
* **LB** / **RB** -> jump to the previous / next panel
* **L3** (left stick pressed) -> help (疑惑解答)

The backend aggregates every connected controller and supports hot-plug (plug
in / unplug at any time; the next poll picks it up).
"""

from __future__ import annotations

import ctypes
import os
from ctypes import c_char_p, c_int, c_uint, c_void_p
from dataclasses import dataclass
from typing import Optional


@dataclass
class GamepadState:
    """Snapshot of a gamepad's digital + analog state for one poll.

    ``name`` is the SDL-reported controller name (empty when disconnected).
    ``lb`` / ``rb`` are the left / right shoulder buttons; ``l3`` / ``r3`` the
    stick presses.  All face / shoulder / stick fields are *physical* SDL
    button names; the :class:`InteractionController` turns them into logical
    actions via the config-driven binding map.
    """

    connected: bool = False
    name: str = ""
    # Face buttons.
    a: bool = False
    b: bool = False
    x: bool = False
    y: bool = False
    # Stick presses.
    l3: bool = False
    r3: bool = False
    # Shoulders.
    lb: bool = False
    rb: bool = False
    # Center / system buttons (SDL "back" == Select, "start" == Start).
    select: bool = False
    start: bool = False
    # D-pad.
    dpad_up: bool = False
    dpad_down: bool = False
    dpad_left: bool = False
    dpad_right: bool = False
    # Left thumb-stick, raw -32768..32767 (0 when centered).
    lx: int = 0
    ly: int = 0
    # Battery tier (SDL_JOYSTICK_POWER_*); 0 = unknown / n/a.  The HUD recolours
    # the gamepad glyph green -> red from this.  Wired pads report WIRED (5).
    power_level: int = 0

    def direction(self) -> tuple[int, int]:
        """Return ``(dx, dy)`` where each component is -1 / 0 / 1 (screen coords:
        ``dy == -1`` is up, ``dy == +1`` is down).

        D-pad wins; otherwise the left stick is used past its dead-zone.  The
        stick follows screen coordinates directly (push up = up, push down =
        down), so no extra Y flip is needed — this matches the D-pad branch.
        """
        if self.dpad_up:
            return (0, -1)
        if self.dpad_down:
            return (0, 1)
        if self.dpad_left:
            return (-1, 0)
        if self.dpad_right:
            return (1, 0)
        DEAD = 7849  # XINPUT_GAMEPAD_LEFT_THUMB_DEADZONE
        dx = 0 if abs(self.lx) < DEAD else (1 if self.lx > 0 else -1)
        dy = 0 if abs(self.ly) < DEAD else (1 if self.ly > 0 else -1)
        return (dx, dy)


class GamepadBackend:
    """Abstract gamepad source."""

    def poll(self) -> GamepadState:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass

    def rumble(self, low: int = 0x4000, high: int = 0, ms: int = 60) -> bool:
        """Fire a one-shot haptic pulse.  No-op on backends without a motor.

        Returns True if the pulse was accepted by at least one controller.
        """
        return False


class MockBackend(GamepadBackend):
    """In-memory backend for tests — set :attr:`state` between polls."""

    def __init__(self) -> None:
        self.state = GamepadState()

    def poll(self) -> GamepadState:
        return self.state


# --- SDL2 (GameController) backend ---------------------------------------
# We load the SDL2 DLL that ships with pygame and call the SDL_GameController
# API directly via ctypes.  This gives consistent A/B/X/Y mapping across every
# controller type (SDL's mapping DB), without pulling in another dependency.

# SDL_INIT_GAMECONTROLLER
_SDL_INIT_GAMECONTROLLER = 0x00002000

# SDL_GameControllerButton enum values (face / shoulder / stick / d-pad).
_BTN = {
    "a": 0,
    "b": 1,
    "x": 2,
    "y": 3,
    "back": 4,
    "guide": 5,
    "start": 6,
    "l3": 7,
    "r3": 8,
    "lb": 9,
    "rb": 10,
    "dup": 11,
    "ddown": 12,
    "dleft": 13,
    "dright": 14,
}
_AXIS_LX = 0
_AXIS_LY = 1

# SDL_JoystickPowerLevel enum (returned by SDL_GameControllerGetPowerLevel).
# The HUD maps these tiers to a green -> red colour ramp, so no extra strings.
SDL_JOYSTICK_POWER_UNKNOWN = 0
SDL_JOYSTICK_POWER_EMPTY = 1
SDL_JOYSTICK_POWER_LOW = 2
SDL_JOYSTICK_POWER_MEDIUM = 3
SDL_JOYSTICK_POWER_FULL = 4
SDL_JOYSTICK_POWER_WIRED = 5


def _find_sdl2_dll() -> Optional[str]:
    """Locate the SDL2 DLL bundled with pygame (preferred), else None to let
    ctypes resolve ``SDL2`` from the system search path."""
    try:


        p = os.path.join(os.path.dirname(pygame.__file__), "SDL2.dll")
        if os.path.exists(p):
            return p
    except Exception:
        pass
    return None


class SDLGamepadBackend(GamepadBackend):
    """Real gamepad reader using SDL2's GameController API.

    Sees Xbox + PlayStation + Switch + generic HID controllers, aggregates all
    connected pads, and supports hot-plug.  If SDL2 cannot be loaded it fails
    silently (``poll()`` always reports ``connected=False``) so the HUD falls
    back to keyboard / mouse only.
    """

    def __init__(self) -> None:
        self._failed = False
        self._sdl: Optional[ctypes.CDLL] = None
        self._controllers: list[tuple[c_void_p, str]] = []
        self._opened: set[int] = set()
        self._has_power = False  # SDL_GameControllerGetPowerLevel available?
        self._has_rumble = False  # SDL_GameControllerRumble available?
        self._load()

    def _load(self) -> None:
        try:
            path = _find_sdl2_dll()
            dll = ctypes.CDLL(path) if path else ctypes.CDLL("SDL2")
            dll.SDL_InitSubSystem.argtypes = [c_uint]
            dll.SDL_InitSubSystem.restype = c_int
            dll.SDL_QuitSubSystem.argtypes = [c_uint]
            dll.SDL_QuitSubSystem.restype = c_int
            dll.SDL_NumJoysticks.restype = c_int
            dll.SDL_IsGameController.argtypes = [c_int]
            dll.SDL_IsGameController.restype = c_int
            dll.SDL_GameControllerOpen.argtypes = [c_int]
            dll.SDL_GameControllerOpen.restype = c_void_p
            dll.SDL_GameControllerClose.argtypes = [c_void_p]
            dll.SDL_GameControllerClose.restype = None
            dll.SDL_GameControllerGetButton.argtypes = [c_void_p, c_int]
            dll.SDL_GameControllerGetButton.restype = c_int
            dll.SDL_GameControllerGetAxis.argtypes = [c_void_p, c_int]
            dll.SDL_GameControllerGetAxis.restype = c_int
            dll.SDL_GameControllerName.argtypes = [c_void_p]
            dll.SDL_GameControllerName.restype = c_char_p
            dll.SDL_GameControllerGetAttached.argtypes = [c_void_p]
            dll.SDL_GameControllerGetAttached.restype = c_int
            # Power level is optional: an SDL2 build without this symbol must
            # NOT break the whole backend, so only bind it when present.
            self._has_power = hasattr(dll, "SDL_GameControllerGetPowerLevel")
            if self._has_power:
                dll.SDL_GameControllerGetPowerLevel.argtypes = [c_void_p]
                dll.SDL_GameControllerGetPowerLevel.restype = c_int
            dll.SDL_PumpEvents.restype = None
            # Haotics: bind the rumble symbol only when the SDL2 build exposes
            # it (SDL >= 2.0.9).  A missing symbol must NOT break the backend.
            self._has_rumble = hasattr(dll, "SDL_GameControllerRumble")
            if self._has_rumble:
                dll.SDL_GameControllerRumble.argtypes = [c_void_p, c_uint, c_uint, c_uint]
                dll.SDL_GameControllerRumble.restype = c_int
            if dll.SDL_InitSubSystem(_SDL_INIT_GAMECONTROLLER) != 0:
                self._failed = True
                return
            self._sdl = dll
        except Exception:
            self._failed = True

    def _close_all(self) -> None:
        sdl = self._sdl
        if sdl is not None:
            for h, _ in self._controllers:
                try:
                    sdl.SDL_GameControllerClose(h)
                except Exception:
                    pass
        self._controllers = []
        self._opened = set()

    def _refresh(self) -> None:
        sdl = self._sdl
        if sdl is None:
            return
        try:
            sdl.SDL_PumpEvents()
            n = sdl.SDL_NumJoysticks()
            # Self-healing: if any already-opened controller is no longer
            # attached — its device context died (the OS re-enumerated it after
            # a suspend/resume, or the cable was yanked) — drop ALL open handles
            # and re-open from scratch below.  This lets the backend recover even
            # when the explicit resume re-init in app_window.py is missed: SDL
            # eventually processes the device removal on its own WM_DEVICECHANGE
            # and flips the attached flag, at which point we prune + reopen.
            # Without this, a stale handle looks "connected" (same joystick
            # count) and is never re-opened, so the gamepad goes dead silently.
            for h, _ in self._controllers:
                try:
                    if sdl.SDL_GameControllerGetAttached(h) == 0:
                        self._close_all()
                        break
                except Exception:
                    self._close_all()
                    break
            # A device removal shifts higher indices down; the safest fix is to
            # close everything and re-open from scratch when the count drops.
            if len(self._controllers) > n:
                self._close_all()
            for i in range(n):
                if i in self._opened:
                    continue
                try:
                    if sdl.SDL_IsGameController(i):
                        h = sdl.SDL_GameControllerOpen(i)
                        if h:
                            nm = sdl.SDL_GameControllerName(h)
                            name = nm.decode("utf-8", "replace") if nm else ""
                            self._controllers.append((h, name))
                            self._opened.add(i)
                except Exception:
                    pass
        except Exception:
            pass

    def rumble(self, low: int = 0x4000, high: int = 0, ms: int = 60) -> bool:
        """Fire a one-shot haptic pulse on every connected controller.

        Used for the long-press "threshold reached" feedback.  Weak by default
        (``low=0x4000`` ≈ 1/4 strength, single ~60ms) — strong enough to feel,
        cheap on the EC.  Returns True if at least one controller accepted the
        pulse.  A controller without a motor simply returns -1 and is ignored.
        """
        if self._failed or self._sdl is None or not self._has_rumble:
            return False
        sdl = self._sdl
        ok = False
        for h, _ in self._controllers:
            try:
                rc = sdl.SDL_GameControllerRumble(
                    h, c_uint(low), c_uint(high), c_uint(ms))
                if rc == 0:
                    ok = True
            except Exception:
                pass
        return ok

    def poll(self) -> GamepadState:
        if self._failed or self._sdl is None:
            return GamepadState()
        try:
            self._refresh()
            if not self._controllers:
                return GamepadState()
            sdl = self._sdl
            a = b = x = y = l3 = r3 = lb = rb = False
            sel = stt = dup = ddown = dleft = dright = False
            lx = ly = 0
            name = ""
            # Battery tier (green -> red in the HUD).  Guarded by _has_power so
            # a missing symbol never breaks the backend; read from the first pad.
            power = 0
            if self._has_power and self._controllers:
                try:
                    power = int(sdl.SDL_GameControllerGetPowerLevel(self._controllers[0][0]))
                except Exception:
                    power = 0
            for h, nm in self._controllers:
                try:
                    a |= bool(sdl.SDL_GameControllerGetButton(h, _BTN["a"]))
                    b |= bool(sdl.SDL_GameControllerGetButton(h, _BTN["b"]))
                    x |= bool(sdl.SDL_GameControllerGetButton(h, _BTN["x"]))
                    y |= bool(sdl.SDL_GameControllerGetButton(h, _BTN["y"]))
                    l3 |= bool(sdl.SDL_GameControllerGetButton(h, _BTN["l3"]))
                    r3 |= bool(sdl.SDL_GameControllerGetButton(h, _BTN["r3"]))
                    lb |= bool(sdl.SDL_GameControllerGetButton(h, _BTN["lb"]))
                    rb |= bool(sdl.SDL_GameControllerGetButton(h, _BTN["rb"]))
                    sel |= bool(sdl.SDL_GameControllerGetButton(h, _BTN["back"]))
                    stt |= bool(sdl.SDL_GameControllerGetButton(h, _BTN["start"]))
                    dup |= bool(sdl.SDL_GameControllerGetButton(h, _BTN["dup"]))
                    ddown |= bool(sdl.SDL_GameControllerGetButton(h, _BTN["ddown"]))
                    dleft |= bool(sdl.SDL_GameControllerGetButton(h, _BTN["dleft"]))
                    dright |= bool(sdl.SDL_GameControllerGetButton(h, _BTN["dright"]))
                    ax = sdl.SDL_GameControllerGetAxis(h, _AXIS_LX)
                    ay = sdl.SDL_GameControllerGetAxis(h, _AXIS_LY)
                    if abs(ax) > abs(lx):
                        lx = int(ax)
                    if abs(ay) > abs(ly):
                        ly = int(ay)
                    if not name and nm:
                        name = nm
                except Exception:
                    continue
            return GamepadState(
                connected=True,
                name=name,
                a=a, b=b, x=x, y=y, l3=l3, r3=r3, lb=lb, rb=rb,
                select=sel, start=stt,
                dpad_up=dup, dpad_down=ddown, dpad_left=dleft, dpad_right=dright,
                lx=lx, ly=ly, power_level=power,
            )
        except Exception:
            return GamepadState()

    def close(self) -> None:
        if self._sdl is not None:
            self._close_all()
            try:
                self._sdl.SDL_QuitSubSystem(_SDL_INIT_GAMECONTROLLER)
            except Exception:
                pass

    def reinit(self) -> bool:
        """Drop every open controller handle and re-initialize SDL's
        gamecontroller subsystem.

        This is mandatory after the OS suspends/resumes (hibernation): the
        ``SDL_GameController*`` handles opened *before* the sleep reference the
        dead pre-sleep device context, so ``SDL_GameControllerGetButton`` keeps
        returning 0 and the gamepad silently stops responding — even though the
        pad is physically back and ``SDL_NumJoysticks`` still reports it.  The
        stale handle looks "connected" to our poll loop (same joystick count),
        so it is never auto-closed.  Re-initializing forces SDL to re-scan the
        device list and hands us fresh handles.

        Returns ``True`` if the subsystem came back up (the next :meth:`poll`
        re-opens controllers).  Returns ``False`` (and marks the backend failed)
        if SDL could not re-init yet — the caller should retry after a delay.
        """
        if self._sdl is None:
            # Never loaded, or a previous hard failure: attempt a full reload.
            self._failed = False
            self._controllers = []
            self._opened = set()
            self._load()
            return (not self._failed) and self._sdl is not None
        sdl = self._sdl
        try:
            self._close_all()
            sdl.SDL_QuitSubSystem(_SDL_INIT_GAMECONTROLLER)
            if sdl.SDL_InitSubSystem(_SDL_INIT_GAMECONTROLLER) != 0:
                self._failed = True
                return False
            self._failed = False
            self._controllers = []
            self._opened = set()
            return True
        except Exception:
            self._failed = True
            return False


def create_backend(user_index: int = 0) -> GamepadBackend:
    """Return the SDL gamepad backend (sees every controller type).

    Falls back to a disconnected :class:`MockBackend` when pygame / SDL2 is
    unavailable (e.g. a headless test environment with no pygame installed),
    so the rest of the app still runs keyboard-only.

    ``user_index`` is accepted for API compatibility but ignored — the SDL
    backend aggregates all connected controllers automatically.
    """
    try:
        import pygame  # noqa: F401  (confirms the bundled SDL2 DLL is present)

        return SDLGamepadBackend()
    except Exception:
        return MockBackend()