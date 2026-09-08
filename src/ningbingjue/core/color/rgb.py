"""Color type modelling ``System.Drawing.Color`` from the original PowerShell script.

The original uses ``[System.Drawing.Color]::FromArgb(a, r, g, b)`` and linear
interpolation of the form ``int(Base + (Color - Base) * Ratio)`` followed by a
``[Math]::Min(255, [Math]::Max(0, x))`` clamp.  Python's ``int()`` truncates
towards zero exactly like a PowerShell ``[int]`` cast, so the math is identical.
"""

from __future__ import annotations

from dataclasses import dataclass


def _clamp(v: int) -> int:
    """Clamp an integer channel value to the [0, 255] range (mirrors PS Math.Min/Max)."""
    if v < 0:
        return 0
    if v > 255:
        return 255
    return int(v)


@dataclass(frozen=True, slots=True)
class Color:
    """Immutable ARGB color. Equality compares all four 8-bit channels."""

    a: int = 255
    r: int = 0
    g: int = 0
    b: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "a", _clamp(self.a))
        object.__setattr__(self, "r", _clamp(self.r))
        object.__setattr__(self, "g", _clamp(self.g))
        object.__setattr__(self, "b", _clamp(self.b))

    # -- constructors -----------------------------------------------------
    @classmethod
    def from_argb(cls, a: int, r: int, g: int, b: int) -> "Color":
        return cls(a, r, g, b)

    @classmethod
    def from_rgb(cls, r: int, g: int, b: int) -> "Color":
        return cls(255, r, g, b)

    @classmethod
    def from_rgba(cls, r: int, g: int, b: int, a: int = 255) -> "Color":
        return cls(a, r, g, b)

    @classmethod
    def transparent(cls) -> "Color":
        return cls(0, 0, 0, 0)

    @classmethod
    def gray(cls) -> "Color":
        return cls(255, 128, 128, 128)

    # -- accessors --------------------------------------------------------
    @property
    def rgb(self) -> tuple[int, int, int]:
        return (self.r, self.g, self.b)

    @property
    def argb(self) -> tuple[int, int, int, int]:
        return (self.a, self.r, self.g, self.b)

    def is_transparent(self) -> bool:
        return self.a == 0

    # -- serialization ----------------------------------------------------
    def to_hex(self) -> str:
        """``RRGGBB`` (6 hex chars) — for Qt/CSS stylesheets and UI use."""
        return f"{self.r:02X}{self.g:02X}{self.b:02X}"

    def to_argb_hex(self) -> str:
        """``AARRGGBB`` (8 hex chars) — matches the original ``ColorToHex``."""
        return f"{self.a:02X}{self.r:02X}{self.g:02X}{self.b:02X}"

    @classmethod
    def from_hex(cls, hex_str: str | None) -> "Color":
        """``HexToColor`` — parses 6 (``RRGGBB``, alpha 255) or 8 (``AARRGGBB``)
        hex digits; returns Transparent on empty/garbage input."""
        if not hex_str:
            return cls.transparent()
        h = hex_str.lstrip("#")
        try:
            if len(h) == 6:
                r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
                return cls(255, r, g, b)
            if len(h) == 8:
                a = int(h[0:2], 16)
                r = int(h[2:4], 16)
                g = int(h[4:6], 16)
                b = int(h[6:8], 16)
                return cls(a, r, g, b)
        except ValueError:
            pass
        return cls.transparent()


def color_to_hex(color: Color | None) -> str:
    """``ColorToHex`` — ``AARRGGBB`` (``00000000`` for ``$null``)."""
    if color is None:
        return "00000000"
    return color.to_argb_hex()


def hex_to_color(hex8: str | None) -> Color:
    """``HexToColor`` — parses 8 hex digits, returns Transparent on failure."""
    return Color.from_hex(hex8)
