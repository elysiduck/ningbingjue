#!/usr/bin/env python3
"""Frozen-mode launcher for 凝冰结魄 (NingBingJiePo).

This is the PyInstaller entry point. When the program is bundled
(``sys.frozen`` is set by PyInstaller), it points the app's data/resource
root at the directory that contains the executable and then delegates to
``run.py``'s CLI exactly as in development.

In normal (un-frozen) development this script does nothing special — it just
calls ``run.main()`` unchanged, so the dev workflow is unaffected.

Behavior:
  * no args        -> GUI mode (adds ``--gui``)
  * --headless     -> dropped; run.py defaults to headless engine self-test
  * --root <dir>   -> forwarded to run.py as the script root
"""
from __future__ import annotations

import os
import sys

if getattr(sys, "frozen", False):
    # PyInstaller 6.x onedir puts all bundled content (the package, blocks/,
    # layout.json, config/, buttons/, Qt DLLs) into an _internal directory and
    # sets sys._MEIPASS to point at it. The executable itself sits one level up.
    # script_root must point at that content dir, not at the exe's directory.
    _EXE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    _candidates = [
        getattr(sys, "_MEIPASS", None),
        _EXE_DIR,
        os.path.join(_EXE_DIR, "_internal"),
    ]
    _candidates = [c for c in _candidates if c]
    # Pick the directory that actually holds our vendored ryzenadj binary.
    _script_root = None
    for _c in _candidates:
        if os.path.exists(
            os.path.join(_c, "blocks", "ryzenadj-win64", "ryzenadj.exe")
        ):
            _script_root = _c
            break
    if _script_root is None:
        _script_root = _candidates[0]
    # script_root for the app: blocks/, layout.json, logs/, buttons/, config/.
    os.environ.setdefault("NINGBING_ROOT", _script_root)
    if _script_root not in sys.path:
        sys.path.insert(0, _script_root)

from run import main

if __name__ == "__main__":
    # ``run.main()`` only understands ``--root`` and ``--gui``; headless is its
    # default (no ``--gui``). So: no args -> GUI; an explicit ``--headless`` is
    # simply dropped (headless is the default without ``--gui``).
    if "--headless" in sys.argv:
        sys.argv = [a for a in sys.argv if a != "--headless"]
    elif "--gui" not in sys.argv:
        sys.argv = [sys.argv[0], "--gui", *sys.argv[1:]]
    raise SystemExit(main())
