# -*- coding: utf-8 -*-
"""platform/hwinfo_deploy.py — HWiNFO 便携自部署（2026-08-31 起：默认随包分发）。

为什么现在默认随包分发
======================
与 RTSS 完全同一个决策（见 ``platform/rtss_deploy.py`` 的模块 docstring）：
NBJ 有两种部署形态——Inno Setup 安装包与 portable 目录，而「移植到新机」通常是
直接拷 portable 目录，**portable 没有安装程序**，所以「安装时顺便装上」这个时机
根本不存在。真正要做的是**首次运行时自部署**。

用户已确认是**自用**场景，不再受再分发条款限制，故默认随包分发（除非显式
``NBJ_BUNDLE_HWINFO=0``）。附带两条历史权衡，现仅作背景保留：

1. **分发条款**：RTSS 当时结论是「EULA 禁止再分发」、HWiNFO 许可页需自行核对；
   自用场景已豁免，故不再作为默认关闭的理由。
2. **免费版共享内存只有 12 小时**：官方许可页 footnote 4 明确「运行 12 小时后
   自动停用，且需手动重新启用」。这意味着即使内嵌，长期常驻也不可靠——nbj 监控
   卡片已做 12h 倒计时预警 + 事后 pollTime 新鲜度兜底，缓解该限制。

因此本模块与 RTSS 同构：

  * 系统已装 HWiNFO 且版本 ≥ 内置 → 直接用系统的，**不释放任何文件**；
  * 否则若本构建内嵌了 HWiNFO（默认内嵌，NBJ_BUNDLE_HWINFO=0 才关）→ 释放到
    ``%LOCALAPPDATA%\\NingBingJue\\HWiNFO``（用户可写，零 UAC）；
  * 两路都没有 → 返回 ``missing``，由 UI 引导去官网下载。

INI 管理策略（grill 决策）
==========================
不随包原样释放第三方 INI。原因：拿到的便携包 INI 里有一串关闭开关
（``EC=0`` / ``SVID_VR=0`` / ``SMBus=0`` / ``LPC=0`` / ``GPUI2C=0`` / ``SMART=0``），
掌机电池数据走 EC/ACPI，``EC=0`` 会让电池类传感器整体消失；``SVID_VR=0`` 会砍掉
SVI 电压通道。我们把这些**全部不写**，让 HWiNFO 走自己的默认探测。

只白名单写入 4 个确实有用的键（``_GOOD_INI_KEYS``），其余一律不碰。

**绝不覆盖用户改动**：HWiNFO 会把 GUI 里的调整回写进 INI。升级/重部署时：
  * INI 不存在 → 写模板；
  * INI 已存在 → **只补写缺失的键**，已有值一律保留（哪怕是 ``SensorsSM=0``）。
    用户手动关掉的共享内存不会被我们偷偷打开——卡片会把「共享内存未开启」
    如实显示出来并提示，而不是替用户改配置。

所有异常都被吞掉并落日志，返回稳定的 ``(state, text)`` 元组，绝不抛给调用方。
"""

from __future__ import annotations

import os
import shutil
import sys
import time

# 版本读取复用 rtss_deploy 已有的 version.dll 封装，避免再抄一份 ctypes。
# 注意：HWiNFO 的 FileVersion 形如 "8.48-5990"（含连字符），该函数把它拆成
# (8, 0, 0, 0) —— 比较会退化成只比 major，但不会崩，对本模块的
# 「系统版本是否 ≥ 内置版本」够用。
from .rtss_deploy import _version_ge, get_file_version

EXE_NAME = "HWiNFO64.exe"
INI_NAME = "HWiNFO64.INI"

# 白名单：只写这 4 个键。其余全部交给 HWiNFO 自身默认值。
#   SensorsOnly=1        —— 开机只跑传感器（不开 Summary 窗口）
#   MinimalizeSensors=1  —— 传感器窗口直接缩到托盘
#   SensorsSM=1          —— 开共享内存（NBJ 读取传感器数据的唯一通道）
#   AutoUpdate=0         —— 关自动更新（便携场景，避免静默改版本）
_GOOD_INI_KEYS: "dict[str, str]" = {
    "SensorsOnly": "1",
    "MinimalizeSensors": "1",
    "SensorsSM": "1",
    "AutoUpdate": "0",
}


def deploy_target_dir() -> str:
    """部署目标目录（用户可写，零 UAC）。与 RTSS 同构。"""
    local = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.environ.get("USERPROFILE", ""), "AppData", "Local"
    )
    return os.path.join(local, "NingBingJue", "HWiNFO")


def _log(msg: str) -> None:
    try:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        d = os.path.join(root, "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "nbj_hwinfo.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [deploy] {msg}\n")
    except Exception:
        pass


def _is_windows() -> bool:
    return sys.platform == "win32"


# -- 系统已装 HWiNFO 探测 -------------------------------------------------
def _registry_hwinfo_dir() -> "str | None":
    """从卸载项读 HWiNFO 安装目录。

    HWiNFO 官方安装包是 Inno Setup，卸载键名为 ``HWiNFO64_is1``，
    里面有 ``InstallLocation`` 或 ``DisplayIcon``（后者形如 "...\\HWiNFO64.exe,0"）。
    """
    if not _is_windows():
        return None
    try:
        import winreg
    except Exception:
        return None
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for root, base in roots:
        try:
            hbase = winreg.OpenKey(root, base)
        except OSError:
            continue
        try:
            n = winreg.QueryInfoKey(hbase)[0]
            for i in range(n):
                try:
                    sub = winreg.EnumKey(hbase, i)
                except OSError:
                    continue
                if not sub.lower().startswith("hwinfo"):
                    continue
                try:
                    with winreg.OpenKey(hbase, sub) as k:
                        for val_name in ("InstallLocation", "DisplayIcon", "UninstallString"):
                            try:
                                raw = winreg.QueryValueEx(k, val_name)[0]
                            except OSError:
                                continue
                            if not isinstance(raw, str) or not raw:
                                continue
                            # DisplayIcon 可能带 ",0" 图标索引后缀
                            p = raw.split(",")[0].strip().strip('"')
                            if p.lower().endswith(".exe"):
                                d = os.path.dirname(p)
                            else:
                                d = p
                            if d and os.path.isdir(d) and os.path.exists(
                                    os.path.join(d, EXE_NAME)):
                                return d
                except OSError:
                    continue
        finally:
            try:
                winreg.CloseKey(hbase)
            except Exception:
                pass
    return None


def _common_hwinfo_dirs() -> "list[str]":
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    pf86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    return [
        os.path.join(pf, "HWiNFO64"),
        os.path.join(pf86, "HWiNFO64"),
        os.path.join(pf, "HWiNFO"),
    ]


def system_hwinfo_dir() -> "str | None":
    """系统已安装的 HWiNFO 目录（注册表优先，其次常见目录，最后部署目录）。"""
    reg = _registry_hwinfo_dir()
    if reg and os.path.isdir(reg):
        return reg
    for d in _common_hwinfo_dirs():
        if os.path.isdir(d) and os.path.exists(os.path.join(d, EXE_NAME)):
            return d
    # 我们自己上次部署过的也算「已有」，避免重复释放。
    t = deploy_target_dir()
    if os.path.exists(os.path.join(t, EXE_NAME)):
        return t
    return None


def bundle_hwinfo_dir() -> "str | None":
    """随包内置的 HWiNFO 目录（默认随包分发，NBJ_BUNDLE_HWINFO=0 才关闭）。

    冻结态：``sys._MEIPASS/ningbingjue/vendor/HWiNFO``。
    源码/调试态：``src/ningbingjue/vendor/HWiNFO``。
    返回 ``None`` 表示本构建未内嵌 HWiNFO。
    """
    candidates = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "ningbingjue", "vendor", "HWiNFO"))
    candidates.append(os.path.join(os.path.dirname(__file__), "..", "vendor", "HWiNFO"))
    for c in candidates:
        c = os.path.abspath(c)
        if os.path.isdir(c) and os.path.exists(os.path.join(c, EXE_NAME)):
            return c
    return None


# -- INI ------------------------------------------------------------------
def _read_ini_keys(path: str) -> "dict[str, str]":
    """极简 INI 读取：只要 ``[Settings]`` 段下的 ``k=v``，够本模块用。"""
    out: "dict[str, str]" = {}
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if not s or s.startswith(";") or s.startswith("#"):
                    continue
                if s.startswith("[") and s.endswith("]"):
                    continue
                if "=" not in s:
                    continue
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


def ensure_ini(target_dir: str) -> bool:
    """确保目标目录有一份「审计版」INI。

    * 不存在 → 写 ``[Settings]`` + 4 个白名单键；
    * 已存在 → **只补写缺失的键**（逐个 append 到 ``[Settings]`` 段），
      已有值一律不动，绝不覆盖用户在 HWiNFO GUI 里的调整。

    返回是否成功（best-effort，失败仅记日志）。
    """
    path = os.path.join(target_dir, INI_NAME)
    try:
        if not os.path.exists(path):
            lines = ["[Settings]"]
            for k, v in _GOOD_INI_KEYS.items():
                lines.append(f"{k}={v}")
            lines.append("")
            with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
                fh.write("\n".join(lines))
            _log(f"已写入审计版 INI → {path}")
            return True

        existing = _read_ini_keys(path)
        missing = [(k, v) for k, v in _GOOD_INI_KEYS.items() if k not in existing]
        if not missing:
            return True
        # 追加缺失键到文件末尾（[Settings] 段内）。绝不改已有行。
        with open(path, "a", encoding="utf-8", newline="\r\n") as fh:
            for k, v in missing:
                fh.write(f"{k}={v}\r\n")
        _log(f"INI 已存在，补写缺失键 {[k for k, _ in missing]} → {path}")
        return True
    except Exception as e:
        _log(f"写 INI 失败: {e!r}")
        return False


def _release_bundle(bundle_dir: str, target_dir: str) -> bool:
    """把随包 HWiNFO 释放到用户目录。

    已存在且版本不旧则跳过复制；释放完成后再补 INI（见 ``ensure_ini``）。
    """
    if os.path.isdir(target_dir) and os.path.exists(os.path.join(target_dir, EXE_NAME)):
        tv = get_file_version(os.path.join(target_dir, EXE_NAME))
        bv = get_file_version(os.path.join(bundle_dir, EXE_NAME))
        if _version_ge(tv, bv):
            _log("目标目录已存在且版本不旧，跳过释放")
            return True
        _log("目标目录版本较旧，覆盖释放")
    try:
        os.makedirs(os.path.dirname(target_dir), exist_ok=True)
        if os.path.isdir(target_dir):
            shutil.rmtree(target_dir, ignore_errors=True)
        shutil.copytree(bundle_dir, target_dir, dirs_exist_ok=True)
        _log(f"已释放 HWiNFO → {target_dir}")
        return True
    except Exception as e:
        _log(f"释放 HWiNFO 失败: {e!r}")
        return False


def ensure_hwinfo() -> "tuple[str, str]":
    """首次运行自部署主入口。返回 ``(state, text)``。

    state ∈ {installed, deployed, missing}：
      * installed —— 直接用系统已装 HWiNFO（版本 ≥ 内置），未释放文件
      * deployed  —— 释放了内置 HWiNFO 到用户目录
      * missing   —— 既无系统 HWiNFO 又无内置，需引导去官网下载

    注意：本函数**不负责拉起 HWiNFO**。HWiNFO 必须管理员权限才能读到传感器，
    静默拉起只会得到一个读不到数据的半残实例（详见 blocks/hwinfo.py），
    是否启动由用户点卡片上的按钮决定。
    """
    sys_dir = system_hwinfo_dir()
    bundle_dir = bundle_hwinfo_dir()

    if sys_dir:
        sys_ver = get_file_version(os.path.join(sys_dir, EXE_NAME))
        bun_ver = (get_file_version(os.path.join(bundle_dir, EXE_NAME))
                   if bundle_dir else None)
        if _version_ge(sys_ver, bun_ver):
            _log(f"检测到系统 HWiNFO v{sys_ver}（≥ 内置 v{bun_ver}），直接使用")
            return ("installed", f"使用系统 HWiNFO v{_fmt(sys_ver)}")

    if bundle_dir:
        target = deploy_target_dir()
        if _release_bundle(bundle_dir, target):
            ensure_ini(target)
            bun_ver = get_file_version(os.path.join(target, EXE_NAME))
            _log(f"HWiNFO 已部署 v{_fmt(bun_ver)} → {target}")
            return ("deployed", f"已部署 HWiNFO v{_fmt(bun_ver)}")
        return ("missing", "HWiNFO 释放失败（权限？）")

    _log("无系统 HWiNFO 且无内置，需引导官方下载")
    return ("missing", "HWiNFO 未安装，请前往 hwinfo.com 下载")


def _fmt(v: "tuple[int, int, int, int] | None") -> str:
    return ".".join(str(x) for x in v) if v else "?"
