"""RTSS 便携部署（首次运行自部署）。

背景（grill 决策）：NBJ 有两条部署形态——Inno Setup 安装包与 portable 目录。
「移植到新机」多是直接拷 portable 目录，而 portable **没有安装程序**，所以「安装时
自动安装」这个时机不存在。真正要做的是**首次运行时自部署**：

  * 检测系统是否已装 RTSS（注册表 + 常见目录）；
  * 系统已装且版本 ≥ 随包内置版本 → 直接用系统的，不释放任何文件；
  * 否则若随包内置了 RTSS（自用默认内嵌，仅 ``NBJ_BUNDLE_RTSS=0`` 才关） →
    释放到用户可写目录 ``%LOCALAPPDATA%\\NingBingJue\\RTSS``（零 UAC），
    调官方 ``RTSS.exe /I`` 自注册，Vulkan 层退 ``HKCU``；
  * 两路都没有（系统未装、且本构建未内嵌 RTSS，如 ``NBJ_BUNDLE_RTSS=0`` 纯净包）
    → 返回 ``missing``，由 UI 引导用户去官方下载。

所有异常都被吞掉并落日志，返回稳定的 ``(state, text)`` 元组，绝不抛给调用方。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time

CREATE_NO_WINDOW = 0x08000000

# 部署目标目录（用户可写，零 UAC；已加入 rtss_cli._rtss_install_dirs 探测）。
def deploy_target_dir() -> str:
    local = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.environ.get("USERPROFILE", ""), "AppData", "Local"
    )
    return os.path.join(local, "NingBingJue", "RTSS")


def _log(msg: str) -> None:
    try:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        d = os.path.join(root, "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "nbj_rtss.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [deploy] {msg}\n")
    except Exception:
        pass


# -- 版本读取（version.dll，仅在 Windows 有效）-----------------------------
def get_file_version(path: str) -> "tuple[int, int, int, int] | None":
    """读 PE 文件的 ``FileVersion`` 四元组 ``(major,minor,patch,build)``。

    用 ``version.dll!GetFileVersionInfo*`` 实现，跨 CRT 稳定。读不到（文件缺失 /
    无版本资源 / 非 Windows）返回 ``None``。
    """
    if not _is_windows():
        return None
    try:
        path_w = ctypes.c_wchar_p(path)
        sz = ctypes.windll.version.GetFileVersionInfoSizeW(path_w, ctypes.byref(ctypes.c_uint32(0)))
        if not sz:
            return None
        buf = ctypes.create_string_buffer(sz)
        if not ctypes.windll.version.GetFileVersionInfoW(path_w, 0, sz, buf):
            return None
        # 取 \\VarFileInfo\\Translation 决定语言-代码页子键
        class _LANG(ctypes.Structure):
            _fields_ = [("lang", ctypes.c_uint16), ("codepage", ctypes.c_uint16)]
        trans = ctypes.c_void_p(0)
        trans_sz = ctypes.c_uint(0)
        if not ctypes.windll.version.VerQueryValueW(
            buf, ctypes.c_wchar_p("\\VarFileInfo\\Translation"),
            ctypes.byref(trans), ctypes.byref(trans_sz),
        ):
            return None
        lang = _LANG.from_buffer_copy(ctypes.string_at(trans, ctypes.sizeof(_LANG)))
        sub = f"\\StringFileInfo\\{lang.lang:04x}{lang.codepage:04x}\\FileVersion"
        ptr = ctypes.c_void_p(0)
        vsz = ctypes.c_uint(0)
        if not ctypes.windll.version.VerQueryValueW(
            buf, ctypes.c_wchar_p(sub), ctypes.byref(ptr), ctypes.byref(vsz),
        ):
            return None
        raw = ctypes.string_at(ptr, vsz).split(b"\x00", 1)[0].decode("utf-8", "replace")
        parts = (raw.split(".") + ["0", "0", "0", "0"])[:4]
        return tuple(int(p) if p.isdigit() else 0 for p in parts)
    except Exception:
        return None


def _version_ge(a: "tuple[int,int,int,int] | None",
                b: "tuple[int,int,int,int] | None") -> bool:
    """a >= b？任一为 None 视为「未知」→ 保守返回 False（不轻易判定新版）。"""
    if a is None or b is None:
        return False
    return a >= b


def _is_windows() -> bool:
    return sys.platform == "win32"


def _registry_rtss_dir() -> "str | None":
    if not _is_windows():
        return None
    try:
        import winreg
    except Exception:
        return None
    for sub in (r"SOFTWARE\RTSS", r"SOFTWARE\WOW6432Node\RTSS"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as k:
                cnt = winreg.QueryInfoKey(k)[1]
                for i in range(cnt):
                    try:
                        val = winreg.EnumValue(k, i)[1]
                    except OSError:
                        continue
                    if isinstance(val, str) and (
                        "RivaTuner" in val or "RTSS" in val
                    ) and os.path.isdir(val):
                        return val
        except OSError:
            pass
    return None


def _common_rtss_dirs() -> "list[str]":
    return [
        r"C:\Program Files (x86)\RivaTuner Statistics Server",
        r"C:\Program Files\RivaTuner Statistics Server",
    ]


def system_rtss_dir() -> "str | None":
    """系统已安装的 RTSS 目录（注册表优先，其次常见目录）。"""
    reg = _registry_rtss_dir()
    if reg and os.path.isdir(reg):
        return reg
    for d in _common_rtss_dirs():
        if os.path.isdir(d):
            return d
    return None


def bundle_rtss_dir() -> "str | None":
    """随包内置的 RTSS 目录（自用默认随包；仅 ``NBJ_BUNDLE_RTSS=0`` 才不内嵌）。

    冻结态：``sys._MEIPASS/ningbingjue/vendor/RTSS``。
    源码/调试态：``src/ningbingjue/vendor/RTSS``（与 ryzenadj / VirtualDesktop 同级）。
    返回 ``None`` 表示本构建未内嵌 RTSS。
    """
    candidates = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "ningbingjue", "vendor", "RTSS"))
    candidates.append(os.path.join(os.path.dirname(__file__), "..", "vendor", "RTSS"))
    for c in candidates:
        c = os.path.abspath(c)
        if os.path.isdir(c) and os.path.exists(os.path.join(c, "RTSS.exe")):
            return c
    return None


def _register_vulkan_hkcu(target_dir: str) -> None:
    """Vulkan 隐式层退到 HKCU（用户目录部署无 HKLM 写权限时的兜底）。

    官方 NSIS 把 RTSSVk64.json 写进 HKLM\\SOFTWARE\\Khronos\\Vulkan\\ImplicitLayers。
    我们部署到用户目录，同样的键写 HKLM 会失败，故改写 HKCU 同名位置。失败静默。
    """
    if not _is_windows():
        return
    try:
        import winreg
        for json_name in ("RTSSVk64.json", "RTSSVk32.json"):
            jp = os.path.join(target_dir, json_name)
            if os.path.exists(jp):
                with winreg.CreateKey(
                    winreg.HKEY_CURRENT_USER,
                    r"SOFTWARE\Khronos\Vulkan\ImplicitLayers",
                ) as k:
                    winreg.SetValueEx(k, os.path.abspath(jp), 0, winreg.REG_DWORD, 0)
                _log(f"Vulkan 隐式层已注册到 HKCU: {json_name}")
    except Exception as e:
        _log(f"Vulkan HKCU 注册失败（非致命）: {e!r}")


def _self_register(target_dir: str) -> bool:
    """调官方 ``RTSS.exe /I`` 自注册（写注册表/文件关联/服务）。

    返回是否成功（best-effort：失败仅记日志，不阻塞部署）。
    """
    exe = os.path.join(target_dir, "RTSS.exe")
    if not os.path.exists(exe):
        _log("自注册失败：RTSS.exe 不存在")
        return False
    try:
        proc = subprocess.run(
            [exe, "/I"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30, creationflags=CREATE_NO_WINDOW,
        )
        ok = proc.returncode == 0
        _log(f"RTSS.exe /I 自注册 rc={proc.returncode}（{'OK' if ok else '失败'}）")
        return ok
    except Exception as e:
        _log(f"RTSS.exe /I 自注册异常: {e!r}")
        return False


def _rtss_running_now() -> bool:
    """当前是否有 RTSS.exe 在运行（用于部署时避免覆盖运行中进程）。"""
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() == "rtss.exe":
                return True
    except Exception:
        pass
    return False


def _release_bundle(bundle_dir: str, target_dir: str) -> bool:
    """把随包 RTSS 释放到用户目录。已存在且版本不旧则跳过复制。

    去破坏化（grill 修复）：
      * **绝不 rmtree 整个目标目录**——会连用户已调好的 Profiles（锁帧/游戏
        profile）一起删掉；改用 copytree 增量覆盖 + ignore 排除 ``Profiles``，
        用户配置在任何情况下不被触碰。
      * RTSS 正在运行时**跳过覆盖释放**：运行中进程的 exe 被锁（WinError 32），
        覆盖既无意义又会触发上面的失败路径；等下次 RTSS 没运行时再覆盖。
    """
    if os.path.isdir(target_dir) and os.path.exists(os.path.join(target_dir, "RTSS.exe")):
        # 已部署过：比较版本，内置更新时才覆盖。
        tv = get_file_version(os.path.join(target_dir, "RTSS.exe"))
        bv = get_file_version(os.path.join(bundle_dir, "RTSS.exe"))
        if _version_ge(tv, bv):
            _log("目标目录已存在且版本不旧，跳过释放")
            return True
        # 版本更旧需覆盖，但 RTSS 正在运行 → 跳过（避免 WinError 32 + 误删配置）。
        if _rtss_running_now():
            _log("目标目录版本较旧，但 RTSS 正在运行，跳过覆盖释放（下次重启再试）")
            return True
        _log("目标目录版本较旧，覆盖释放")
    try:
        os.makedirs(os.path.dirname(target_dir), exist_ok=True)
        # 增量覆盖：dirs_exist_ok 覆盖陈旧文件；ignore 排除 Profiles 目录，确保
        # 用户已调的锁帧/游戏 profile 永不被 bundle 默认值（Limit=0）冲掉。
        shutil.copytree(
            bundle_dir, target_dir,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("Profiles"),
        )
        _log(f"已释放 RTSS → {target_dir}（Profiles 已保留）")
        return True
    except Exception as e:
        _log(f"释放 RTSS 失败: {e!r}")
        return False


# 进程级一次性闩：本进程内部署逻辑只真正执行一次，重复调用返回首次结果，
# 杜绝任何周期触发（守护/进程重启循环）反复释放砸盘 Profiles。
_DEPLOY_LATCH: "tuple[str, str] | None" = None


def ensure_rtss(force: bool = False) -> "tuple[str, str]":
    """首次运行自部署主入口（带进程级一次性闩）。

    force=True 跳过闩强制重新部署（手动「修复 RTSS」按钮用）；普通启动调用
    不传 force，本进程内部署逻辑只执行一次，杜绝周期触发反复释放砸盘。
    """
    global _DEPLOY_LATCH
    if not force and _DEPLOY_LATCH is not None:
        _log("ensure_rtss 本进程已执行过，跳过重复部署")
        return _DEPLOY_LATCH
    result = _ensure_rtss_impl()
    _DEPLOY_LATCH = result
    return result


def _ensure_rtss_impl() -> "tuple[str, str]":
    """首次运行自部署主实现。返回 ``(state, text)``。

    state ∈ {running, installed, deployed, missing}：
      * running    —— 系统 RTSS 已装且在跑（理论上 deploy 不负责拉起，这里仅表「就绪」）
      * installed  —— 直接用系统已装 RTSS（版本 ≥ 内置），未释放文件
      * deployed   —— 释放了内置 RTSS 到用户目录并自注册成功
      * missing    —— 既无系统 RTSS 又无内置（需引导官方下载）
    """
    # 1) 系统已装？版本 ≥ 内置则直接用系统的，零释放。
    sys_dir = system_rtss_dir()
    bundle_dir = bundle_rtss_dir()
    if sys_dir:
        sys_ver = get_file_version(os.path.join(sys_dir, "RTSS.exe"))
        bun_ver = (get_file_version(os.path.join(bundle_dir, "RTSS.exe"))
                   if bundle_dir else None)
        if _version_ge(sys_ver, bun_ver):
            _log(f"检测到系统 RTSS v{sys_ver}（≥ 内置 v{bun_ver}），直接使用")
            return ("installed", f"使用系统 RTSS v{_fmt(sys_ver)}")

    # 2) 有内置 → 释放到用户目录并自注册。
    if bundle_dir:
        target = deploy_target_dir()
        if _release_bundle(bundle_dir, target):
            _self_register(target)
            _register_vulkan_hkcu(target)
            bun_ver = get_file_version(os.path.join(target, "RTSS.exe"))
            _log(f"RTSS 已部署 v{_fmt(bun_ver)} → {target}")
            return ("deployed", f"已部署 RTSS v{_fmt(bun_ver)}")
        return ("missing", "RTSS 释放失败（权限？）")

    # 3) 两路皆无 → 引导。
    _log("无系统 RTSS 且无内置，需引导官方下载")
    return ("missing", "RTSS 未安装，请前往 guru3d 下载")


def _fmt(v: "tuple[int,int,int,int] | None") -> str:
    return ".".join(str(x) for x in v) if v else "?"
