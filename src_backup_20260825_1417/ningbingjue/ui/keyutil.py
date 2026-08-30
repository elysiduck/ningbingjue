"""Shared keyboard-key -> canonical token helper.

Both the main window and the shortcut-button editor need to turn a
``QKeyEvent`` into the *same* stable string token (e.g. ``"F5"``, ``"A"``,
``"CTRL+A"``, ``"ALT+SHIFT+TAB"``) so a shortcut captured in the editor matches
the live key event at runtime.  Kept in its own PyQt-dependent module so the
PyQt-free :mod:`core.shortcuts` engine can stay headless-testable.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt


def key_token_from_event(ev) -> str:
    """Return a canonical token for ``ev`` (a ``QKeyEvent``)."""
    mods: list[str] = []
    m = ev.modifiers()
    if m & Qt.KeyboardModifier.ControlModifier:
        mods.append("CTRL")
    if m & Qt.KeyboardModifier.AltModifier:
        mods.append("ALT")
    if m & Qt.KeyboardModifier.ShiftModifier:
        mods.append("SHIFT")
    if m & Qt.KeyboardModifier.MetaModifier:
        mods.append("WIN")
    k = ev.key()
    if Qt.Key.Key_F1 <= k <= Qt.Key.Key_F24:
        name = "F" + str(k - Qt.Key.Key_F1 + 1)
    else:
        try:
            name = Qt.Key(k).name
        except Exception:
            name = str(k)
        if name.startswith("Key_"):
            name = name[4:]
    return "+".join(mods + [name]) if (mods or name) else ""
