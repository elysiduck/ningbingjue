#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test-launch/py-script/test_ryzenadj.py

Send arbitrary commands to ryzenadj.exe from Python and print back everything
ryzenadj returns (exit code + full stdout/stderr + the wrapper's success flag).

This deliberately REUSES the project's own ``RyzenAdj`` wrapper
(``src/ningbingjue/platform/ryzenadj.py``) instead of re-implementing the
subprocess call, so it inherits:

  * the ``0xC0000005`` (ACCESS_VIOLATION) "treated as success" quirk,
  * the non-ASCII install path -> %LOCALAPPDATA% copy,
  * the admin / ring0 privilege check,
  * the diagnostic logging to ``logs/ryzenadj_invoke.log``.

Design (agreed with the user):
  * Parameter pass-through: every argument after the script's own flags goes
    straight to ryzenadj. No arguments => default to ``-i`` (read-only health
    check, does NOT change hardware).
  * The user supplies the actual ryzenadj command ("我自己输入").

Usage:
    python test_ryzenadj.py                 # -> runs `ryzenadj.exe -i`
    python test_ryzenadj.py --max-gfxclk 800 --min-gfxclk 800   # Strix Point(HX370): 钉 GPU 到 800
    python test_ryzenadj.py --gfx-clk 800    # 注意：Renoir Only，HX370 上被 SMU 忽略！
    python test_ryzenadj.py --exe C:/path/ryzenadj.exe --stapm-limit 15000

Exit codes:
    0  ryzenadj reported success
    1  ryzenadj reported failure
    2  ryzenadj.exe could not be located

NOTE: .bat launchers must stay pure ASCII (GBK parsing rule); this .py is UTF-8.
"""

from __future__ import annotations

import argparse
import os
import sys

# Headless safety: importing the platform package pulls in PyQt (via
# platform/__init__.py -> OverlayWindow). Force the offscreen Qt platform so the
# import works on machines without a display. Harmless when a display exists.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# --- locate the project's `src` tree so we can import the wrapper -----------
# __file__ = <ROOT>/test-launch/py-script/test_ryzenadj.py
#   dirname x1 -> py-script
#   dirname x2 -> test-launch
#   dirname x3 -> <ROOT>  (project root, e.g. "ningbingjue")
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_SRC_DIR = os.path.join(_PROJECT_ROOT, "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from ningbingjue.platform.ryzenadj import (  # noqa: E402  (sys.path set above)
    RyzenAdj,
    SUCCESS_EXIT_CODE,
)

# Commands that only READ state and never change hardware. Anything else gets a
# WARNING banner because it will modify TDP / clocks / voltages on the APU.
_READONLY_ARGS = {"-i", "-h", "--help", "-v", "--version", "--info"}


def _resolve_default_exe() -> str | None:
    """Same resolution order the app uses, with a project-root fallback."""
    candidates = [
        os.path.join(
            _PROJECT_ROOT, "src", "ningbingjue", "blocks",
            "ryzenadj-win64", "ryzenadj.exe",
        ),
        os.path.join(
            _PROJECT_ROOT, "blocks", "ryzenadj-win64", "ryzenadj.exe",
        ),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def _humanize_code(code: int | None) -> str:
    if code is None:
        return "n/a (no exit code captured)"
    text = f"{code} (0x{code & 0xFFFFFFFF:08X})"
    if code == SUCCESS_EXIT_CODE:
        text += "  [ACCESS_VIOLATION 0xC0000005 -> wrapper treats as SUCCESS]"
    return text


def _parse_args(argv: list[str]) -> tuple[str | None, list[str]]:
    parser = argparse.ArgumentParser(
        description="Send commands to ryzenadj.exe and print its return value.",
        add_help=True,
    )
    parser.add_argument(
        "--exe", default=None,
        help="Explicit path to ryzenadj.exe (overrides auto-detection).",
    )
    # Everything else is forwarded verbatim to ryzenadj (unknown to us).
    args, unknown = parser.parse_known_args(argv)
    return args.exe, unknown


def main(argv: list[str]) -> int:
    requested_exe, ryzen_args = _parse_args(argv)

    # No ryzenadj args supplied -> default to the read-only health check.
    if not ryzen_args:
        ryzen_args = ["-i"]

    # Resolve the executable.
    exe = requested_exe or _resolve_default_exe()
    if not exe:
        print("=" * 64)
        print("[ERROR] ryzenadj.exe not found.")
        print("  Looked under:")
        print("    <ROOT>/src/ningbingjue/blocks/ryzenadj-win64/ryzenadj.exe")
        print("    <ROOT>/blocks/ryzenadj-win64/ryzenadj.exe")
        print("  Pass an explicit path with:  --exe C:\\path\\to\\ryzenadj.exe")
        print("=" * 64)
        return 2

    adj = RyzenAdj(exe)

    # Show the path the wrapper will actually use (after the non-ASCII -> copy).
    resolved_exe = adj._resolve_exe()
    available = adj.available

    print("=" * 64)
    print(" ryzenadj test launcher")
    print("=" * 64)
    print(f" exe (requested): {exe}")
    print(f" exe (resolved) : {resolved_exe}")
    print(f" command        : ryzenadj.exe {' '.join(ryzen_args)}")
    print(f" available      : {available}")

    # Warn before any command that mutates hardware state.
    touches_hw = not any(a in _READONLY_ARGS for a in ryzen_args)
    if touches_hw:
        print("-" * 64)
        print(" [WARNING] This command is NOT read-only.")
        print("           It will change TDP / GPU clock / voltages on the APU.")
        print("-" * 64)

    if not available:
        print("[ERROR] ryzenadj.exe is not available at the resolved path.")
        print("         (Check the path / driver / admin privileges.)")
        print("=" * 64)
        return 2

    # --- the actual send + capture -----------------------------------------
    ok, code, out = adj.invoke(ryzen_args, silent=False)

    print("-" * 64)
    print(" invoke result")
    print("-" * 64)
    print(f" exit code : {_humanize_code(code)}")
    print(f" success   : {ok}")
    print(" output:")
    if out:
        for line in out.replace("\r\n", "\n").split("\n"):
            print(f"   | {line}")
    else:
        print("   (no stdout/stderr captured)")
    print("-" * 64)
    print(f" detail log: {os.path.join(_PROJECT_ROOT, 'logs', 'ryzenadj_invoke.log')}")
    print("=" * 64)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
