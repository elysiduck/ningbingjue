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
        """从 ``wmic ... get ExecutablePath /format:csv`` 输出里取出首个非空路径。"""
        text = _decode_cli(raw)
        if not text.strip():
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
            text = _decode_cli(raw)
            if text.strip():
                reader = csv.DictReader(io.StringIO(text))
                for row in reader:
                    ep = (row.get("ExecutablePath") or "").strip().strip('"')
                    if ep and os.path.exists(ep):
                        return ep
        except Exception:
            pass
        return ""

    def find_exe_path_by_name(self, name: str) -> str | None:
        """Return the full image path of the first running process whose image
        name (case-insensitive) equals ``name``.

        Used by the LS launcher's "auto-set" feature: scan for
        ``losslessscaling.exe`` and read its on-disk location so it can be
        re-launched later.  Returns ``None`` when no match or on any failure.

        探测策略（见 ``_find_exe_path_via_wmi`` 说明）：优先用 WMI
        ``ExecutablePath``（无需句柄、跨会话/跨 IL 鲁棒），失败再回退到
        ``enumerate()`` + ``QueryFullProcessImageNameW``。
        """
        if self._kernel32 is None:
            return None
        name = (name or "").lower()
        if not name:
            return None
        # 主路径：WMI ExecutablePath（鲁棒，跨会话/跨 IL/无需 OpenProcess）。
        exe = self._find_exe_path_via_wmi(name)
        if exe:
            return exe
        # 兜底：enumerate() + QueryFullProcessImageNameW（需打开目标句柄）。
        try:
            for p in self.enumerate():
                if p.name.lower() == name:
                    exe = self._query_image_name(p.pid)
                    if exe:
                        return exe
        except Exception:
            return None
        return None

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
            # 先最小化，再挂起。被挂起的进程其窗口线程无法处理 SC_MINIMIZE
            # 消息，若先 NtSuspendProcess 再最小化，最小化等于空操作 —— 窗口
            # 会以「还原态」停留在（很可能已被移走的）后台虚拟桌面上，而不是
            # 最小化。掌机实测即「进程已移到其他虚拟桌面，但没有最小化」。
            self._set_window_state(pid, True)
            ret = self._ntdll.NtSuspendProcess(h)
            self._kernel32.CloseHandle(h)  # type: ignore[attr-defined]
            if ret == 0:
                self._suspended.add(pid)
                return True
        except Exception:
            pass
        return False

    def resume(self, pid: int, restore_window: bool = True) -> bool:
        """Resume ``pid``'s threads (``NtResumeProcess``).

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
        if self._ntdll is None or self._kernel32 is None:
            return False
        try:
            h = self._handle(pid)
            if not h:
                return False
            ret = self._ntdll.NtResumeProcess(h)
            self._kernel32.CloseHandle(h)  # type: ignore[attr-defined]
            if ret == 0:
                self._suspended.discard(pid)
                if restore_window:
                    self._set_window_state(pid, False)
                return True
        except Exception:
            pass
        return False

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
            text = _decode_cli(raw)
            if not text.strip():
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
            text = _decode_cli(raw)
            if not text.strip():
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
                 exe_paths: dict[str, str] | None = None) -> None:
        self.suspended: set[int] = set()
        self.resumed: set[int] = set()
        self.started: list[str] = []
        self.stopped: list[int | str] = []
        self.processes: list[ProcessInfo] = list(processes or [])
        # image-name -> full path, used by find_exe_path_by_name (tests inject
        # e.g. {"losslessscaling.exe": "C:\\LS\\losslessscaling.exe"}).
        self.exe_paths: dict[str, str] = dict(exe_paths or {})

    @property
    def available(self) -> bool:
        return True

    def suspend(self, pid: int) -> bool:
        self.suspended.add(pid)
        return True

    def resume(self, pid: int, restore_window: bool = True) -> bool:
        self.resumed.add(pid)
        self.suspended.discard(pid)
        return True

    def restore_window(self, pid: int) -> None:
        pass

    def is_suspended(self, pid: int) -> bool:
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

    def find_exe_path_by_name(self, name: str) -> str | None:
        return self.exe_paths.get((name or "").lower())

    def focus_window(self, pid: int) -> bool:
        return any(p.pid == pid for p in self.processes)