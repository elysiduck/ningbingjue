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
from .rtss_native import (
    native_apply_limit,
    _running_rtss_dir,
    _read_global_limit,
)


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


def _rtss_set_log(msg: str) -> None:
    """下发链路日志（成功/失败/降级都记），掌机跑一次即可定位任何残余问题。"""
    try:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        d = os.path.join(root, "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "nbj_rtss.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


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
    dirs = [
        # 用户可写部署目录（第 2 步「首次运行自部署」的目标；零 UAC）。
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "NingBingJue", "RTSS",
        ),
        r"C:\Program Files (x86)\RivaTuner Statistics Server",
        r"C:\Program Files\RivaTuner Statistics Server",
    ]
    # 去空、去重、保序
    seen = set()
    out = []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _rtss_dir_via_registry() -> "str | None":
    """从 HKLM\\SOFTWARE\\RTSS* 读安装路径（ps1 Get-RtssDir 同逻辑）。"""
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


def _rtss_installed_via_registry() -> bool:
    return _rtss_dir_via_registry() is not None


class Win32RtssCli(RtssCli):
    def __init__(self, ps1_path: str, powershell: str = "powershell.exe",
                 rtss_dir: "str | None" = None) -> None:
        self.ps1_path = ps1_path
        self.powershell = powershell
        # 显式 RTSS 目录（第 2 步用户目录部署 / 配置 RtssPath）。None → 自动探测。
        self.rtss_dir = rtss_dir
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

    def _resolve_rtss_dir(self) -> "str | None":
        """解析 RTSS 安装/部署目录。

        优先级：显式配置 > **运行中 RTSS.exe 的真实目录** > 已知安装目录探测 >
        注册表路径。返回 None 表示未安装。

        关键：运行中目录插在静态目录猜测之前（显式配置之后），保证
        DLL / Profiles\\Global / 运行中进程三者配对到同一份安装——否则改错目录
        会让下发静默失效（假成功）。
        """
        if self.rtss_dir and os.path.isdir(self.rtss_dir):
            return self.rtss_dir
        # 配对真相源：运行中 RTSS.exe 的真实目录优先于静态目录猜测。
        run_dir = _running_rtss_dir()
        if run_dir and os.path.isdir(run_dir):
            return run_dir
        for d in _rtss_install_dirs():
            if os.path.isdir(d):
                return d
        reg = _rtss_dir_via_registry()
        if reg and os.path.isdir(reg):
            return reg
        return None

    @property
    def available(self) -> bool:
        # 控制面（ps1 或 ctypes 原生）只有在 RTSS 真正安装时才有效；未安装时
        # 写盘必失败，此时返回 False 让上层（apply_lock）直接报「未安装」而非假成功。
        if not _is_windows():
            return False
        if self._resolve_rtss_dir() is None:
            return False
        return True

    def is_rtss_installed(self) -> bool:
        if not _is_windows():
            return False
        now = time.monotonic()
        if (self._installed_cache is not None
                and (now - self._installed_cache[0]) < self._INSTALLED_TTL):
            return self._installed_cache[1]
        val = self._resolve_rtss_dir() is not None
        self._installed_cache = (now, val)
        return val

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
    def _run_ps1(self, limit: int, rtss_dir: "str | None" = None) -> bool:
        if not self.available:
            return False
        args = [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", self.ps1_path, "-Limit", str(int(limit))]
        # 锁定与 native 相同的目录，避免 PS 内部 Get-RtssDir 解析到另一份 RTSS，
        # 导致两次下发写不同 profile（读回验证也会对不上）。
        if rtss_dir:
            args += ["-RTSSPath", rtss_dir]
        try:
            # 用 Popen + wait(timeout) 而非 subprocess.run：run() 超时后会把子进程
            # 留在后台成为僵尸（PowerShell 提权框不消失时尤其明显），且会永久阻塞
            # 当前线程——而 _run_ps1 由 auto_lock 后台采样回调驱动，挂起即冻结整条
            # 采样链（GPU 浮动/风扇/锁帧同时失灵）。超时即判失败并显式 kill 回收。
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            return False
        try:
            return proc.wait(timeout=10) == 0
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
            return False
        except Exception:
            return False

    def set_limiter(self, enabled: bool) -> bool:
        self._enabled = bool(enabled)
        # 实际写盘推迟到 set_limit，避免重复拉起进程。
        return self.available

    def set_limit(self, fps: int) -> bool:
        self._limit = int(fps)
        effective = self._limit if self._enabled else 0
        # 解析出的目录已优先配对运行中 RTSS.exe（见 _resolve_rtss_dir），
        # 保证 native 与 PS 两条链都命中同一份安装。
        rtss_dir = self._resolve_rtss_dir()
        if rtss_dir is None:
            _rtss_set_log(f"set_limit 无 RTSS 目录 target={effective}")
            return False
        running = self.is_rtss_running()
        _rtss_set_log(
            f"set_limit target={effective} rtss_dir={rtss_dir} running={running}"
        )
        # 1) 先试 ctypes 原生路径（纯净、零 UAC、毫秒级）。native 内部已含读回
        #    验证：写 Limit → 热重载 → 重读确认，被 RTSS 内存回写冲掉即判失败。
        if native_apply_limit(rtss_dir, effective, running):
            _rtss_set_log(f"set_limit NATIVE OK limit={effective} dir={rtss_dir}")
            return True
        # 2) 降级 PowerShell 后端（处理 Program Files 提权、复杂情况）。
        #    同样锁定 rtss_dir，且退出后读回验证，杜绝假成功。
        if self._run_ps1(effective, rtss_dir):
            applied = _read_global_limit(os.path.join(rtss_dir, "Profiles", "Global"))
            if applied == effective:
                _rtss_set_log(f"set_limit PS OK limit={effective} dir={rtss_dir}")
                return True
            _rtss_set_log(
                f"set_limit PS 退出码成功但读回不符(期望{effective} 实得{applied}) "
                f"dir={rtss_dir}"
            )
            return False
        _rtss_set_log(f"set_limit ALL FAILED limit={effective} dir={rtss_dir}")
        return False


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
