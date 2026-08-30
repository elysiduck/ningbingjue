"""rtss_deploy 部署逻辑（不依赖真实 RTSS / Windows 特定 API）。

通过 monkeypatch 把系统/内置目录、版本读取、释放/注册都替换为可控桩，
验证 ensure_rtss() 的分支决策。
"""
import os
import sys

import pytest

import ningbingjue.platform.rtss_deploy as dep


@pytest.fixture
def patch_all(monkeypatch, tmp_path):
    """把外部依赖全部替换为可控桩。"""
    state = {
        "system_dir": None,
        "bundle_dir": None,
        "sys_ver": None,
        "bundle_ver": None,
        "released": [],
        "registered": [],
        "hkcu": [],
        "target": str(tmp_path / "RTSS_target"),
    }

    monkeypatch.setattr(dep, "system_rtss_dir", lambda: state["system_dir"])
    monkeypatch.setattr(dep, "bundle_rtss_dir", lambda: state["bundle_dir"])
    monkeypatch.setattr(
        dep, "get_file_version",
        lambda p: (state["sys_ver"] if "system" in str(p) else state["bundle_ver"]),
    )
    monkeypatch.setattr(dep, "deploy_target_dir", lambda: state["target"])
    monkeypatch.setattr(dep, "_release_bundle",
                        lambda b, t: (state["released"].append((b, t)) or True))
    monkeypatch.setattr(dep, "_self_register",
                        lambda t: (state["registered"].append(t) or True))
    monkeypatch.setattr(dep, "_register_vulkan_hkcu",
                        lambda t: state["hkcu"].append(t))
    return state


def test_version_ge():
    assert dep._version_ge((7, 3, 4, 0), (7, 3, 0, 0)) is True
    assert dep._version_ge((7, 3, 0, 0), (7, 3, 4, 0)) is False
    assert dep._version_ge(None, (7, 3, 0, 0)) is False
    assert dep._version_ge((7, 3, 0, 0), None) is False


def test_ensure_rtss_uses_system_when_newer(patch_all):
    # 系统已装且版本 >= 内置 → 直接用系统，不释放文件。
    s = patch_all
    s["system_dir"] = r"C:\Program Files\RivaTuner Statistics Server"
    s["bundle_dir"] = r"X:\bundle"
    s["sys_ver"] = (7, 3, 4, 0)
    s["bundle_ver"] = (7, 3, 0, 0)
    state, text = dep.ensure_rtss()
    assert state == "installed"
    assert s["released"] == []          # 未释放
    assert "系统 RTSS" in text


def test_ensure_rtss_deploys_bundle_when_system_missing(patch_all):
    # 系统无 RTSS、内置有 → 释放 + 自注册 + Vulkan HKCU。
    s = patch_all
    s["system_dir"] = None
    s["bundle_dir"] = r"X:\bundle"
    s["bundle_ver"] = (7, 3, 0, 0)
    state, text = dep.ensure_rtss()
    assert state == "deployed"
    assert len(s["released"]) == 1
    assert len(s["registered"]) == 1
    assert len(s["hkcu"]) == 1
    assert "已部署" in text


def test_ensure_rtss_missing_when_nothing(patch_all):
    # 两路皆无 → 引导官方下载。
    s = patch_all
    s["system_dir"] = None
    s["bundle_dir"] = None
    state, text = dep.ensure_rtss()
    assert state == "missing"
    assert "未安装" in text


def test_ensure_rtss_release_failure_returns_missing(patch_all, monkeypatch):
    # 内置存在但释放失败 → 返回 missing（不假装成功）。
    s = patch_all
    s["system_dir"] = None
    s["bundle_dir"] = r"X:\bundle"
    s["bundle_ver"] = (7, 3, 0, 0)

    def _fail(b, t):
        return False
    monkeypatch.setattr(dep, "_release_bundle", _fail)
    state, text = dep.ensure_rtss()
    assert state == "missing"
    assert "释放失败" in text


def test_bundle_rtss_dir_finds_vendor(monkeypatch, tmp_path):
    # 源码态：src/ningbingjue/vendor/RTSS 存在且有 RTSS.exe → 命中。
    vendor = tmp_path / "vendor" / "RTSS"
    vendor.mkdir(parents=True)
    (vendor / "RTSS.exe").write_bytes(b"MZ")
    # platform 目录的上级 = src/ningbingjue；构造 fake __file__
    fake_platform = str(tmp_path / "platform" / "x.py")
    monkeypatch.setattr(dep, "__file__", fake_platform)
    # _MEIPASS 不存在（非冻结）
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert dep.bundle_rtss_dir() == str(vendor)


def test_bundle_rtss_dir_frozen(monkeypatch, tmp_path):
    # 冻结态：sys._MEIPASS/ningbingjue/vendor/RTSS 命中。
    meipass = tmp_path / "_MEIPASS" / "ningbingjue" / "vendor" / "RTSS"
    meipass.mkdir(parents=True)
    (meipass / "RTSS.exe").write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_MEIPASS"), raising=False)
    assert dep.bundle_rtss_dir() == str(meipass)


def test_bundle_rtss_dir_none_when_absent(monkeypatch):
    # 两路都没有 → None（默认构建未内嵌）。
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(dep, "__file__", r"C:\nope\platform\rtss_deploy.py")
    assert dep.bundle_rtss_dir() is None
