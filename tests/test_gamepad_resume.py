"""Regression tests for the gamepad-after-hibernation fix.

Scenario: SDL opens a controller handle at startup.  When the OS suspends /
resumes (hibernates), Windows tears down and re-enumerates the device stack.
The old ``SDL_GameController*`` handle now points at a dead device context and
``SDL_GameControllerGetButton`` keeps returning 0 — the gamepad silently dies.
Because ``SDL_NumJoysticks`` still reports the same count, the poll loop never
auto-closes the stale handle.

The fix has two halves:
  * ``SDLGamepadBackend.reinit`` — explicitly quit + re-init the gamecontroller
    subsystem and drop every handle (driven by the WM_POWERBROADCAST resume
    handler in app_window.py).
  * ``SDLGamepadBackend._refresh`` — self-healing: if an opened handle reports
    ``SDL_GameControllerGetAttached == 0`` it is pruned and re-opened, so the
    backend recovers even if the resume event is missed.

These tests drive both with a mock SDL2 DLL.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from ningbingjue.ui.gamepad import MockBackend, SDLGamepadBackend


class _MockSDL:
    """Minimal ctypes-like stand-in for the SDL2 DLL the backend calls.

    Simulates one (or more) gamepads that can be flipped to "detached" (as a
    real SDL handle becomes after the OS re-enumerates the device on resume)
    and re-opened afterwards.
    """

    def __init__(self, num_joysticks: int = 1) -> None:
        self.num = num_joysticks
        self._next_handle = 1
        self.handles: dict[int, str] = {}
        self.attached: dict[int, bool] = {}
        self.closed: list[int] = []
        self.quit_calls = 0
        self.init_calls = 0
        self.init_should_fail = False

    # -- SDL API surface the backend touches -------------------------------
    def SDL_InitSubSystem(self, sub):  # noqa: N802 - SDL camelCase
        self.init_calls += 1
        return 0 if not self.init_should_fail else -1

    def SDL_QuitSubSystem(self, sub):  # noqa: N802 - SDL camelCase
        self.quit_calls += 1
        return 0

    def SDL_NumJoysticks(self):  # noqa: N802 - SDL camelCase
        return self.num

    def SDL_IsGameController(self, i):  # noqa: N802 - SDL camelCase
        return 1 if 0 <= i < self.num else 0

    def SDL_GameControllerOpen(self, i):  # noqa: N802 - SDL camelCase
        h = self._next_handle
        self._next_handle += 1
        self.handles[h] = f"Pad{i}".encode()  # SDL returns a C char* -> bytes
        self.attached[h] = True
        return h

    def SDL_GameControllerClose(self, h):  # noqa: N802 - SDL camelCase
        self.closed.append(h)
        self.handles.pop(h, None)
        self.attached.pop(h, None)

    def SDL_GameControllerGetAttached(self, h):  # noqa: N802 - SDL camelCase
        return 1 if self.attached.get(h, True) else 0

    def SDL_GameControllerGetButton(self, h, btn):  # noqa: N802 - SDL camelCase
        return 0

    def SDL_GameControllerGetAxis(self, h, axis):  # noqa: N802 - SDL camelCase
        return 0

    def SDL_GameControllerName(self, h):  # noqa: N802 - SDL camelCase
        return self.handles.get(h, b"")

    def SDL_PumpEvents(self):  # noqa: N802 - SDL camelCase
        return None


def _make_backend(mock: _MockSDL) -> SDLGamepadBackend:
    """Build a backend wired to ``mock`` (bypassing the real SDL load)."""
    b = SDLGamepadBackend()
    b._failed = False
    b._sdl = mock
    b._controllers = []
    b._opened = set()
    return b


def test_self_heal_prunes_detached_handle():
    """A stale (detached) handle must be dropped and reported disconnected,
    mirroring what happens to a pre-hibernation handle after resume."""
    mock = _MockSDL(num_joysticks=1)
    b = _make_backend(mock)

    st = b.poll()
    assert st.connected is True
    old_handle = b._controllers[0][0]

    # Hibernate: the open handle is no longer attached to a live device.
    mock.attached[old_handle] = False
    st2 = b.poll()
    # The stale handle is pruned AND a fresh one is opened in the same poll
    # (the device is still enumerated), so input recovers without an explicit
    # reinit — this is the defense-in-depth self-heal path.
    assert old_handle in mock.closed
    assert b._controllers, "stale handle should be replaced by a fresh one"
    new_handle = b._controllers[0][0]
    assert new_handle != old_handle
    assert st2.connected is True


def test_reinit_rebuilds_fresh_handle():
    """reinit() must force a fresh handle after the OS re-enumerates the pad,
    not keep recycling the dead one."""
    mock = _MockSDL(num_joysticks=1)
    b = _make_backend(mock)

    b.poll()
    old_handle = b._controllers[0][0]

    # Resume: device comes back; explicitly re-init the subsystem.
    ok = b.reinit()
    assert ok is True
    assert mock.quit_calls >= 1
    assert mock.init_calls >= 1
    assert b._controllers == []  # cleared by reinit, ready to re-open

    st = b.poll()
    assert st.connected is True
    new_handle = b._controllers[0][0]
    assert new_handle != old_handle  # genuinely fresh SDL handle


def test_reinit_failure_is_retryable():
    """If SDL can't re-init yet (device still waking up), reinit() reports
    failure and the caller can retry; poll() stays safely disconnected."""
    mock = _MockSDL(num_joysticks=1)
    mock.init_should_fail = True
    b = _make_backend(mock)

    ok = b.reinit()
    assert ok is False
    assert b._failed is True
    assert b.poll().connected is False

    # Device is ready now -> retry succeeds.
    mock.init_should_fail = False
    assert b.reinit() is True
    assert b._failed is False
    assert b.poll().connected is True


def test_reinit_with_no_sdl_does_full_reload(monkeypatch):
    """A backend that never loaded SDL should attempt a full reload on reinit
    instead of crashing on a None _sdl."""
    mock = _MockSDL(num_joysticks=1)
    b = _make_backend(mock)
    b._sdl = None  # simulate hard-load failure state

    # reinit's fallback path calls _load(); wire our mock so we don't probe the
    # real (possibly absent) SDL2 DLL / require actual hardware.
    def fake_load(self):
        self._sdl = mock
        self._failed = False

    monkeypatch.setattr(SDLGamepadBackend, "_load", fake_load)
    ok = b.reinit()
    assert ok is True
    assert b._sdl is mock
    assert b.poll().connected is True


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_reinit_gamepad_clears_edge_state(qapp):
    """InteractionController.reinit_gamepad must reset edge-detection so a
    button held across the suspend can't fire a spurious action on resume."""
    from ningbingjue.ui.interaction import InteractionController

    win = QWidget()
    ctrl = InteractionController(win, gamepad=MockBackend())
    # Seed some pre-existing edge state (as if a button was pressed pre-sleep).
    ctrl._gp_prev = MockBackend().state
    ctrl._gp_connected_prev = True
    ctrl._gp_had_disconnect = False

    ctrl.reinit_gamepad()

    assert ctrl._gp_prev is None
    assert ctrl._gp_connected_prev is False
    assert ctrl._gp_had_disconnect is True
