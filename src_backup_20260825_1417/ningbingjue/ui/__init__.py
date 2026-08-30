"""UI layer — PyQt6 custom-drawn controls.

These mirror the C# ``RoundedButton`` / ``RoundedPanel`` / ``TileButton`` and the
slider/breathing visuals.  Importing this package requires PyQt6 (only available
on the Windows host); the core engine never imports it, so tests run headless.
"""

from __future__ import annotations
