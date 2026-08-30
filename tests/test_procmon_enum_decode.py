"""Regression tests for the process-enumeration decode bug.

Historically ``_enum_wmic`` / ``_enum_cim`` decoded the subprocess stdout as
UTF-8.  On Windows ``wmic`` emits UTF-16LE (with BOM) when redirected to a
pipe, so decoding as UTF-8 injected a NUL byte between every character and
corrupted the CSV header.  ``row.get("CommandLine")`` then always returned
``None`` and every monitored process was reported as "not running" — the
process-monitor card stayed grey even for scripts that were clearly running.

These tests pin the real-world byte shape (a UTF-16LE wmic CSV containing an
AutoHotkey-hosted script) and prove the decoded command line is now readable
and matched.
"""

import csv
import io

import pytest

from ningbingjue.platform.process_manager import (
    ProcessInfo,
    _decode_cli,
    match_running,
)


def _wmic_csv_bytes() -> bytes:
    """Build a realistic wmic ``/format:csv`` payload as raw UTF-16LE bytes.

    Mirrors what ``wmic process get ... /format:csv`` actually writes to a pipe
    on Windows: UTF-16 little-endian with a BOM, header first (note the leading
    ``Node`` column), then one row per process.
    """
    header = "Node,ProcessId,Name,CommandLine,WorkingSetSize,SessionId"
    # AutoHotkey hosts the script; the script path is a quoted command-line arg.
    row = (
        "DESKTOP,1234,AutoHotkeyU64.exe,"
        '"C:\\AHK\\AutoHotkeyU64.exe" "D:\\scripts\\myscript.ahk",524288,1'
    )
    text = "\ufeff" + header + "\n" + row + "\n"
    return text.encode("utf-16")


def test_decode_cli_recovers_wmic_utf16():
    raw = _wmic_csv_bytes()
    text = _decode_cli(raw)
    # No NUL bytes survived, and the header text is intact.
    assert "\x00" not in text
    assert "CommandLine" in text
    assert "AutoHotkeyU64.exe" in text


def test_old_utf8_decode_would_have_broken():
    """Document the original failure mode: UTF-8 decode of UTF-16 injects NULs
    and makes the CommandLine column unreadable."""
    raw = _wmic_csv_bytes()
    broken = raw.decode("utf-8", "ignore")
    assert "\x00" in broken  # NUL between every char
    reader = csv.DictReader(io.StringIO(broken))
    first = next(reader)
    # Header key is "CommandLine\x00" -> the clean key returns nothing.
    assert first.get("CommandLine") in (None, "")


def test_ahk_running_detected_after_fix():
    raw = _wmic_csv_bytes()
    text = _decode_cli(raw)
    reader = csv.DictReader(io.StringIO(text))
    row = next(reader)
    info = ProcessInfo(
        pid=int(row["ProcessId"]),
        name=row["Name"].strip(),
        cmdline=row["CommandLine"].strip(),
        session_id=int(row["SessionId"] or 0),
    )
    # Config stored with forward slashes (file dialog / hand-edited JSON).
    assert match_running("D:/scripts/myscript.ahk", [info]) is True
    # A different script must NOT match.
    assert match_running("D:/scripts/other.ahk", [info]) is False


def test_cim_style_utf8_bom_also_decodes():
    raw = "Node,ProcessId,Name,CommandLine,WorkingSetSize,SessionId\r\n".encode(
        "utf-8-sig"
    ) + (
        "BOX,99,notepad.exe,C:\\Windows\\notepad.exe,1024,1\r\n".encode("utf-8")
    )
    text = _decode_cli(raw)
    assert "\x00" not in text
    assert "CommandLine" in text
