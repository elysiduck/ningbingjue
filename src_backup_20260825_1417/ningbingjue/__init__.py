"""凝冰结魄 (Níngbīngjiépò) — Python port of the PowerShell overlay HUD.

This package is a faithful, modular re-implementation of the original
``ds8ZZZ.ps1`` + ``blocks/*.ps1`` overlay.  The core engine (color system,
block registry, navigation, theme, config) runs headless; the UI layer
(``ui.*``, PyQt6) and platform adapters (``platform.*``) are validated on the
Windows host.
"""

from __future__ import annotations

__version__ = "0.1.0"
