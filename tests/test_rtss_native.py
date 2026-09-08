"""rtss_native（ctypes 控制层）+ Win32RtssCli.set_limit 的单元测试。

不依赖真实 RTSS 安装：通过 monkeypatch ctypes.CDLL / native_apply_limit 验证
降级逻辑与调用顺序，确保「native 失败必降级 PS、native 成功不再拉 PS」。
"""

import os

import pytest

from ningbingjue.platform import rtss_cli, rtss_native
from ningbingjue.platform.rtss_cli import Win32RtssCli


def _make_fake_dll(record):
    class FakeDll:
        def __getattr__(self, name):
            def fn(*_a, **_k):
                record.append(name)
            return fn
    return FakeDll()


def _setup_dir(tmp_path, with_dll=True):
    d = tmp_path / "RTSS"
    prof = d / "Profiles"
    prof.mkdir(parents=True)
    (prof / "Global").write_text("Limit=0\r\nFramerate=1\r\n", encoding="mbcs")
    if with_dll:
        (d / "RTSSHooks64.dll").write_bytes(b"FAKE")
    return str(d)


def test_native_writes_limit_and_calls_hooks(tmp_path, monkeypatch):
    rtss_dir = _setup_dir(tmp_path, with_dll=True)
    record = []
    monkeypatch.setattr(rtss_native.ctypes, "CDLL", lambda _p: _make_fake_dll(record))
    ok = rtss_native.native_apply_limit(rtss_dir, 60, rtss_running=True)
    assert ok is True
    content = (tmp_path / "RTSS" / "Profiles" / "Global").read_text(encoding="mbcs")
    assert "Limit=60" in content
    assert record == ["LoadProfile", "SaveProfile", "UpdateProfiles"]


def test_native_unlimited_writes_zero(tmp_path, monkeypatch):
    rtss_dir = _setup_dir(tmp_path, with_dll=True)
    record = []
    monkeypatch.setattr(rtss_native.ctypes, "CDLL", lambda _p: _make_fake_dll(record))
    ok = rtss_native.native_apply_limit(rtss_dir, 0, rtss_running=True)
    assert ok is True
    content = (tmp_path / "RTSS" / "Profiles" / "Global").read_text(encoding="mbcs")
    assert "Limit=0" in content


def test_native_missing_dll_returns_false(tmp_path, monkeypatch):
    rtss_dir = _setup_dir(tmp_path, with_dll=False)
    ok = rtss_native.native_apply_limit(rtss_dir, 60, rtss_running=True)
    assert ok is False


def test_native_not_running_false(tmp_path, monkeypatch):
    rtss_dir = _setup_dir(tmp_path, with_dll=True)
    record = []
    monkeypatch.setattr(rtss_native.ctypes, "CDLL", lambda _p: _make_fake_dll(record))
    ok = rtss_native.native_apply_limit(rtss_dir, 60, rtss_running=False)
    assert ok is False
    assert record == []  # 不应写文件也不应调 DLL


def test_native_alive_probe_false(tmp_path, monkeypatch):
    rtss_dir = _setup_dir(tmp_path, with_dll=True)
    record = []
    monkeypatch.setattr(rtss_native.ctypes, "CDLL", lambda _p: _make_fake_dll(record))
    monkeypatch.setattr(rtss_native, "_rtss_alive", lambda: False)
    ok = rtss_native.native_apply_limit(rtss_dir, 60, rtss_running=None)
    assert ok is False
    assert record == []


def test_cli_set_limit_native_first(tmp_path, monkeypatch):
    cli = Win32RtssCli(ps1_path="nonexistent.ps1")
    native_calls = []
    monkeypatch.setattr(cli, "_resolve_rtss_dir", lambda: str(tmp_path / "RTSS"))
    monkeypatch.setattr(cli, "is_rtss_running", lambda: True)
    monkeypatch.setattr(
        rtss_cli, "native_apply_limit",
        lambda d, l, running: (native_calls.append((d, l, running)) or True),
    )
    ps_calls = []
    monkeypatch.setattr(cli, "_run_ps1", lambda lim: (ps_calls.append(lim) or True))
    cli._enabled = True
    assert cli.set_limit(60) is True
    assert native_calls and native_calls[0][1] == 60
    assert ps_calls == []  # native 成功 → 不降级 PS


def test_cli_set_limit_falls_back_to_ps(tmp_path, monkeypatch):
    cli = Win32RtssCli(ps1_path="nonexistent.ps1")
    # 准备 RTSS 目录 + Global profile：PS 路径会写它，随后 set_limit 读回验证。
    rtss_dir = str(tmp_path / "RTSS")
    prof = os.path.join(rtss_dir, "Profiles")
    os.makedirs(prof, exist_ok=True)
    with open(os.path.join(prof, "Global"), "w", encoding="mbcs") as fh:
        fh.write("Limit=0\r\n")
    monkeypatch.setattr(cli, "_resolve_rtss_dir", lambda: rtss_dir)
    monkeypatch.setattr(cli, "is_rtss_running", lambda: True)
    monkeypatch.setattr(rtss_cli, "native_apply_limit", lambda d, l, running: False)
    ps_calls = []
    def fake_ps(lim, d=None):
        ps_calls.append(lim)
        # 模拟 PS 真正写入全局 profile，使随后的读回验证通过。
        if d:
            with open(os.path.join(d, "Profiles", "Global"), "w", encoding="mbcs") as fh:
                fh.write(f"Limit={lim}\r\n")
        return True
    monkeypatch.setattr(cli, "_run_ps1", fake_ps)
    cli._enabled = True
    assert cli.set_limit(60) is True
    assert ps_calls == [60]  # native 失败 → 降级 PS，传 effective=60


def test_resolve_rtss_dir_includes_localappdata():
    # 仅验证候选目录列表包含用户可写部署路径（不依赖真实存在）。
    dirs = rtss_cli._rtss_install_dirs()
    assert any("NingBingJue" in d and "RTSS" in d for d in dirs)
