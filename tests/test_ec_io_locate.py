"""ec_io._locate_winring0_dll must resolve the vendored WinRing0x64.dll
across source / onedir / onefile layouts — without ever loading the driver
(so this test needs no admin rights)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ningbingjue.platform.ec_io as ec


def test_source_layout():
    # Running from src tree: here = src/ningbingjue/platform
    p = ec._locate_winring0_dll()
    assert p and os.path.exists(p), f"source locate failed: {p!r}"
    norm = p.replace("\\", "/")
    assert "src/ningbingjue/blocks/ryzenadj-win64" in norm, norm
    # dll + sys must sit together
    sys_path = os.path.join(os.path.dirname(p), "WinRing0x64.sys")
    assert os.path.exists(sys_path), f".sys not beside .dll: {sys_path}"


def test_onedir_layout():
    # Simulate a frozen onedir exe: <portable>/NingBingJue/NingBingJue.exe
    portable_exe = (
        "C:/Users/13567/WorkBuddy/2026-07-18-10-22-49/ningbingjue/"
        "NingBingJue_portable/NingBingJue/NingBingJue.exe"
    )
    if not os.path.exists(portable_exe):
        # fall back to dist build if portable not present
        portable_exe = (
            "C:/Users/13567/WorkBuddy/2026-07-18-10-22-49/ningbingjue/"
            "dist/NingBingJue/NingBingJue.exe"
        )
    assert os.path.exists(portable_exe), "no frozen exe present to simulate"
    orig = sys.executable
    sys.executable = portable_exe
    try:
        p = ec._locate_winring0_dll()
        assert p and os.path.exists(p), f"onedir locate failed: {p!r}"
        norm = p.replace("\\", "/")
        # must resolve to blocks/ryzenadj-win64 (dll + sys together)
        assert "blocks/ryzenadj-win64" in norm, norm
    finally:
        sys.executable = orig


def test_onefile_layout():
    # Simulate onefile: sys._MEIPASS is the temp extraction root.
    meipass = (
        "C:/Users/13567/WorkBuddy/2026-07-18-10-22-49/ningbingjue/"
        "NingBingJue_portable/NingBingJue/_internal"
    )
    assert os.path.exists(meipass), "no _internal to simulate _MEIPASS"
    orig_meipass = getattr(sys, "_MEIPASS", None)
    sys._MEIPASS = meipass
    try:
        p = ec._locate_winring0_dll()
        assert p and os.path.exists(p), f"onefile locate failed: {p!r}"
    finally:
        if orig_meipass is None:
            del sys._MEIPASS
        else:
            sys._MEIPASS = orig_meipass


if __name__ == "__main__":
    test_source_layout()
    print("PASS source_layout")
    test_onedir_layout()
    print("PASS onedir_layout")
    test_onefile_layout()
    print("PASS onefile_layout")
    print("=== ALL ec_io locate tests passed ===")
