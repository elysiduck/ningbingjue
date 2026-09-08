"""RTSS 共享内存帧率读取器单测 — 用伪造 RTSS 段验证解析与 FPS 计算。

覆盖：header/entry 解析、FPS 公式、PID 匹配取最新样本、签名校验、
无段降级、any_3d 互证信号。
"""

from __future__ import annotations

import ctypes
import sys

import pytest

sys.path.insert(0, "src")
from ctypes import wintypes

from ningbingjue.platform.rtss_smi import (
    RtssSmi, _SIG_RTSS, _HEADER_SIZE, _ENTRY_MIN_SIZE,
    _OFF_PID, _OFF_NAME, _OFF_FLAGS, _OFF_TIME0, _OFF_TIME1, _OFF_FRAMES,
)

MAP = "Local\\RTSS_SM_TEST"
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
FILE_MAP_WRITE = 0x0002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_kernel32.CreateFileMappingW.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR]
_kernel32.CreateFileMappingW.restype = wintypes.HANDLE
_kernel32.MapViewOfFile.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, ctypes.c_size_t]
_kernel32.MapViewOfFile.restype = ctypes.c_void_p


def _make_fake_rtss(apps, sig: int = _SIG_RTSS, entry_size: int = _ENTRY_MIN_SIZE):
    """构造一段伪 RTSS 共享内存，返回 (map_name, close_fn)。

    apps: list[(pid, name, frames, t0, t1)]；entry 大小取 _ENTRY_MIN_SIZE（280B），
    只填充前 280 字节中需要的字段，其余置 0。
    """
    header_size = _HEADER_SIZE
    arr_offset = header_size
    arr_size = len(apps) * entry_size
    total = arr_offset + arr_size
    h = _kernel32.CreateFileMappingW(INVALID_HANDLE_VALUE, None, 0x04, 0,
                                     total, MAP)
    assert h, f"CreateFileMappingW failed: {ctypes.get_last_error()}"
    p = _kernel32.MapViewOfFile(h, FILE_MAP_WRITE, 0, 0, 0)
    assert p, f"MapViewOfFile failed: {ctypes.get_last_error()}"

    def _w32(off, val):
        ctypes.memmove(ctypes.cast(p + off, ctypes.POINTER(ctypes.c_uint32)),
                       ctypes.byref(ctypes.c_uint32(val)), 4)

    _w32(0, sig)
    _w32(4, 0x00020000)          # version 2.0
    _w32(8, entry_size)
    _w32(12, arr_offset)
    _w32(16, arr_size)
    for i, (pid, name, frames, t0, t1) in enumerate(apps):
        base = arr_offset + i * entry_size
        _w32(base + _OFF_PID, pid)
        nbytes = name.encode("ascii")[: 260 - 1]
        ctypes.memmove(ctypes.cast(p + base + _OFF_NAME, ctypes.POINTER(ctypes.c_char * 260)),
                       nbytes + b"\x00" * (260 - len(nbytes)), 260)
        _w32(base + _OFF_FLAGS, 1)
        _w32(base + _OFF_TIME0, t0)
        _w32(base + _OFF_TIME1, t1)
        _w32(base + _OFF_FRAMES, frames)

    def _close():
        _kernel32.UnmapViewOfFile(ctypes.c_void_p(p))
        _kernel32.CloseHandle(h)

    return MAP, _close


@pytest.fixture()
def fake_rtss():
    map_name, close = _make_fake_rtss([
        (1234, "game.exe", 60, 1000, 2000),   # 60 帧 / 1s = 60 FPS
        (5678, "launcher.exe", 30, 1000, 1500),  # 30 帧 / 0.5s = 60 FPS
        (9999, "idle.exe", 0, 0, 0),           # 无有效样本
    ])
    yield map_name
    close()


def test_parse_apps_and_fps(fake_rtss):
    map_name = fake_rtss
    smi = RtssSmi(session_first=False)
    # 用假段：直接调用 _open 换成假段名 —— 用 monkeypatch 风格的替换
    snap = smi._parse(_map_of(map_name))
    assert snap.ok
    assert len(snap.apps) == 3
    by_pid = {a.pid: a for a in snap.apps}
    assert by_pid[1234].fps == pytest.approx(60.0)   # 60*1000/1000
    assert by_pid[5678].fps == pytest.approx(60.0)   # 30*1000/500
    assert by_pid[9999].fps is None


def test_fps_for_pid_latest(fake_rtss):
    snap = RtssSmi(session_first=False)._parse(_map_of(fake_rtss))
    assert snap.fps_for(1234) == pytest.approx(60.0)
    assert snap.fps_for(9999) is None
    assert snap.fps_for(None) is None


def test_any_3d_signal(fake_rtss):
    snap = RtssSmi(session_first=False)._parse(_map_of(fake_rtss))
    assert snap.any_3d is True


def test_bad_signature():
    map_name, close = _make_fake_rtss([], sig=0xDEADBEEF)
    try:
        snap = RtssSmi(session_first=False)._parse(_map_of(map_name))
        assert not snap.ok
        assert "signature" in snap.err
    finally:
        close()


def test_empty_apps_ok():
    map_name, close = _make_fake_rtss([])
    try:
        snap = RtssSmi(session_first=False)._parse(_map_of(map_name))
        assert snap.ok
        assert snap.apps == []
        assert not snap.any_3d
    finally:
        close()


def test_no_map_graceful():
    snap = RtssSmi(session_first=False).read()
    # 本机没有 RTSS → 必然降级（不抛）
    assert not snap.ok
    assert snap.err


def _map_of(map_name):
    """打开假 RTSS 段返回映射指针（与 RtssSmi._parse 的输入一致）。"""
    h = _kernel32.OpenFileMappingA(0x0004, False, map_name.encode("ascii"))
    assert h, f"open failed {ctypes.get_last_error()}"
    p = _kernel32.MapViewOfFile(h, 0x0004, 0, 0, 0)
    assert p
    return p
