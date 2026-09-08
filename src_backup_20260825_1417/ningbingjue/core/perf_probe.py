# -*- coding: utf-8 -*-
"""core/perf_probe.py - single source of truth for performance probing.

Owns the canonical PowerShell probe scripts and the shared monitor starters so
that every performance-detection task in the project uses *identical*,
battle-tested code instead of copy-pasted ``\\GPU Engine(*)`` loops that drift
out of sync:

  - measure_game_load.py  (game-load recorder / undervolt threshold采集)
  - record_load.py        (generic load recorder)
  - core/metrics.py       (in-app shared MetricSampler)
  - platform/ryzenadj.py  (_GpuUtilMonitor, feeds GPU auto-lock)

In-app sampling (2026-08-19): the 2~5 resident PowerShell processes (GPU +
per-core + thermal, one each) were merged into ONE host — ``COMBINED_PS`` +
:class:`SharedMonitor` — that emits ``G``/``C``/``T`` prefixed lines and
dispatches them to subscribers (MetricSampler, ThermalProbe, SmtChartWidget,
_GpuUtilMonitor).  The standalone recorders keep using the legacy
``GPU_UTIL_PS`` / ``CORE_UTIL_PS`` + ``start_gpu_monitor`` /
``start_core_monitor`` short-lived probes.

The GPU script below is the *robust* variant (try/catch + explicit stdout flush
+ stderr error reporting) that originally lived in ryzenadj._GpuUtilMonitor and
fixed the "GPU util always 0 in elevated session" bug. Lifting it here means the
standalone recorders inherit that fix for free.

Only depends on the standard library (+ ctypes on Windows). Importing this
module is safe on non-Windows (the ctypes-backed helpers are stubbed); the
actual probing only runs on Windows.
"""
from __future__ import annotations

import atexit
import subprocess
import sys
import threading
import time
from ctypes import wintypes

# --------------------------------------------------------------------------
# Canonical PowerShell probe scripts
# --------------------------------------------------------------------------
# GPU utilization: sum of \GPU Engine(*)\Utilization Percentage.
# Robust variant: surfaces Get-Counter failures to stderr (LOOP_ERR/FATAL)
# instead of silently producing 0 forever.
GPU_UTIL_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
try {
  while ($true) {
    try {
      # 用 SilentlyContinue 代替 -ErrorAction Stop：掌机（尤其 HX370）上
      # \GPU Engine(*) 计数器偶发“一个性能计数器示例中的数据无效”（Status≠0
      # 的坏实例），用 Stop 会让整次 Get-Counter 抛异常、永远读不到任何值，
      # 导致 GPU 浮动/自动锁频因拿不到利用率而彻底失效。改为取回 CounterSamples
      # 后逐实例过滤 Status=0 的有效样本再求和，坏实例被跳过、有效实例仍能用。
      $c = Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction SilentlyContinue
      $sum = 0.0
      $valid = 0
      if ($c -and $c.CounterSamples) {
        foreach ($s in $c.CounterSamples) {
          if ($s.Status -eq 0 -and $null -ne $s.CookedValue) {
            $sum += [double]$s.CookedValue
            $valid++
          }
        }
      }
      if ($valid -gt 0) {
        # 多引擎实例（3D/Copy/Video…）求和可能 >100%，按上限钳制，
        # 避免浮动/自动锁频的 88/92% 阈值被恒触发。
        if ($sum -gt 100.0) { $sum = 100.0 }
        [Console]::Out.WriteLine($sum)
        [Console]::Out.Flush()   # stdout is a pipe (not TTY) -> .NET does not flush per line
      } else {
        [Console]::Error.WriteLine('LOOP_ERR: 所有 GPU Engine 实例状态无效（计数器可能已损坏，请以管理员运行 lodctr /r 重建）')
      }
    } catch {
      [Console]::Error.WriteLine(('LOOP_ERR: ' + $_.Exception.Message))
    }
    Start-Sleep -Milliseconds 1500
  }
} catch {
  [Console]::Error.WriteLine(('FATAL: ' + $_.Exception.Message))
}
"""

# Per-core utilization: \Processor(*)\% Processor Time, excluding _Total.
# Emits "idx:val|idx:val|..." lines (e.g. "0:12.3|1:45.6|...").
CORE_UTIL_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
while ($true) {
  $samples = (Get-Counter '\Processor(*)\% Processor Time' -ErrorAction SilentlyContinue).CounterSamples
  if ($samples) {
    $parts = @()
    foreach ($s in $samples) {
      if ($s.InstanceName -eq '_Total') { continue }
      $parts += ($s.InstanceName + ':' + $s.CookedValue.ToString('F1'))
    }
    [Console]::Out.WriteLine(($parts -join '|'))
  } else {
    [Console]::Out.WriteLine('')
  }
    [Console]::Out.Flush()
  Start-Sleep -Milliseconds 1000
}
"""

# Single-process merged monitor (2026-08-19): GPU + per-core + thermal in ONE
# long-lived PowerShell.  The old design spawned one PowerShell per signal
# (GPU_UTIL_PS / CORE_UTIL_PS / THERMAL_PS), so the app held 2~5 resident
# powershell.exe at once.  This merges them into a single process that emits
# one prefixed line per signal every tick (~1 s):
#
#   "G <gpu_pct>"              GPU engine utilization sum (clamped 0-100)
#   "C <idx:val|idx:val|...>"  per-core processor time
#   "T <temp_c>"               hottest ACPI thermal zone (deg C)
#   stderr: "LOOP_ERR: <CH>: <msg>"  probe errors (surfaced, not swallowed)
#
# The old GPU_UTIL_PS / CORE_UTIL_PS constants are kept for the standalone
# recorders (measure_game_load.py / record_load.py / topology.py) which spawn
# short-lived probes; the in-app samplers all consume COMBINED_PS through
# :class:`SharedMonitor` below.
COMBINED_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
while ($true) {
  # --- GPU engine utilization (robust variant: skip Status!=0 bad instances) ---
  try {
    $c = Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction SilentlyContinue
    $sum = 0.0; $valid = 0
    if ($c -and $c.CounterSamples) {
      foreach ($s in $c.CounterSamples) {
        if ($s.Status -eq 0 -and $null -ne $s.CookedValue) {
          $sum += [double]$s.CookedValue; $valid++
        }
      }
    }
    if ($valid -gt 0) {
      if ($sum -gt 100.0) { $sum = 100.0 }
      [Console]::Out.WriteLine(('G ' + $sum.ToString('F1')))
    }
  } catch {
    [Console]::Error.WriteLine(('LOOP_ERR: GPU: ' + $_.Exception.Message))
  }
  # --- per-core processor time (exclude _Total) ---
  try {
    $samples = (Get-Counter '\Processor(*)\% Processor Time' -ErrorAction SilentlyContinue).CounterSamples
    if ($samples) {
      $parts = @()
      foreach ($s in $samples) {
        if ($s.InstanceName -eq '_Total') { continue }
        $parts += ($s.InstanceName + ':' + $s.CookedValue.ToString('F1'))
      }
      if ($parts.Count -gt 0) { [Console]::Out.WriteLine(('C ' + ($parts -join '|'))) }
    }
  } catch {
    [Console]::Error.WriteLine(('LOOP_ERR: CORE: ' + $_.Exception.Message))
  }
  # --- hottest ACPI thermal zone (MSAcpi_ThermalZoneTemperature, tenths of K) ---
  try {
    $zs = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop
    if ($zs) {
      $best = $null
      foreach ($z in $zs) {
        $raw = $z.CurrentTemperature
        if ($null -ne $raw) {
          $raw = [double]$raw
          if ($raw -gt 4000.0) { $c = ($raw / 100.0) - 273.15 } else { $c = ($raw / 10.0) - 273.15 }
          if ($c -ge 0.0 -and $c -le 130.0) {
            if ($null -eq $best -or $c -gt $best) { $best = $c }
          }
        }
      }
      if ($null -ne $best) { [Console]::Out.WriteLine(('T ' + $best.ToString('F1'))) }
    }
  } catch {
    [Console]::Error.WriteLine(('LOOP_ERR: TEMP: ' + $_.Exception.Message))
  }
  [Console]::Out.Flush()
  Start-Sleep -Milliseconds 1000
}
"""


def start_powershell(script: str, capture_stderr: bool = False,
                     errors: "str | None" = None):
    """Launch a background PowerShell monitor.

    Returns (proc, None) on success or (None, err) if the process could not
    be spawned. ``capture_stderr=True`` lets the caller read PowerShell errors
    (used by the in-app GPU monitor for diagnostics); otherwise stderr is
    discarded.  ``errors`` is passed to ``subprocess.Popen`` for text-mode
    decoding — PowerShell writes stderr in the system OEM codepage (e.g. GBK),
    so a UTF-8 decoder can raise ``UnicodeDecodeError`` and silently drop the
    error line; pass ``errors="replace"`` when you must survive any byte.
    """
    try:
        proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
            text=True, bufsize=1, errors=errors,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as e:
        return None, str(e)
    return proc, None


def start_gpu_monitor(enabled: bool):
    """Start the shared GPU-utilization monitor.

    Returns (state, proc). ``state`` is ``{"v": float|None, "ok": bool, "err": str}``
    and is continuously updated by a reader thread. ``proc`` is None when
    disabled or spawn failed.
    """
    state: dict = {"v": None, "ok": False, "err": ""}
    if not enabled:
        return state, None
    proc, err = start_powershell(GPU_UTIL_PS)
    if proc is None:
        state["err"] = f"GPU monitor start failed: {err}"
        return state, None

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            s = line.strip()
            try:
                state["v"] = float(s)
                state["ok"] = True
            except ValueError:
                if s:
                    state["err"] = s[:200]

    threading.Thread(target=_reader, daemon=True).start()
    return state, proc


def start_core_monitor(enabled: bool):
    """Start the shared per-core utilization monitor.

    Returns (state, proc). ``state`` is
    ``{"cores": dict[idx->float]|None, "ok": bool, "err": str}`` updated by a
    reader thread. ``proc`` is None when disabled or spawn failed.
    """
    state: dict = {"cores": None, "ok": False, "err": ""}
    if not enabled:
        return state, None
    proc, err = start_powershell(CORE_UTIL_PS)
    if proc is None:
        state["err"] = f"core monitor start failed: {err}"
        return state, None

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            d: dict = {}
            for tok in line.split("|"):
                if ":" in tok:
                    idx, _, val = tok.partition(":")
                    try:
                        d[idx] = float(val)
                    except ValueError:
                        pass
            if d:
                state["cores"] = d
                state["ok"] = True

    threading.Thread(target=_reader, daemon=True).start()
    return state, proc


def kill_orphaned_monitors() -> int:
    """Kill orphaned monitor PowerShell processes left running by previous
    (force-killed) app instances.

    These are ``powershell.exe`` processes executing our ``Get-Counter`` monitor
    loops (per-core / GPU utilization — both the legacy per-signal scripts and
    the merged ``COMBINED_PS`` match: it contains ``Get-Counter``,
    ``% Processor Time`` and ``Utilization Percentage``).  They are matched
    *strictly* by command-line signature so ordinary PowerShell sessions are
    never touched.  Returns the number of processes killed (0 on any failure /
    non-Windows).

    This is a self-healing guard: the buggy pre-fix ``CoreSampler`` could leave
    a PowerShell ``Get-Counter`` loop orphaned when the app was force-killed
    mid-read, and those accumulate across launches and peg the performance
    counter subsystem.  The merged host's kill-on-close Job Object prevents new
    orphans; this function cleans up legacy ones.
    """
    if sys.platform != "win32":
        return 0
    try:
        ps = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" "
             "| Where-Object { $_.CommandLine -match 'Get-Counter' -and "
             "($_.CommandLine -match '% Processor Time' -or "
             "$_.CommandLine -match 'Utilization Percentage') } "
             "| ForEach-Object { $_.ProcessId }"],
            capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        return 0
    killed = 0
    for line in ps.stdout.splitlines():
        line = line.strip()
        if not line.isdigit():
            continue
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(line)],
                           capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW,
                           timeout=10)
            killed += 1
        except Exception:
            pass
    return killed


# --------------------------------------------------------------------------
# Windows process-CPU reader (GetProcessTimes delta) - shared by the
# standalone recorders. Stubbed off-Windows so this module imports cleanly.
# --------------------------------------------------------------------------
_WIN = sys.platform == "win32"

if _WIN:
    import ctypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                    ("dwHighDateTime", ctypes.c_uint32)]

        def as_int64(self) -> int:
            return (self.dwHighDateTime << 32) | self.dwLowDateTime

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    NO_INHERIT = False

    _GetProcessTimes = _kernel32.GetProcessTimes
    _GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
    ]
    _GetProcessTimes.restype = wintypes.BOOL

    _OpenProcess = _kernel32.OpenProcess
    _OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _OpenProcess.restype = wintypes.HANDLE

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL

    def open_process(access: int, inherit: bool, pid: int) -> int:
        return _OpenProcess(access, inherit, pid)

    def read_proc_times(h: int):
        """Return (kernel, user) FILETIME ticks (100ns); None on failure."""
        ct, et = _FILETIME(), _FILETIME()
        kt, ut = _FILETIME(), _FILETIME()
        if not _GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et),
                                ctypes.byref(kt), ctypes.byref(ut)):
            return None
        return kt.as_int64(), ut.as_int64()

    def close_process(h: int) -> None:
        if h:
            _CloseHandle(h)

    # --- Windows Job Object: kill-on-close so a hard-crashed parent (Python
    # killed before stop()) can never leave orphaned Get-Counter loops behind.
    # SharedMonitor below attaches its single PowerShell child to this job.
    # (Same implementation family as metrics.py / ryzenadj.py — kept here so
    # the merged host is self-contained.)
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFO = 9  # JobObjectExtendedLimitInformation

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_void_p),
            ("MaximumWorkingSetSize", ctypes.c_void_p),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_void_p),
            ("JobMemoryLimit", ctypes.c_void_p),
            ("PeakProcessMemoryUsed", ctypes.c_void_p),
            ("PeakJobMemoryUsed", ctypes.c_void_p),
        ]

    _CreateJobObjectW = _kernel32.CreateJobObjectW
    _CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    _CreateJobObjectW.restype = ctypes.c_void_p

    _SetInformationJobObject = _kernel32.SetInformationJobObject
    _SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
    _SetInformationJobObject.restype = ctypes.c_int

    _AssignProcessToJobObject = _kernel32.AssignProcessToJobObject
    _AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _AssignProcessToJobObject.restype = ctypes.c_int

    def _make_kill_on_close_job():
        """Create a "kill all members when the last handle closes" Job Object.

        Returns the HANDLE (int) or None on any failure.
        """
        try:
            job = _CreateJobObjectW(None, None)
            if not job:
                return None
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _SetInformationJobObject(
                    job, _JOB_OBJECT_EXTENDED_LIMIT_INFO,
                    ctypes.byref(info), ctypes.sizeof(info)):
                _CloseHandle(job)
                return None
            return job
        except Exception:  # noqa: BLE001
            return None

    def _assign_to_job(job, proc_handle):
        try:
            return bool(_AssignProcessToJobObject(
                ctypes.c_void_p(job), ctypes.c_void_p(proc_handle)))
        except Exception:  # noqa: BLE001
            return False

else:  # pragma: no cover - non-Windows
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    NO_INHERIT = False

    def open_process(access: int, inherit: bool, pid: int):  # type: ignore[no-redef]
        raise NotImplementedError("perf_probe process APIs require Windows")

    def read_proc_times(h: int):  # type: ignore[no-redef]
        raise NotImplementedError("perf_probe process APIs require Windows")

    def close_process(h: int) -> None:  # type: ignore[no-redef]
        raise NotImplementedError("perf_probe process APIs require Windows")

    def _make_kill_on_close_job():  # type: ignore[no-redef]
        return None

    def _assign_to_job(job, proc_handle):  # type: ignore[no-redef]
        return False


# --------------------------------------------------------------------------
# SharedMonitor — one long-lived PowerShell host for ALL in-app sampling
# --------------------------------------------------------------------------
class SharedMonitor:
    """Single long-lived PowerShell sampling host (singleton).

    Merges the GPU / per-core / thermal probes into ONE ``powershell.exe``
    running :data:`COMBINED_PS`, which emits a prefixed line per signal every
    tick (~1 s)::

        G 42.3            <- GPU engine utilization sum (%)
        C 0:12.3|1:45.6   <- per-core processor time ("idx:val|idx:val|...")
        T 58.2            <- hottest ACPI thermal zone (deg C)

    A single reader thread parses the stream and dispatches each channel to
    its subscribers.  Subscriber callbacks run on that reader thread, exactly
    like the per-probe reader threads they replace — consumers keep their
    existing locking / Qt-thread handoff patterns.

    Lifecycle / safety design:
      * Refcounted: the PowerShell process lives while at least one channel
        has a subscriber.  The last ``unsubscribe`` terminates it, so closing
        any one consumer can never kill another consumer's data.
      * Idempotent ``subscribe`` (same callback on same channel added once).
      * Kill-on-close Job Object + ``atexit`` cover hard crashes (Python killed
        before ``stop()``), so no Get-Counter orphans can accumulate.
      * EOF (PowerShell died) with subscribers remaining -> auto-restart with
        a 3 s backoff; a running app heals itself instead of pinning 0 values.
      * Non-Windows: all methods become no-ops (``start`` never spawns).
    """

    _instance = None
    _instance_lock = threading.Lock()

    CHANNELS = ("gpu", "cores", "temp")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, list] = {ch: [] for ch in self.CHANNELS}
        self._proc = None
        self._running = False
        self._job = None
        self._job_unavailable = False
        self._restart_at = 0.0
        self._last_err = ""
        self._atexit_registered = False

    @classmethod
    def instance(cls) -> "SharedMonitor":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # -- introspection ------------------------------------------------------
    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def last_err(self) -> str:
        with self._lock:
            return self._last_err

    # -- subscription API ---------------------------------------------------
    def subscribe(self, channel: str, cb) -> None:
        """Register a callback for a channel; starts the host if idle.

        ``cb(value)`` is invoked from the reader thread on every fresh sample
        for that channel (``gpu`` -> float, ``cores`` -> dict[str, float],
        ``temp`` -> float).  Callback exceptions are isolated.
        """
        if sys.platform != "win32":
            return
        with self._lock:
            if channel not in self._subs:
                return
            if cb not in self._subs[channel]:
                self._subs[channel].append(cb)
        self.start()

    def unsubscribe(self, channel: str, cb) -> None:
        """Remove a callback; stops the host when the last one leaves."""
        if sys.platform != "win32":
            return
        with self._lock:
            if channel in self._subs and cb in self._subs[channel]:
                self._subs[channel].remove(cb)
            empty = not any(self._subs.values())
        if empty:
            self.stop()

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Spawn the merged PowerShell monitor. Idempotent + backoff-aware."""
        if sys.platform != "win32":
            return
        with self._lock:
            if self._running:
                return
            if time.monotonic() < self._restart_at:
                return  # inside restart backoff window
            try:
                proc, err = start_powershell(COMBINED_PS, capture_stderr=True,
                                             errors="replace")
            except Exception as e:  # noqa: BLE001
                proc, err = None, repr(e)
            if proc is None:
                self._restart_at = time.monotonic() + 5.0
                self._last_err = f"host spawn failed: {err}"
                return
            self._proc = proc
            self._running = True
            self._last_err = ""
            self._attach_kill_on_close(proc)
            self._register_atexit()
            threading.Thread(target=self._reader, args=(proc,),
                             daemon=True, name="shared-monitor").start()
            threading.Thread(target=self._stderr_reader, args=(proc,),
                             daemon=True, name="shared-monitor-stderr").start()

    def stop(self) -> None:
        """Terminate the PowerShell host (and its Job Object). Safe to repeat."""
        with self._lock:
            self._running = False
            proc = self._proc
            self._proc = None
            job = self._job
            self._job = None
        if job:
            try:
                _CloseHandle(job)
            except OSError:
                pass
        if proc is None:
            return
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=1.0)
        except Exception:  # noqa: BLE001
            pass
        if proc.poll() is None:
            # still alive (incl. conhost subtree) -> force clean
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception:  # noqa: BLE001
                pass

    def _register_atexit(self) -> None:
        if self._atexit_registered:
            return
        self._atexit_registered = True
        try:
            atexit.register(self.stop)
        except Exception:  # noqa: BLE001
            pass

    def _proc_handle(self, proc) -> "int | None":
        h = getattr(proc, "_handle", None)
        if h:
            return int(h)
        try:
            # PROCESS_TERMINATE(0x1) | PROCESS_SET_QUOTA(0x100)
            h2 = _kernel32.OpenProcess(0x0001 | 0x0100, False, int(proc.pid))
            if h2:
                return int(h2)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _attach_kill_on_close(self, proc) -> None:
        if sys.platform != "win32" or self._job_unavailable:
            return
        try:
            if self._job is None:
                job = _make_kill_on_close_job()
                if job is None:
                    self._job_unavailable = True
                    return
                self._job = job
            h = self._proc_handle(proc)
            if h is None or not _assign_to_job(self._job, h):
                # attach failed -> give up Job path, do not retry every spawn
                try:
                    _CloseHandle(self._job)
                except OSError:
                    pass
                self._job = None
                self._job_unavailable = True
        except Exception:  # noqa: BLE001
            self._job_unavailable = True

    # -- reader threads -----------------------------------------------------
    def _reader(self, proc) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._dispatch(line)
        except Exception:  # pragma: no cover
            pass
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                    self._running = False
            # EOF -> if subscribers remain, auto-restart (backoff inside start)
            self._maybe_restart()

    def _stderr_reader(self, proc) -> None:
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                s = line.strip()
                if s:
                    with self._lock:
                        self._last_err = s[:300]
        except Exception:  # pragma: no cover
            pass

    def _maybe_restart(self) -> None:
        with self._lock:
            has_subs = any(self._subs.values())
            now = time.monotonic()
            if not has_subs or now < self._restart_at:
                return
            self._restart_at = now + 3.0
        self.start()

    def _dispatch(self, line: str) -> None:
        s = line.strip()
        if not s:
            return
        ch, _, val = s.partition(" ")
        if ch == "G":
            try:
                v = float(val)
            except ValueError:
                return
            self._emit("gpu", v)
        elif ch == "C":
            d: dict[str, float] = {}
            for tok in val.split("|"):
                if ":" in tok:
                    idx, _, vv = tok.partition(":")
                    try:
                        d[idx] = float(vv)
                    except ValueError:
                        pass
            if d:
                self._emit("cores", d)
        elif ch == "T":
            try:
                v = float(val)
            except ValueError:
                return
            self._emit("temp", v)

    def _emit(self, channel: str, value) -> None:
        with self._lock:
            cbs = list(self._subs.get(channel, ()))
        for cb in cbs:
            try:
                cb(value)
            except Exception:  # pragma: no cover - one bad consumer must not kill others
                pass
