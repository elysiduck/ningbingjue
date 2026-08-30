"""RTSS (RivaTuner Statistics Server) adapter.

历史后端 ``rtss-cli.exe`` 在不少机器上与本地 RTSS 版本/API 不匹配，导致「锁不住」。
现改用随包自带的参数化脚本 ``blocks/RTSS/rtss_set_limit.ps1``：

  * 直接编辑 RTSS 全局 profile 文件（Profiles\\Global）里的 ``Limit=<n>``；
  * 通过 ``RTSSHooks64.dll`` 的 ``LoadProfile/SaveProfile/UpdateProfiles`` 让 RTSS 热重载；
  * 非管理员且写入被拒时，脚本内部自提权一次（弹一次 UAC）后重跑。

接口保持 ``set_limiter`` / ``set_limit`` 不变，使 ``RtssBlock``、自动锁帧、
休眠恢复与既有测试（MockRtssCli）无需改动。真正的写盘动作只在 ``set_limit``
里发生一次（合并 limiter 开关与 fps 为 ``-Limit`` 单一参数，0 = 解锁），避免
``set_limiter`` 与 ``set_limit`` 各拉起一次进程。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

from .base import ExternalAdapter


class RtssCli(ExternalAdapter, metaclass=__import__("abc").ABCMeta):
    @classmethod
    def __subclasshook__(cls, subclass):  # pragma: no cover - structural typing
        return hasattr(subclass, "set_limiter") and hasattr(subclass, "set_limit")

    # -- RTSS 安装/运行状态探测（供 UI 状态显示，避免「假成功」）------------
    def is_rtss_installed(self) -> bool | None:
        """True/False 当在 Windows 上能确定；非 Windows 返回 None。"""
        return None

    def is_rtss_running(self) -> bool | None:
        return None

    def rtss_status(self) -> "tuple[str, str]":
        """返回 ``(state, text)``，state ∈ {running, installed, missing, unknown}。"""
        return ("unknown", "RTSS 状态未知")


def _is_windows() -> bool:
    return sys.platform == "win32"


def _powershell_exists(ps: str) -> bool:
    if not _is_windows():
        return False
    try:
        proc = subprocess.run([ps, "-NoProfile", "-Command", "exit 0"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        return proc.returncode == 0
    except Exception:
        return False


def _running_as_admin() -> bool | None:
    """True/False 当在 Windows 上能确定；非 Windows 返回 None。"""
    if not _is_windows():
        return None
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def _rtss_install_dirs() -> "list[str]":
    return [
        r"C:\Program Files (x86)\RivaTuner Statistics Server",
        r"C:\Program Files\RivaTuner Statistics Server",
    ]


def _rtss_installed_via_registry() -> bool:
    if not _is_windows():
        return False
    try:
        import winreg
    except Exception:
        return False
    for sub in (r"SOFTWARE\RTSS", r"SOFTWARE\WOW6432Node\RTSS"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub):
                return True
        except OSError:
            pass
    return False


class Win32RtssCli(RtssCli):
    def __init__(self, ps1_path: str, powershell: str = "powershell.exe") -> None:
        self.ps1_path = ps1_path
        self.powershell = powershell
        # 合并 limiter 开关与 fps：set_limiter 仅记状态，set_limit 才真正写盘。
        self._enabled = False
        self._limit = 0
        # ---- 状态探测缓存（避免每秒拉满注册表/进程列表）----
        # 安装状态一整局几乎不变，缓存 30s，最多每 30s 查一次注册表+目录。
        self._installed_cache: "tuple[float, bool] | None" = None
        self._INSTALLED_TTL = 30.0
        # 运行探测：记住 RTSS.exe 的 PID，每秒只做 O(1) 的 pid_exists 探活；
        # 仅 PID 失效时才全量扫一次，结果再缓存 3s。tasklist 回退同理受 3s 限流。
        self._rtss_pid: "int | None" = None
        self._running_cache: "tuple[float, bool] | None" = None
        self._RUNNING_TTL = 3.0

    @property
    def available(self) -> bool:
        # 控制面（ps1）只有在 RTSS 真正安装时才有效；未安装时写盘必失败，
        # 此时返回 False 让上层（apply_lock）直接报「未安装」而非假成功。
        if not (bool(self.ps1_path) and os.path.exists(self.ps1_path)
                and _powershell_exists(self.powershell)):
            return False
        installed = self.is_rtss_installed()
        if installed is False:
            return False
        return True

    def is_rtss_installed(self) -> bool:
        if not _is_windows():
            return False
        now = time.monotonic()
        if (self._installed_cache is not None
                and (now - self._installed_cache[0]) < self._INSTALLED_TTL):
            return self._installed_cache[1]
        val = self._compute_installed()
        self._installed_cache = (now, val)
        return val

    def _compute_installed(self) -> bool:
        for d in _rtss_install_dirs():
            if os.path.isdir(d):
                return True
        return _rtss_installed_via_registry()

    def is_rtss_running(self) -> bool:
        now = time.monotonic()
        # 命中缓存（3s 内）直接返回，不碰进程列表 / 不拉起 tasklist。
        if self._running_cache is not None and (now - self._running_cache[0]) < self._RUNNING_TTL:
            return self._running_cache[1]
        val = self._compute_running()
        self._running_cache = (now, val)
        return val

    def _compute_running(self) -> bool:
        # O(1) 快路径：已知 PID 则只查该进程是否还活着（psutil.pid_exists 微秒级）。
        if self._rtss_pid is not None:
            try:
                import psutil
                if psutil.pid_exists(self._rtss_pid):
                    proc = psutil.Process(self._rtss_pid)
                    if (proc.name() or "").lower() == "rtss.exe":
                        return True
            except Exception:
                pass
            # PID 失效（进程退出/重启被复用）→ 清空，进入全量扫描。
            self._rtss_pid = None
        # 全量扫描：仅缓存过期或 PID 丢失时发生，扫到即记下 PID 供后续 O(1) 探活。
        try:
            import psutil
            for p in psutil.process_iter(["name", "pid"]):
                nm = (p.info.get("name") or "")
                if nm.lower() == "rtss.exe":
                    self._rtss_pid = p.info.get("pid")
                    return True
            # psutil 可用但未找到 RTSS.exe → 不回退 tasklist，直接判未运行。
            return False
        except Exception:
            pass
        # psutil 不可用：回退 tasklist。注意调用方已用 3s 缓存限流，
        # 因此即便走到这里也不会每秒拉起一个子进程。
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq RTSS.exe"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout
            return "RTSS.exe" in out
        except Exception:
            return False

    def rtss_status(self) -> "tuple[str, str]":
        if not self.is_rtss_installed():
            return ("missing", "RTSS 未安装")
        if self.is_rtss_running():
            return ("running", "RTSS 运行中")
        return ("installed", "RTSS 已安装但未运行")

    @property
    def is_admin(self) -> bool | None:
        return _running_as_admin()

    # -- 真正写盘：用 -Limit 单一参数调用 PS1（0 = 解锁）-------------------
    def _run_ps1(self, limit: int) -> bool:
        if not self.available:
            return False
        try:
            args = [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", self.ps1_path, "-Limit", str(int(limit))]
            proc = subprocess.run(args, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, check=False,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
            return proc.returncode == 0
        except Exception:
            return False

    def set_limiter(self, enabled: bool) -> bool:
        self._enabled = bool(enabled)
        # 实际写盘推迟到 set_limit，避免重复拉起进程。
        return self.available

    def set_limit(self, fps: int) -> bool:
        self._limit = int(fps)
        effective = self._limit if self._enabled else 0
        return self._run_ps1(effective)


class MockRtssCli(RtssCli):
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._available = True

    @property
    def available(self) -> bool:
        return self._available

    def set_limiter(self, enabled: bool) -> bool:
        self.calls.append(("limiter", enabled))
        return True

    def set_limit(self, fps: int) -> bool:
        self.calls.append(("limit", fps))
        return True
