"""ProcMon AHK (AutoHotkey) monitoring tests.

Covers the new ability to monitor ``.ahk`` scripts, which run *under* an
AutoHotkey interpreter process (``AutoHotkeyU64.exe`` …) rather than as their
own image.  Path-separator normalization is asserted explicitly because a
config captured from a file dialog often uses ``/`` while a Windows process
command line uses ``\\`` — the original matching was separator-sensitive and
therefore missed running AHK scripts.
"""

from __future__ import annotations

from ningbingjue.platform.process_manager import (
    MockProcessManager,
    ProcessInfo,
    find_autohotkey,
    is_ahk_script,
    match_running,
    norm_path_sep,
)

try:
    from ningbingjue.blocks.procmon import ProcMonBlock
    from ningbingjue.core.config import AppConfig
    _HAVE_GUI = True
except Exception:  # PyQt6 not installed in headless environments
    _HAVE_GUI = False


def _ahk_proc(cmd: str) -> ProcessInfo:
    return ProcessInfo(pid=5, name="AutoHotkeyU64.exe", cmdline=cmd, memory_mb=1)


AHK_CMD = 'AutoHotkeyU64.exe "D:\\scripts\\myscript.ahk"'


# -- pure helper tests (no PyQt) ------------------------------------------
def test_is_ahk_script():
    assert is_ahk_script("x.ahk") and is_ahk_script("X.AHK")
    assert not is_ahk_script("x.exe")


def test_norm_path_sep():
    assert norm_path_sep("C:/a//b/") == "C:\\a\\b\\"


def test_match_running_ahk_separator_normalization():
    # config uses '/', running cmdline uses '\\' -> must still detect
    assert match_running("D:/scripts/myscript.ahk", [_ahk_proc(AHK_CMD)]) is True


def test_match_running_ahk_other_script():
    assert match_running("D:/scripts/other.ahk", [_ahk_proc(AHK_CMD)]) is False


def test_match_running_ahk_by_basename():
    # configured by filename only, running cmdline has full path
    assert match_running("myscript.ahk", [_ahk_proc(AHK_CMD)]) is True


def test_match_running_non_ahk_unchanged():
    assert match_running("C:/tools/a.exe",
                          [ProcessInfo(pid=1, name="powershell.exe",
                                       cmdline="C:/tools/a.exe -arg")]) is True
    assert match_running("C:/x/b.exe",
                          [ProcessInfo(pid=2, name="b.exe", cmdline="")]) is True
    assert match_running("C:/x/b.exe",
                          [ProcessInfo(pid=2, name="notepad.exe", cmdline="")]) is False


def test_stop_matching_separator_normalization():
    pm = MockProcessManager([_ahk_proc(AHK_CMD)])
    assert pm.stop_matching("D:/scripts/myscript.ahk") is True
    assert pm.stopped == [5]


def test_find_autohotkey_no_crash():
    # must not raise in a headless environment without AutoHotkey
    assert find_autohotkey() in (None, str)


# -- GUI-dependent tests (require PyQt6) ----------------------------------
def _cfg(items):
    cfg = AppConfig()
    cfg.procmon_configs = [{"Name": n, "Path": p} for n, p in items]
    return cfg


def test_procmon_detect_ahk_running():
    if not _HAVE_GUI:
        import pytest
        pytest.skip("PyQt6 not available")
    cfg = _cfg([("AHK脚本", "D:/scripts/myscript.ahk")])
    block = ProcMonBlock(cfg, MockProcessManager([_ahk_proc(AHK_CMD)]))
    block.refresh_panel()
    assert block.configs[0]["Running"] is True


def test_procmon_toggle_ahk_start():
    if not _HAVE_GUI:
        import pytest
        pytest.skip("PyQt6 not available")
    cfg = _cfg([("AHK脚本", "D:/scripts/myscript.ahk")])
    pm = MockProcessManager()  # not running yet
    block = ProcMonBlock(cfg, pm)
    block.refresh_panel()
    assert not block.configs[0]["Running"]
    block.toggle_config(0)  # start
    assert block.configs[0]["Running"] and pm.started == ["D:/scripts/myscript.ahk"]


def test_procmon_toggle_ahk_stop():
    if not _HAVE_GUI:
        import pytest
        pytest.skip("PyQt6 not available")
    cfg = _cfg([("AHK脚本", "D:/scripts/myscript.ahk")])
    pm = MockProcessManager([_ahk_proc(AHK_CMD)])  # already running
    block = ProcMonBlock(cfg, pm)
    block.refresh_panel()
    assert block.configs[0]["Running"]
    block.toggle_config(0)  # stop -> kill by matching script in cmdline
    assert not block.configs[0]["Running"] and pm.stopped == [5]
