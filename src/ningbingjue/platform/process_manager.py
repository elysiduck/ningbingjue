"""Process management — enumeration, start/stop, and kernel-level freeze.

Mirrors the C# ``ProcessManager`` + PowerShell ``Get-CimInstance Win32_Process``
/ ``Get-Process`` used by the procmon / freeze blocks.  The real Win32
implementation uses ``ctypes`` + ``ntdll`` for suspend/resume and ``wmic`` for
enumeration (Windows-only); ``MockProcessManager`` is used in tests and on
non-Windows hosts and is fully controllable.
"""

from __future__ import annotations

import abc
import ctypes
import ctypes.wintypes
import io
import csv
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from .base import ExternalAdapter


# --------------------------------------------------------------------------
# AutoHotkey support
# --------------------------------------------------------------------------
# An ``.ahk`` script never runs as its own process — it is hosted by the
# AutoHotkey interpreter (``AutoHotkeyU64.exe`` / ``AutoHotkey.exe`` …).  The
# script path is passed as a *command-line argument*, so detection must match
# the path/name inside the host's command line, not the process image name.
AHK_EXT = ".ahk"
AHK_HOST_NAMES = (
    "autohotkey.exe",
    "autohotkey64.exe",
    "autohotkey32.exe",
    "autohotkeyu64.exe",
    "autohotkeyu32.exe",
    "autohotkeya32.exe",
    "autohotkeyux64.exe",
    "autohotkeyux32.exe",
    "autohotkeyunicode.exe",
)

# Well-known install roots, expanded via environment variables.
_KNOWN_AHK_ROOTS = (
    r"%ProgramFiles%\AutoHotkey",
    r"%ProgramFiles(x86)%\AutoHotkey",
    r"%LocalAppData%\Programs\AutoHotkey",
    r"%LocalAppData%\AutoHotkey",
)


def _decode_cli(raw: bytes) -> str:
    """Decode process-enumeration output robustly.

    ``wmic`` writes UTF-16LE (with a BOM) when redirected to a pipe; PowerShell
    ``Get-CimInstance`` may emit UTF-8 (with or without BOM) or the console
    codepage.  Decoding any of these as UTF-8 (as the enumerators used to)
    injects NUL bytes between every character and corrupts the CSV header, so
    ``CommandLine`` could never be read.  Detect the encoding by BOM first
    (``wmic`` always emits one), then fall back to UTF-8 / the system codepage,
    and strip any stray NULs so CSV parsing and string matching work.
    """
    if not raw:
        return ""
    if isinstance(raw, str):
        # Already-decoded input (e.g. tests / non-Windows shims): just strip NULs.
        return raw.replace("\x00", "")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        # UTF-16 (LE or BE) with BOM — what wmic writes to a pipe.
        return raw.decode("utf-16", errors="ignore").replace("\x00", "")
    if raw[:3] == b"\xef\xbb\xbf":
        # UTF-8 with BOM — some PowerShell CIM output.
        return raw.decode("utf-8-sig", errors="ignore").replace("\x00", "")
    for enc in ("utf-8", "mbcs", "latin-1"):
        try:
            return raw.decode(enc).replace("\x00", "")
        except Exception:
            continue
    return raw.decode("latin-1", errors="ignore").replace("\x00", "")


def norm_path_sep(text: str) -> str:
    """Normalize path separators to backslash.

    Windows process command lines use backslashes, but a config path coming
    from a file dialog (or hand-edited JSON) can use forward slashes.  Collapse
    runs so ``a//b`` and ``a\\b`` both become ``a\b``.
    """
    return (text or "").replace("/", "\\").replace("\\\\", "\\")


def is_ahk_script(path: str) -> bool:
    return os.path.splitext(path or "")[1].lower() == AHK_EXT


def find_autohotkey() -> str | None:
    """Locate the AutoHotkey interpreter executable.

    Search order: ``PATH`` (``where``), then well-known install directories.
    Returns the absolute path, or ``None`` when AutoHotkey is not installed.
    """
    exe = shutil.which("autohotkey.exe") or shutil.which("autohotkey")
    if exe:
        return os.path.abspath(exe)
    for raw in _KNOWN_AHK_ROOTS:
        root = os.path.expandvars(raw)
        if not root or root == raw or not os.path.isdir(root):
            continue
        for name in AHK_HOST_NAMES:
            cand = os.path.join(root, name)
            if os.path.isfile(cand):
                return cand
    return None


def match_running(target_path: str, processes: list[Any]) -> bool:
    """True if a monitored target (script / exe) is currently running.

    AHK-aware: an ``.ahk`` script is hosted by an ``AutoHotkey*.exe`` process,
    so its image name is never the script — we match the script path/name in
    the host's command line.  Path separators are normalized so a config stored
    with ``/`` still matches a Windows command line that uses ``\\``.
    """
    path = norm_path_sep((target_path or "").lower())
    base = norm_path_sep(os.path.basename(path)) if path else ""
    ahk = is_ahk_script(target_path)
    for p in processes:
        cmd = norm_path_sep((getattr(p, "cmdline", "") or "").lower())
        name = (getattr(p, "name", "") or "").lower()
        if path and path in cmd:
            return True
        if base and base == name:
            return True
        if ahk and name in AHK_HOST_NAMES and base and base in cmd:
            return True
    return False


@dataclass
class ProcessInfo:
    """One row of ``Get-CimInstance Win32_Process`` (subset we use)."""

    pid: int = 0
    name: str = ""
    cmdline: str = ""
    memory_mb: float = 0.0
    session_id: int = 0
    # Full image path (resolved on demand via QueryFullProcessImageNameW).
    # Left empty by enumerate() to avoid an OpenProcess+query per process;
    # use ProcessManager.find_exe_path_by_name() to resolve it for a target.
    exe: str = ""
    # Whether this process owns at least one visible top-level window (used by
    # the freeze block to rank foreground processes above background ones).
    has_visible_window: bool = False


def _collect_visible_window_pids() -> set[int]:
    """Return the set of PIDs that own a visible, non-owned, non-zero-size
    top-level window.  Used to rank foreground processes above background ones
    in the freeze candidate list.  Returns an empty set off-Windows or on any
    failure (the caller degrades to memory-only ranking)."""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        GW_OWNER = 4

        visible: set[int] = set()

        @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL,
                            ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def _enum_cb(hwnd: int, lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            # Owned windows (dialogs/tooltips) are not real top-level apps.
            if user32.GetWindow(hwnd, GW_OWNER):
                return True
            rect = ctypes.wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                if (rect.right - rect.left) <= 0 or (rect.bottom - rect.top) <= 0:
                    return True
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                visible.add(pid.value)
            return True

        user32.EnumWindows(_enum_cb, 0)
        return visible
    except Exception:
        return set()


# --------------------------------------------------------------------------
# 进程扫描原语 —— 对齐 platform/hwinfo_status.py 的 HWiNFO 式写法
# --------------------------------------------------------------------------
# 为什么不再把 wmic / WMI 当首选（实测对照，见 _ls_probe_cmp.py）：
#
#   现行 wmic→CIM→enumerate 链   388 ms
#   psutil 直扫                   37 ms
#   CreateToolhelp32Snapshot      15 ms（且覆盖率最高）
#
# 三点决定性差异：
#   1. **会话过滤**：``enumerate()`` 只返回当前会话。实测本机 405 个进程里有
#      234 个（58%）被会话过滤砍掉 —— 跨会话的目标（Steam / 启动器在别的会话
#      拉起的游戏与工具）用 enumerate() **永远扫不到**。这正是「LS 明明开着
#      却检测不到」的结构性原因。
#   2. **子进程依赖**：wmic 是「按需功能」可选组件，精简版系统与掌机 OEM 镜像
#      上常被移除；PowerShell Get-CimInstance 可能被执行策略拦截，且每次都要
#      拉起一个 powershell.exe。
#   3. **速度**：纯进程内 API 比拉子进程快一个数量级，点「自动设置」不再卡顿。
#
# 因此顺序改为：本机直扫（psutil→Toolhelp）→ WMI → enumerate 兜底。
class PROCESSENTRY32W(ctypes.Structure):
    """``CreateToolhelp32Snapshot`` 的进程条目（宽字符版）。"""

    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("cntUsage", ctypes.wintypes.DWORD),
        ("th32ProcessID", ctypes.wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_ulonglong),
        ("th32ModuleID", ctypes.wintypes.DWORD),
        ("cntThreads", ctypes.wintypes.DWORD),
        ("th32ParentProcessID", ctypes.wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szExeFile", ctypes.wintypes.WCHAR * 260),
    ]


def _iter_processes_toolhelp() -> list[tuple[int, str]]:
    """``CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS)`` 全量枚举。

    psutil 不可用时的兜底：同为纯进程内 API，**无会话过滤**、不起子进程。
    失败返回空列表。"""
    out: list[tuple[int, str]] = []
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        TH32CS_SNAPPROCESS = 0x00000002
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap in (-1, 0, None):
            return []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                out.append((int(entry.th32ProcessID), str(entry.szExeFile or "")))
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return []
    return out


def iter_processes_native() -> list[tuple[int, str]]:
    """枚举全系统 ``(pid, 映像名)`` —— 无会话过滤、不拉起子进程。

    psutil 优先（已随 exe 打包，见 build_exe.py 的 ``--hidden-import psutil``），
    不可用则回退 ``CreateToolhelp32Snapshot``。任一路失败都返回空列表，
    由调用方继续兜底（绝不抛异常）。"""
    try:
        import psutil
        out: list[tuple[int, str]] = []
        for p in psutil.process_iter(["name", "pid"]):
            try:
                out.append((int(p.info["pid"]), str(p.info.get("name") or "")))
            except Exception:
                continue
        if out:
            return out
    except Exception:
        pass
    return _iter_processes_toolhelp()


def exe_path_of_pid(pid: int) -> str:
    """返回 ``pid`` 的完整映像路径；取不到返回 ""。

    psutil 的 ``Process.exe()`` 优先，失败回退 ``QueryFullProcessImageNameW``
    （``PROCESS_QUERY_LIMITED_INFORMATION``，Vista+ 均支持）。"""
    try:
        import psutil
        path = psutil.Process(int(pid)).exe() or ""
        if path:
            return path
    except Exception:
        pass
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(2048)
            size = ctypes.wintypes.DWORD(2048)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value or ""
        finally:
            k32.CloseHandle(h)
    except Exception:
        pass
    return ""


def iter_pids_by_name(name: str, exact: bool = True) -> list[int]:
    """按映像名找 PID（大小写不敏感，无会话过滤）。

    ``exact=True``  —— 映像名完全等于 ``name``；
    ``exact=False`` —— ``name`` 是映像名的**子串**，容忍 exe 改名或带版本后缀
                        （``losslessscaling`` 能匹配 ``LosslessScaling.exe`` /
                        ``LosslessScaling3.exe``）。老版只做精确相等，版本一改
                        就整体漏判。
    """
    target = (name or "").lower()
    if not target:
        return []
    out: list[int] = []
    for pid, pname in iter_processes_native():
        nm = (pname or "").lower()
        if (nm == target) if exact else (target in nm):
            out.append(pid)
    return out


# --------------------------------------------------------------------------
# resume_ex() 的结构化结局
# --------------------------------------------------------------------------
# ``resume()`` 只有一个 bool，把「进程早就不存在了」和「真的没解开」压成同一个
# False。调用方（冻结卡片的解冻 worker）拿它做 ``ok = ok and r`` 一票否决，于是
# 冻结组里只要有一个**早已失效、PID 已被复用**的成员就会让整组报失败 —— 哪怕
# 主进程其实已经解冻成功（实测：403 个进程里 158 个≈39% 无法 OpenProcess，
# PID 一旦被复用到受保护进程就每次必失败）。
#
# 结局分五类，让调用方能区分对待：
#   RES_RESUMED  真的调用了 NtResumeProcess 并成功（含"内核确认已恢复"）
#   RES_RUNNING  内核确认本来就没挂起，无需恢复
#   RES_GONE     进程已不存在（PID 失效）—— **不算失败**，调用方只提示
#   RES_DENIED   进程在，但打不开句柄（受保护 / 权限）—— 真失败
#   RES_FAILED   句柄拿到了，但 NtResumeProcess 返回非 0
RES_RESUMED = "resumed"
RES_RUNNING = "running"
RES_GONE = "gone"
RES_DENIED = "denied"
RES_FAILED = "failed"

#: 调用方视为「这一步没问题」的结局（gone 不计入失败）。
RES_OK = (RES_RESUMED, RES_RUNNING, RES_GONE)


class ProcessManager(ExternalAdapter, abc.ABC):
    @abc.abstractmethod
    def suspend(self, pid: int) -> bool: ...

    @abc.abstractmethod
    def resume(self, pid: int) -> bool: ...

    @abc.abstractmethod
    def is_suspended(self, pid: int) -> bool: ...

    @abc.abstractmethod
    def enumerate(self) -> list[ProcessInfo]: ...

    @abc.abstractmethod
    def start(self, path: str, **kw: Any) -> bool: ...

    @abc.abstractmethod
    def stop(self, pid_or_name: int | str) -> bool: ...

    @abc.abstractmethod
    def stop_matching(self, cmd_substr: str) -> bool: ...

    @abc.abstractmethod
    def is_running(self, name: str) -> bool: ...

    # -- 跨会话判定（非抽象：默认退化，实现可覆写） ------------------------
    def find_pid_by_name(self, name: str, exact: bool = False) -> int | None:
        """无会话过滤地按映像名找首个 PID；找不到返回 ``None``。

        ``enumerate()`` 的会话过滤会让跨会话目标（Steam/启动器在别的会话拉起的
        程序）永远不可见，故另开一条跨会话通道。默认实现返回 ``None``。"""
        return None

    def is_running_anywhere(self, name: str, exact: bool = False) -> bool:
        """跨会话判定进程是否在跑（默认实现：退回 ``is_running`` 的语义）。"""
        return False

    def is_suspended_native(self, pid: int) -> bool | None:
        """问内核这个进程**现在**是否真的被挂起（与内存账本无关）。

        True / False；查询不了返回 ``None``（未知）——调用方必须把「未知」
        当保守情况处理，绝不当「未挂起」。基类默认全部返回 ``None``，
        由 Win32 实现（NtQuerySystemInformation）与 Mock（内存集合）覆写。
        """
        return None

    # -- 结构化恢复 / PID 可达性（非抽象：默认退化） --------------------
    def note_suspended(self, pid: int) -> None:
        """把 ``pid`` 登记进内存账本 = 「本工具挂起过它」。

        用于跨会话恢复：NBJ 重启后 ``_suspended`` 是空的，而持久化的冻结组
        里记录的全是我们自己挂起的进程。不补登记的话，``resume_ex`` 会退回
        纯内核判据（对复杂进程极易误判为「没挂」）而跳过恢复 —— 即「假解冻」。
        """
        try:
            self._suspended.add(int(pid))  # type: ignore[attr-defined]
        except Exception:
            pass

    def pid_reachability(self, pid: int) -> str:
        """``'alive'`` / ``'gone'`` / ``'denied'``。基类默认 ``'alive'``。"""
        return "alive"

    def pid_table(self) -> dict[int, str]:
        """当前系统的 ``{pid: 映像名}``。基类无从得知，返回空表；由 Win32
        （真实直扫）与 Mock（自己注入的 processes）覆写。"""
        return {}

    def pid_exists(self, pid: int) -> bool:
        """默认实现：查 :meth:`pid_table`。子类可覆写以加缓存/兜底。"""
        try:
            return int(pid) in self.pid_table()
        except Exception:
            return False

    def resume_ex(self, pid: int, restore_window: bool = True) -> str:
        """结构化恢复；默认退化实现把 ``resume()`` 的 bool 翻译回结局。"""
        return RES_RESUMED if self.resume(pid) else RES_FAILED


class Win32ProcessManager(ProcessManager):
    """ctypes-backed ``NtSuspendProcess`` / ``NtResumeProcess`` + wmic enum.

    Faithful to the original ``blocks/freeze.ps1``:
    * suspend/resume open the process with ``PROCESS_SUSPEND_RESUME`` (0x0800)
      — the minimal right ``NtSuspendProcess``/``NtResumeProcess`` require.
    * on suspend the target's visible windows are minimized (``ShowWindow`` 6);
      on resume they are restored (``ShowWindow`` 9) — matches the PS1
      ``Suspend-Proc`` / ``Resume-Proc`` window handling.
    * ``enumerate`` parses the wmic CSV with the ``csv`` module (command lines
      contain commas, so naive ``str.split`` corrupts the columns) and returns
      only processes in the current session, excluding our own PID.
    """

    PROCESS_SUSPEND_RESUME = 0x0800
    SW_MINIMIZE = 6
    SW_RESTORE = 9

    def __init__(self) -> None:
        self._ntdll = None
        self._kernel32 = None
        self._user32 = None
        try:
            self._ntdll = ctypes.windll.ntdll  # type: ignore[attr-defined]
            self._kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            self._user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            self._ntdll.NtSuspendProcess.argtypes = [ctypes.wintypes.HANDLE]
            self._ntdll.NtSuspendProcess.restype = ctypes.wintypes.LONG
            self._ntdll.NtResumeProcess.argtypes = [ctypes.wintypes.HANDLE]
            self._ntdll.NtResumeProcess.restype = ctypes.wintypes.LONG
        except Exception:
            self._ntdll = None
            self._kernel32 = None
            self._user32 = None
        self._suspended: set[int] = set()

    @property
    def available(self) -> bool:
        return self._ntdll is not None

    @staticmethod
    def _current_session_id() -> int | None:
        try:
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            pid = k32.GetCurrentProcessId()
            sid = ctypes.wintypes.DWORD(0)
            if k32.ProcessIdToSessionId(pid, ctypes.byref(sid)):  # type: ignore[attr-defined]
                return sid.value
        except Exception:
            pass
        return None

    def _handle(self, pid: int) -> Any:
        return self._kernel32.OpenProcess(self.PROCESS_SUSPEND_RESUME, False, pid)  # type: ignore[attr-defined]

    def _set_window_state(self, pid: int, minimized: bool) -> None:
        if self._user32 is None:
            return
        try:
            user32 = self._user32
            found: list[int] = []

            @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
            def _enum(hwnd: int, _lparam: int) -> bool:  # type: ignore[valid-type]
                pid_buf = ctypes.wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
                if pid_buf.value == pid and user32.IsWindowVisible(hwnd):
                    found.append(hwnd)
                return True

            user32.EnumWindows(_enum, 0)  # type: ignore[arg-type]
            for hwnd in found:
                user32.ShowWindow(hwnd, self.SW_MINIMIZE if minimized else self.SW_RESTORE)
        except Exception:
            pass

    def _query_image_name(self, pid: int) -> str:
        """Return the full image path of ``pid`` via ``QueryFullProcessImageNameW``.

        Returns ``""`` on any failure (no handle / access denied / not Windows).
        Best-effort only — never raises."""
        if self._kernel32 is None:
            return ""
        try:
            QUERY_LIMITED = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
            h = self._kernel32.OpenProcess(QUERY_LIMITED, False, pid)  # type: ignore[attr-defined]
            if not h:
                return ""
            try:
                self._kernel32.QueryFullProcessImageNameW.argtypes = [  # type: ignore[attr-defined]
                    ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD,
                    ctypes.c_wchar_p, ctypes.POINTER(ctypes.wintypes.DWORD)]
                self._kernel32.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL  # type: ignore[attr-defined]
                buf = ctypes.create_unicode_buffer(2048)
                size = ctypes.wintypes.DWORD(2048)
                if self._kernel32.QueryFullProcessImageNameW(  # type: ignore[attr-defined]
                        h, 0, buf, ctypes.byref(size)):
                    return buf.value or ""
            finally:
                self._kernel32.CloseHandle(h)  # type: ignore[attr-defined]
        except Exception:
            pass
        return ""

    def _is_protected_system_process(self, pid: int) -> bool:
        """底层硬保护：拒绝挂起「映像位于 Windows 系统目录」的进程。

        与业务层 ``FreezeBlock.EXCLUDED`` 名单互补——覆盖名单遗漏的关键系统
        进程（csrss/wininit/lsass/services/smss/dwm/winlogon/svchost 等都在
        System32 下），防止 ``NtSuspendProcess`` 命中它们导致蓝屏/桌面崩溃。
        仅在可用底层句柄时生效；取路径失败一律视为「不保护」（不误伤）。
        """
        if self._kernel32 is None:
            return False
        try:
            path = self._query_image_name(pid).lower()
            if not path:
                return False
            sysroot = os.environ.get("SystemRoot", "C:\\Windows").lower().rstrip("\\")
            for sub in ("system32", "syswow64", "winsxs"):
                if path.startswith(os.path.join(sysroot, sub)):
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _parse_wmic_execpath(raw: bytes) -> str:
        """从 ``wmic ... get ExecutablePath /format:csv`` 输出里取出首个非空路径。

        wmic 重定向到管道时是 UTF-16LE：解码后首行常为空白（``\\r\\r\\n`` 前缀），
        且以 ``\\r`` 而非 ``\\n`` 换行。直接用 ``csv.DictReader`` 会把真正的表头
        ``Node,ExecutablePath`` 误当成数据行（fieldnames 变成空列表），导致路径
        永远取不到、自动检测失效。这里先把 ``\\r`` 统一成 ``\\n`` 并去掉首尾空行，
        再交给 DictReader。
        """
        text = _decode_cli(raw).replace("\r", "\n").strip()
        if not text:
            return ""
        try:
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                ep = (row.get("ExecutablePath") or "").strip()
                if ep:
                    return ep
        except Exception:
            pass
        return ""

    def _find_exe_path_via_wmi(self, name: str) -> str:
        """通过 WMI ``Win32_Process.ExecutablePath`` 直接取路径，无需 OpenProcess。

        比 ``enumerate()`` + ``_query_image_name`` 更鲁棒：

        * 不受 ``enumerate()`` 的 **会话(session)过滤** 影响——LS 即使与凝冰结魄
          不在同一会话也能找到（掌机常见：游戏/启动器单独会话）。
        * 不打开目标进程句柄，规避不同 **完整性级别(IL)/权限** 导致的
          ``OpenProcess`` 失败（典型：凝冰结魄「以管理员运行」读标准用户进程，
          或反之）。

        主路径用 wmic（最快），wmic 在部分 Win11 已被移除则回退 PowerShell
        ``Get-CimInstance``。任一失败都静默返回 ""，由调用方回退到原 enumerate 路径。
        """
        name = (name or "").lower()
        if not name:
            return ""
        # 1) wmic 直查（最快）。
        try:
            raw = subprocess.run(
                ["wmic", "process", "where", f"name='{name}'",
                 "get", "ExecutablePath", "/format:csv"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
                timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout or b""
            path = self._parse_wmic_execpath(raw)
            if path:
                return path
        except Exception:
            pass
        # 2) PowerShell Get-CimInstance 兜底（wmic 被移除的 Win11 构建）。
        try:
            ps = (
                f"Get-CimInstance Win32_Process -Filter \"Name='{name}'\" | "
                "Select-Object -First 1 ExecutablePath | "
                "ConvertTo-Csv -NoTypeInformation"
            )
            raw = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", ps],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
                timeout=25, creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout or b""
            text = _decode_cli(raw).replace("\r", "\n").strip()
            if text:
                reader = csv.DictReader(io.StringIO(text))
                for row in reader:
                    ep = (row.get("ExecutablePath") or "").strip().strip('"')
                    if ep and os.path.exists(ep):
                        return ep
        except Exception:
            pass
        return ""

    def find_exe_path_by_name(self, name: str, fuzzy: bool = True) -> str | None:
        """Return the full image path of the first running process whose image
        name (case-insensitive) equals ``name``.

        Used by the LS launcher's "auto-set" feature: scan for
        ``losslessscaling.exe`` and read its on-disk location so it can be
        re-launched later.  Returns ``None`` when no match or on any failure.

        探测顺序（实测依据见模块顶部注释与 ``_ls_probe_cmp.py`` 对照实验）：

        ① **本机直扫** ``iter_pids_by_name`` + ``exe_path_of_pid``
           （psutil → Toolhelp）。无会话过滤、不起子进程，15~40 ms。放在最前，
           因为 ``enumerate()`` 的会话过滤实测砍掉 58% 的进程（跨会话的 LS
           永远扫不到），而 wmic 在精简版系统上可能根本不存在。
        ② **WMI** ``ExecutablePath``（需 wmic/PowerShell，慢但无需进程句柄，
           对提升权限 / 低完整性级别的目标仍有效）。
        ③ **模糊直扫**：用去扩展名的主干做子串匹配，容忍 exe 改名/版本后缀。
        ④ 最后才退回 ``enumerate()`` + ``QueryFullProcessImageNameW``。
        """
        if self._kernel32 is None:
            return None
        name = (name or "").lower()
        if not name:
            return None

        # ① 本机直扫：精确名（无会话过滤，最快最全）
        try:
            for pid in iter_pids_by_name(name, exact=True):
                exe = exe_path_of_pid(pid) or self._query_image_name(pid)
                if exe:
                    return exe
        except Exception:
            pass

        # ② WMI ExecutablePath（无需句柄，跨 IL 鲁棒）
        exe = self._find_exe_path_via_wmi(name)
        if exe:
            return exe

        # ③ 模糊直扫：容忍 exe 改名 / 版本后缀（如 LosslessScaling3.exe）
        if fuzzy:
            stem = os.path.splitext(name)[0]
            if stem and stem != name:
                try:
                    for pid in iter_pids_by_name(stem, exact=False):
                        exe = exe_path_of_pid(pid) or self._query_image_name(pid)
                        if exe:
                            return exe
                except Exception:
                    pass

        # ④ 最后退回 enumerate()（有会话过滤，仅作兜底）
        try:
            for p in self.enumerate():
                if p.name.lower() == name:
                    exe = self._query_image_name(p.pid)
                    if exe:
                        return exe
        except Exception:
            return None
        return None

    def find_pid_by_name(self, name: str, exact: bool = False) -> int | None:
        """无会话过滤地按映像名找首个 PID（见基类说明）。"""
        try:
            for pid in iter_pids_by_name(name, exact=exact):
                return pid
        except Exception:
            pass
        return None

    def is_running_anywhere(self, name: str, exact: bool = False) -> bool:
        """跨会话判定进程是否在跑（见基类说明）。

        只走无会话过滤的直扫 —— 与 ``enumerate()`` 互补：后者用于需要按会话
        收敛的列表场景（冻结候选 / procmon），前者用于「在不在跑」的判定。"""
        try:
            return bool(iter_pids_by_name(name, exact=exact))
        except Exception:
            return False

    def focus_window(self, pid: int) -> bool:
        """Restore + activate the top-level visible windows owned by ``pid``.

        Best-effort: a single-instance app (e.g. Lossless Scaling) that is
        already running gets brought to the foreground instead of being launched
        a second time.  Never raises."""
        if self._user32 is None:
            return False
        try:
            user32 = self._user32
            targets: list[int] = []

            @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
            def _enum(hwnd: int, _lparam: int) -> bool:  # type: ignore[valid-type]
                pid_buf = ctypes.wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
                if pid_buf.value == pid and user32.IsWindowVisible(hwnd):
                    targets.append(hwnd)
                return True

            user32.EnumWindows(_enum, 0)  # type: ignore[arg-type]
            if not targets:
                return False
            for hwnd in targets:
                user32.ShowWindow(hwnd, self.SW_RESTORE)
            user32.SetForegroundWindow(targets[0])
            return True
        except Exception:
            return False

    def suspend(self, pid: int) -> bool:
        if self._ntdll is None or self._kernel32 is None:
            return False
        # R2 底层硬保护：系统关键进程（位于 Windows 系统目录）一律拒绝挂起，
        # 防止名单遗漏导致 NtSuspendProcess 命中关键进程致蓝屏/桌面崩溃。
        if self._is_protected_system_process(pid):
            return False
        try:
            h = self._handle(pid)
            if not h:
                return False
            try:
                # 幂等（计数器防护）：NtSuspendProcess 与 SuspendThread 一样是
                # 计数器语义 —— 对已挂起的进程再挂一次，计数被推到 2，之后
                # 单次 NtResumeProcess 解不开。
                #
                # 2026-09-02 曾把守卫写成 ``is not False``，把「查询失败
                # (None)」也当成「已挂起」而跳过 —— 结果是**假冻结**：查询
                # 一失败就根本不调 NtSuspendProcess，UI 却照常显示「已冻结」
                # 并把窗口最小化、搬到后台桌面，进程其实还在全速跑。
                # 假冻结的危害（用户完全被蒙在鼓里）远大于叠加（已有
                # resume_ex 的抽干循环兜底），故这里收紧为：
                #   * 内核明确说已挂起(True)          -> 跳过
                #   * 查询失败(None) 且账本里有它      -> 跳过（大概率真挂着）
                #   * 其余（含查询失败且账本里没有）    -> 真的挂
                native = self.is_suspended_native(pid)
                if native is True or (native is None
                                      and pid in self._suspended):
                    self._suspended.add(pid)
                    return True
                # 先最小化，再挂起。被挂起的进程其窗口线程无法处理 SC_MINIMIZE
                # 消息，若先 NtSuspendProcess 再最小化，最小化等于空操作 —— 窗口
                # 会以「还原态」停留在（很可能已被移走的）后台虚拟桌面上，而不是
                # 最小化。掌机实测即「进程已移到其他虚拟桌面，但没有最小化」。
                self._set_window_state(pid, True)
                ret = self._ntdll.NtSuspendProcess(h)
                if ret == 0:
                    self._suspended.add(pid)
                    return True
            finally:
                self._kernel32.CloseHandle(h)  # type: ignore[attr-defined]
        except Exception:
            pass
        return False

    def pid_table(self) -> dict[int, str]:
        """当前系统的 ``{pid: 映像名}``（无会话过滤的直扫）。

        抽成方法是为了可替换：Mock / 测试替身覆写它即可脱离真实 ctypes，
        让自己的虚拟进程表成为权威（否则冻结组的失效 PID 剪枝在测试里会把
        所有虚构 PID 都当成"进程已消失"而误删）。结果带 1s TTL 缓存，避免
        逐 PID 重复全系统扫描。
        """
        now = time.time()
        cache = Win32ProcessManager._PID_TABLE_CACHE
        if cache is not None and (now - Win32ProcessManager._PID_TABLE_TS) \
                < Win32ProcessManager._PID_TABLE_TTL_S:
            return cache
        try:
            table = {int(p): nm for p, nm in iter_processes_native()}
        except Exception:
            table = {}
        Win32ProcessManager._PID_TABLE_CACHE = table
        Win32ProcessManager._PID_TABLE_TS = now
        return table

    _PID_TABLE_CACHE: dict[int, str] | None = None
    _PID_TABLE_TS: float = 0.0
    _PID_TABLE_TTL_S = 1.0

    def pid_exists(self, pid: int) -> bool:
        """``pid`` 对应的进程现在还存在吗（无会话过滤的直扫）。

        命中 PID 表缓存时很快；**未命中时必须再直扫一次确认** —— 缓存有
        1 s TTL，刚启动的进程可能还没进缓存，若据此直接判「不存在」，
        ``resume_ex`` 会把它当成 gone 而跳过恢复，进程就白白冻在内核里。
        假阴性（漏解冻）的代价远大于多扫一次的开销。
        """
        pid = int(pid or 0)
        if pid <= 0:
            return False
        try:
            if pid in self.pid_table():
                return True
        except Exception:
            pass
        try:
            for p, _nm in iter_processes_native():
                if int(p) == pid:
                    return True
        except Exception:
            pass
        return False

    def pid_reachability(self, pid: int) -> str:
        """``'alive'`` / ``'gone'`` / ``'denied'``。

        * ``gone``   —— 进程已不在系统进程列表（PID 失效 / 早已退出）
        * ``denied`` —— 进程在，但 ``OpenProcess(PROCESS_SUSPEND_RESUME)`` 失败
                        （受保护进程，或 PID 已被系统复用到受保护进程）
        * ``alive``  —— 在，且能拿到句柄

        实测依据：Windows 上约 39% 的进程属于 ``denied``（svchost / csrss /
        lsass / dwm / Secure System / fontdrvhost / 各类驱动服务）。冻结组里
        一旦残留一个这样的 PID，旧逻辑就会让**整组**解冻失败（而主进程其实
        已经恢复成功了）。用无会话过滤的直扫判定存活，避免会话过滤误判。
        """
        pid = int(pid or 0)
        if pid <= 0:
            return "gone"
        if not self.pid_exists(pid):
            return "gone"
        h = self._handle(pid)
        if not h:
            return "denied"
        try:
            self._kernel32.CloseHandle(h)  # type: ignore[attr-defined]
        except Exception:
            pass
        return "alive"

    def resume_ex(self, pid: int, restore_window: bool = True) -> str:
        """恢复 ``pid``，返回 ``RES_*`` 结局之一（语义见模块顶部常量）。

        ``restore_window`` 含义同 :meth:`resume`。

        与旧版 bool 返回的关键差别：把「进程已经不存在（``gone``）」从失败里
        摘出来。旧版 ``ok = all(resume(...))`` 会把一个早已失效的陈旧 PID 当成
        硬失败，一票否决整组 —— 用户看到的就是「窗口被拉回当前桌面了，然后
        报解冻失败」。
        """
        if self._ntdll is None or self._kernel32 is None:
            return RES_FAILED
        pid = int(pid or 0)
        if pid <= 0:
            return RES_GONE

        reach = self.pid_reachability(pid)
        if reach == "gone":
            # 进程已不存在：它不再需要解冻，这**不是**失败。同步掉账本即可。
            self._suspended.discard(pid)
            return RES_GONE
        if reach == "denied":
            # 进程在但打不开句柄：真失败（受保护 / PID 被复用），保留账本。
            return RES_DENIED

        h = self._handle(pid)
        if not h:
            return RES_DENIED
        try:
            # 幂等判定：**仅当**内存账本里没有它（说明不是本工具挂的）**且**
            # 内核明确说没挂起，才判定「无需恢复」。二者任一存疑都必须真的
            # 调用 NtResumeProcess —— 否则就是「假解冻」：进程仍冻在内核里、
            # 账本却已清空，用户再冻结就把挂起计数叠到 2，单次 resume 永远
            # 解不开（这正是 2026-09-02 那次修复没能根除的病根）。
            # 判据之所以不能全信：它要求「所有线程 State==Waiting 且
            # WaitReason==Suspended」，而 Windows 自己就会用 PLM 挂起 UWP
            # 进程、游戏也会自己 SuspendThread，复杂进程极易出现部分线程
            # 状态不一致 -> 误判为「没挂」。
            ledger = pid in self._suspended
            native = self.is_suspended_native(pid)
            if native is False and not ledger:
                if restore_window:
                    self._set_window_state(pid, False)
                return RES_RUNNING
            # 计数器语义抽干：NtResumeProcess 每次仅把内核挂起计数 -1，
            # 若此前被叠加挂起（计数到 2+），单次调用解不开。循环直到内核
            # 确认已恢复（上限 8 次防极端/权限死循环）。
            #
            # ``force`` = 账本说这是我们挂的 —— 那么**第一轮无条件真调一次**
            # NtResumeProcess，不先问判据。因为判据可能正误判为「没挂」，
            # 若照旧先问就直接 break 了，等于一次都没解 —— 假解冻原地复发。
            ret = 1
            force = bool(ledger)
            for _ in range(8):
                if not force and self.is_suspended_native(pid) is False:
                    ret = 0
                    break
                force = False
                try:
                    ret = self._ntdll.NtResumeProcess(h)
                except Exception:
                    ret = 1
                    break
                if ret != 0:
                    break  # 系统调用失败：立即停止空转，如实回报
            if ret == 0:
                self._suspended.discard(pid)
                if restore_window:
                    self._set_window_state(pid, False)
                return RES_RESUMED
            # 抽干 8 次仍挂着 / 调用失败：如实回报失败并保留账本，
            # 让 HUD 知道解冻未完成、可重试，而不是假装成功。
            return RES_FAILED
        except Exception:
            return RES_FAILED
        finally:
            try:
                self._kernel32.CloseHandle(h)  # type: ignore[attr-defined]
            except Exception:
                pass

    def resume(self, pid: int, restore_window: bool = True) -> bool:
        """Resume ``pid``'s threads (``NtResumeProcess``).

        向后兼容的 bool 版，等价于 ``resume_ex(...) in RES_OK`` ——
        即 ``gone``（进程已不存在）**算成功**：它无需解冻，不该拖累整组。

        ``restore_window=True`` (default) additionally restores + activates the
        process's visible windows (``SW_RESTORE``) — the historical behaviour.

        ``restore_window=False`` only un-suspends the threads and leaves the
        windows untouched.  The freeze block's unfreeze path uses this to avoid
        the "window restored on the BACKGROUND desktop -> Windows jumps the
        active desktop" bug: it moves the windows back to the current desktop
        FIRST (they are now responsive, so the move is fast), then restores
        them via :meth:`restore_window` — at which point they are already on
        the user's desktop and cannot yank the desktop away.
        """
        return self.resume_ex(pid, restore_window) in RES_OK

    def restore_window(self, pid: int) -> None:
        """Restore + activate ``pid``'s visible windows (``SW_RESTORE``).

        Separate from :meth:`resume` so the unfreeze flow can first move the
        windows to the current virtual desktop (while the process is already
        responsive) and only THEN restore them — restoring a window that is
        still parked on the background desktop would make Windows switch the
        active desktop over to it (the "解冻后跳到后台桌面" bug).  Best-effort;
        never raises.
        """
        try:
            self._set_window_state(pid, False)
        except Exception:
            pass

    def is_suspended(self, pid: int) -> bool:
        return pid in self._suspended

    # -- 内核级真实挂起态（NtQuerySystemInformation） ----------------------
    # ``_suspended`` 是本进程的内存账本：App 重启后清零、休眠恢复后可能滞后，
    # 而 NtSuspendProcess 是**计数器语义**（同一线程可叠加多次挂起），账本与
    # 内核真值一旦脱节，就会出现「记录说不挂、其实挂着」「resume 一次解不开
    # 双重挂起」。这里直接问内核：该 pid 的所有线程是否都处于
    # ThreadState==Waiting(5) 且 WaitReason==WrSuspended(5)。
    #
    # psutil 在 Windows 上不暴露 wait_reason（只有 Linux 有），所以必须走
    # ctypes 的 NtQuerySystemInformation(SystemProcessInformation=5)。
    # 结构体偏移按 x64 公开布局（phnt / Process Hacker 同源），写死并集中
    # 在一个 dataclass 里，便于核对。

    _SYS_PROC_INFO_CLASS = 5      # SystemProcessInformation
    _STATUS_INFO_LEN_MISMATCH = 0xC0000004
    _THREAD_STATE_WAITING = 5
    _WAIT_REASON_SUSPENDED = 5    # KWAIT_REASON.WrSuspended
    # x64 SYSTEM_THREAD_INFORMATION（sizeof=80）
    _OFF_THREAD_STATE = 68
    _OFF_WAIT_REASON = 72
    _SIZEOF_THREAD_INFO = 80
    # x64 SYSTEM_PROCESS_INFORMATION：线程数组从 +256 开始
    _OFF_NUM_THREADS = 4
    _OFF_PID = 80
    _OFF_FIRST_THREAD = 256

    def is_suspended_native(self, pid: int) -> bool | None:
        """问内核这个进程**现在**是否真的被挂起。

        返回 True / False；查询失败（权限、缓冲区、结构解析）返回 ``None``，
        调用方应回退到内存账本语义 —— 永远不要把「未知」当「未挂起」。
        """
        if self._ntdll is None:
            return None
        size = 1 << 22  # 4 MiB 起步，不够就翻倍（进程多的大机器）
        buf = None
        try:
            for _ in range(7):  # 最多翻到 256 MiB
                buf = ctypes.create_string_buffer(size)
                ret = self._ntdll.NtQuerySystemInformation(
                    self._SYS_PROC_INFO_CLASS, buf, size, None)
                if ret == 0:
                    break
                # ⚠ NTSTATUS 是 32 位**无符号**，而本函数未设 restype ——
                # ctypes 默认返回【有符号 c_int】，缓冲区不足时拿到的是
                # -1073741820 而不是 0xC0000004。直接与无符号常量比较永远
                # 不成立，于是缓冲区永不翻倍重试、函数直接 return None
                # （实测确认：见 _diag_ntqsi.py）。必须先掩回 32 位。
                if (ret & 0xFFFFFFFF) == self._STATUS_INFO_LEN_MISMATCH:
                    size *= 2
                    continue
                return None
            else:
                return None
        except Exception:
            return None

        try:
            off = 0
            limit = size
            while True:
                base = ctypes.addressof(buf) + off
                next_off = ctypes.c_uint32.from_address(base).value
                n_threads = ctypes.c_uint32.from_address(
                    base + self._OFF_NUM_THREADS).value
                upid = ctypes.c_uint64.from_address(
                    base + self._OFF_PID).value
                if int(upid) == int(pid):
                    if n_threads == 0:
                        return None  # 读不到线程（权限）→ 未知
                    tbase = base + self._OFF_FIRST_THREAD
                    for i in range(n_threads):
                        t = tbase + i * self._SIZEOF_THREAD_INFO
                        state = ctypes.c_uint32.from_address(
                            t + self._OFF_THREAD_STATE).value
                        reason = ctypes.c_uint32.from_address(
                            t + self._OFF_WAIT_REASON).value
                        if (state != self._THREAD_STATE_WAITING
                                or reason != self._WAIT_REASON_SUSPENDED):
                            return False
                    return True
                if next_off == 0:
                    return False  # 遍历完没找到 → 进程不存在
                off += next_off
                if off >= limit:
                    return None
        except Exception:
            return None

    def enumerate(self) -> list[ProcessInfo]:
        """List processes in the current session (never ourselves).

        Robust against ``wmic``'s column reordering: we parse by *header name*
        (``csv.DictReader``) instead of fixed positions.  If ``wmic`` yields
        nothing (deprecated/removed on some builds) we fall back to a
        ``Get-CimInstance`` PowerShell query — faithful to the original PS1.
        Each entry is also annotated with ``has_visible_window`` (whether the
        process owns a visible top-level window) for the freeze block's
        foreground-prioritised ranking.
        """
        rows = self._enum_wmic()
        if not rows:
            rows = self._enum_cim()
        sid = self._current_session_id()
        self_pid = os.getpid()
        visible = self._visible_window_pids()
        out: list[ProcessInfo] = []
        for p in rows:
            if p.pid == self_pid:
                continue
            if sid is not None and p.session_id != sid:
                continue
            p.has_visible_window = p.pid in visible
            out.append(p)
        return out

    # Window-visibility probing is somewhat expensive (a full EnumWindows
    # walk), so cache the result briefly — the freeze/procmon panels refresh on
    # the order of seconds, not milliseconds.
    _VIS_CACHE: set[int] | None = None
    _VIS_CACHE_TS: float = 0.0
    _VIS_CACHE_TTL_S = 1.5

    def _visible_window_pids(self) -> set[int]:
        now = time.time()
        cache = Win32ProcessManager._VIS_CACHE
        if cache is not None and (now - Win32ProcessManager._VIS_CACHE_TS) \
                < Win32ProcessManager._VIS_CACHE_TTL_S:
            return cache
        visible = _collect_visible_window_pids()
        Win32ProcessManager._VIS_CACHE = visible
        Win32ProcessManager._VIS_CACHE_TS = now
        return visible

    @staticmethod
    def _row_to_info(row: dict[str, str]) -> ProcessInfo | None:
        pid_s = (row.get("ProcessId") or "").strip()
        if not pid_s.isdigit():
            return None
        try:
            pid = int(pid_s)
            mem = float(row.get("WorkingSetSize") or 0) / (1024 * 1024)
            psid = int(row.get("SessionId") or 0)
        except ValueError:
            return None
        return ProcessInfo(
            pid=pid,
            name=(row.get("Name") or "").strip(),
            cmdline=(row.get("CommandLine") or "").strip(),
            memory_mb=mem,
            session_id=psid,
        )

    def _enum_wmic(self) -> list[ProcessInfo]:
        try:
            raw = subprocess.run(
                ["wmic", "process", "get", "ProcessId,Name,CommandLine,WorkingSetSize,SessionId", "/format:csv"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
                timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout or b""
            text = _decode_cli(raw).replace("\r", "\n").strip()
            if not text:
                return []
            reader = csv.DictReader(io.StringIO(text))
            out: list[ProcessInfo] = []
            for row in reader:
                info = self._row_to_info(row)
                if info is not None:
                    out.append(info)
            return out
        except Exception:
            return []

    def _enum_cim(self) -> list[ProcessInfo]:
        """PowerShell ``Get-CimInstance Win32_Process`` fallback (original PS1
        source of truth).  Triggered only when ``wmic`` returns nothing."""
        try:
            ps = (
                "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,"
                "CommandLine,WorkingSetSize,SessionId | "
                "ConvertTo-Csv -NoTypeInformation"
            )
            raw = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", ps],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
                timeout=25, creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout or b""
            text = _decode_cli(raw).replace("\r", "\n").strip()
            if not text:
                return []
            reader = csv.DictReader(io.StringIO(text))
            out: list[ProcessInfo] = []
            for row in reader:
                info = self._row_to_info(row)
                if info is not None:
                    out.append(info)
            return out
        except Exception:
            return []

    def start(self, path: str, **kw: Any) -> bool:
        """Launch a target with its associated host (faithful to the original
        ``Toggle-ProcMonScript`` / ``Invoke-TileLaunch``).

        ``.bat``/``.cmd`` → ``cmd.exe /c``; ``.ps1`` → ``powershell -File``;
        ``.vbs``/``.vbe`` → ``wscript``; everything else uses the shell
        association via ``os.startfile`` (handles ``.exe``/``.lnk``/``.url``…).
        """
        try:
            ext = os.path.splitext(path)[1].lower()
            proc = None
            if ext in (".bat", ".cmd"):
                proc = subprocess.Popen(["cmd.exe", "/c", path], shell=False,  # noqa: S603
                                        creationflags=subprocess.CREATE_NO_WINDOW)
            elif ext == ".ps1":
                proc = subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", path], shell=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            elif ext in (".vbs", ".vbe"):
                proc = subprocess.Popen(["wscript.exe", path], shell=False)
            elif ext == AHK_EXT:
                # Launch via the AHK interpreter so the script path is recorded
                # in the process command line and is therefore monitorable by
                # the procmon block.  Fall back to the file association if the
                # interpreter cannot be located.
                host = find_autohotkey()
                if host:
                    proc = subprocess.Popen([host, path], shell=False)
                else:
                    os.startfile(path)  # type: ignore[attr-defined]
            else:
                os.startfile(path)  # type: ignore[attr-defined]
            if proc is not None:
                # Fire-and-forget: detach so the child runs independently
                # (like os.startfile) and the Popen object's __del__ does not
                # emit a ResourceWarning / leak the handle when it is GC'd.
                proc.detach()
            return True
        except Exception:
            return False

    def stop(self, pid_or_name: int | str) -> bool:
        try:
            if isinstance(pid_or_name, int):
                subprocess.run(["taskkill", "/PID", str(pid_or_name), "/F"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              check=False,
                              creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                subprocess.run(["taskkill", "/IM", str(pid_or_name), "/F"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              check=False,
                              creationflags=subprocess.CREATE_NO_WINDOW)
            return True
        except Exception:
            return False

    def stop_matching(self, cmd_substr: str) -> bool:
        """Kill processes whose command line contains ``cmd_substr``.

        Mirrors the original procmon stop path: it enumerates the host process
        (``cmd.exe``/``powershell.exe``/…) and matches the *target path* in the
        command line, then kills by PID — not by image name (which would hit
        every unrelated ``cmd.exe``).  Path separators are normalized so a
        config stored with ``/`` matches a Windows command line using ``\\``.
        """
        cmd_substr = norm_path_sep((cmd_substr or "").lower())
        if not cmd_substr:
            return False
        killed = False
        for p in self.enumerate():
            if cmd_substr in norm_path_sep((getattr(p, "cmdline", "") or "").lower()):
                try:
                    subprocess.run(["taskkill", "/PID", str(p.pid), "/F"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  check=False,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
                    killed = True
                except Exception:
                    pass
        return killed

    def is_running(self, name: str) -> bool:
        name = name.lower()
        return any(p.name.lower() == name for p in self.enumerate())


class MockProcessManager(ProcessManager):
    """Records suspend/resume/start/stop calls; fully controllable process list."""

    def __init__(self, processes: list[ProcessInfo] | None = None,
                 exe_paths: dict[str, str] | None = None,
                 denied_pids: set[int] | None = None) -> None:
        self.suspended: set[int] = set()
        self.resumed: set[int] = set()
        self.started: list[str] = []
        self.stopped: list[int | str] = []
        self.processes: list[ProcessInfo] = list(processes or [])
        # image-name -> full path, used by find_exe_path_by_name (tests inject
        # e.g. {"losslessscaling.exe": "C:\\LS\\losslessscaling.exe"}).
        self.exe_paths: dict[str, str] = dict(exe_paths or {})
        # 模拟「进程在但 OpenProcess 被拒」的 PID（受保护/被复用），用于
        # 覆盖解冻组里失效成员不再一票否决的行为。
        self.denied_pids: set[int] = set(denied_pids or ())

    @property
    def available(self) -> bool:
        return True

    def suspend(self, pid: int) -> bool:
        self.suspended.add(pid)
        return True

    def note_suspended(self, pid: int) -> None:
        """Mock 的账本就是 ``suspended`` 集合（见 :meth:`resume_ex`）。"""
        self.suspended.add(int(pid))

    def pid_table(self) -> dict[int, str]:
        """Mock 的权威进程表就是自己注入的 ``processes``。"""
        return {int(getattr(p, "pid", 0) or 0): getattr(p, "name", "")
                for p in self.processes}

    def pid_reachability(self, pid: int) -> str:
        pid = int(pid or 0)
        if pid <= 0:
            return "gone"
        if pid in self.denied_pids:
            return "denied"
        if any(int(getattr(p, "pid", 0) or 0) == pid for p in self.processes):
            return "alive"
        # Mock 世界：只 suspend 过、没登记进 processes 的也算存活（大量测试
        # 只驱动 suspend/resume 而不构造进程列表）。
        if pid in self.suspended:
            return "alive"
        return "gone"

    def resume_ex(self, pid: int, restore_window: bool = True) -> str:
        pid = int(pid or 0)
        reach = self.pid_reachability(pid)
        if reach == "gone":
            # 进程已不存在：无需解冻，不算失败（与 Win32 实现保持一致）。
            self.resumed.add(pid)
            self.suspended.discard(pid)
            return RES_GONE
        if reach == "denied":
            return RES_DENIED
        self.resumed.add(pid)
        self.suspended.discard(pid)
        return RES_RESUMED

    def resume(self, pid: int, restore_window: bool = True) -> bool:
        return self.resume_ex(pid, restore_window) in RES_OK

    def restore_window(self, pid: int) -> None:
        pass

    def is_suspended(self, pid: int) -> bool:
        return pid in self.suspended

    def is_suspended_native(self, pid: int) -> bool | None:
        # Mock 语义：内存集合即「内核真值」—— Mock.suspend 挂上的就算挂着。
        # 不在集合里一律 False（Mock 世界没有「未知」）。
        return pid in self.suspended

    def enumerate(self) -> list[ProcessInfo]:
        return list(self.processes)

    def start(self, path: str, **kw: Any) -> bool:
        self.started.append(path)
        self.processes.append(ProcessInfo(pid=10000 + len(self.processes),
                                           name=path.rsplit("\\", 1)[-1]))
        return True

    def stop(self, pid_or_name: int | str) -> bool:
        self.stopped.append(pid_or_name)
        return True

    def stop_matching(self, cmd_substr: str) -> bool:
        sub = norm_path_sep((cmd_substr or "").lower())
        hit = False
        keep: list[ProcessInfo] = []
        for p in self.processes:
            if sub and sub in norm_path_sep((p.cmdline or "").lower()):
                self.stopped.append(p.pid)
                hit = True
            else:
                keep.append(p)
        self.processes = keep
        return hit

    def is_running(self, name: str) -> bool:
        name = name.lower()
        return any(p.name.lower() == name for p in self.processes)

    def find_exe_path_by_name(self, name: str, fuzzy: bool = True) -> str | None:
        key = (name or "").lower()
        if key in self.exe_paths:
            return self.exe_paths[key]
        return None

    def find_pid_by_name(self, name: str, exact: bool = False) -> int | None:
        key = (name or "").lower()
        for p in self.processes:
            nm = (p.name or "").lower()
            if (nm == key) if exact else (key in nm):
                return p.pid
        return None

    def is_running_anywhere(self, name: str, exact: bool = False) -> bool:
        return self.find_pid_by_name(name, exact=exact) is not None

    def focus_window(self, pid: int) -> bool:
        return any(p.pid == pid for p in self.processes)