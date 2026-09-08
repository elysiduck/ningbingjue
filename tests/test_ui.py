"""UI control tests — run where PyQt6 is installed (uses the offscreen platform
so they also pass on a headless CI/Windows host without a display)."""

from __future__ import annotations

import os

# Force the offscreen Qt platform BEFORE any PyQt6 import so widgets can be
# instantiated without a real display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication

from ningbingjue.core.color.rgb import Color
from ningbingjue.ui.rounded_button import RoundedButton


@pytest.fixture(scope="session", autouse=True)
def qapp() -> QApplication:
    """A single headless QApplication for the whole module."""
    app = QApplication.instance() or QApplication([])
    yield app


def test_rounded_button_set_colors():
    btn = RoundedButton()
    btn.set_colors(Color.from_rgb(10, 20, 30), Color.from_rgb(40, 50, 60),
                  Color.from_rgb(70, 80, 90), Color.from_rgb(100, 110, 120))
    assert btn._back == QColor(10, 20, 30)
    assert btn._normal_back == QColor(10, 20, 30)


def test_rounded_button_toggle_state():
    btn = RoundedButton()
    btn.set_button_state(True)
    assert btn._is_toggle is True
    assert btn._active is True
    btn.clear_button_state()
    assert btn._active is False
