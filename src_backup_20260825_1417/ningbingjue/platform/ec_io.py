"""EC I/O for fan control — faithfully ported from MotionAssistant.

Access methods (in priority order):
  1. **OLS DLL** (``WinRing0x64.dll`` exports) — ``InitializeOls`` +
     ``WriteIoPortByte`` / ``ReadIoPortByte``.  This is exactly what MA's
     ``ECRamWrite4E4F`` calls internally (via the ``Ols`` COM object).  We
     talk to the same C exports directly via ctypes.

  2. **SCM Driver** (``_WinRing0Driver``) — MA's internal ``Driver`` class.
     Loads the kernel driver as SCM service ``WinRing0_1_2_0`` and issues
     port-I/O through ``DeviceIoControl``.  Used as fallback when OLS
     ``GetDllStatus != 0``.

  3. **Standard EC ports** (``ec_write_standard`` / ``ec_read_standard``) —
     MA's ``ECIOWriteByte`` / ``ECIORead`` path.  Uses embedded controller
     command/status registers (0x66/0x62/0x64) with IBF/OBF polling.
     This is what Zotac and some other devices use; it bypasses SIO entirely.

EC register sequences (``ECRamWrite4E4F`` / ``ECRamWrite2E2F``) are copied
**byte-for-byte** from MA decompiled source (``Gyro_FanControl.cs``).
Only the underlying port-I/O primitive is swapped depending on which access
method is active.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import re
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# WinRing0 constants
# ---------------------------------------------------------------------------
SC_MANAGER_ALL_ACCESS = 0xF003F
SERVICE_ALL_ACCESS = 0xF01FF
SERVICE_KERNEL_DRIVER = 1
SERVICE_DEMAND_START = 3
SERVICE_ERROR_NORMAL = 1

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2

INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF

OLS_IOCTL_READ_IO_PORT_BYTE = 0x9C4060CC
OLS_IOCTL_WRITE_IO_PORT_BYTE = 0x9C40A0D8

SERVICE_NAME = "WinRing0_1_2_0"
DEVICE_NAME = "\\\\.\\WinRing0_1_2_0"
EC_MUTEX_NAME = "EcMutex-{B39C3216-FB5D-431C-8F7B-C94EA01AB855}"

# SIO EC window register ports (MA ECRamWrite* reg_addr / reg_data).
REG_4E = 0x4E
REG_4F = 0x4F
REG_2E = 0x2E
REG_2F = 0x2F

# ITE EC index/data window sub-registers written through the SIO ports.
IDX_HI = 0x2E   # 46 — select index step
IDX_LO = 0x2F   # 47 — select data step
DAT_LO = 0x11   # 17 — high address byte target
DAT_HI = 0x10   # 16 — low address byte target
DAT_VAL = 0x12  # 18 — data byte target

# Standard EC ports (MA ECIOWriteByte / ECIORead).
EC_CMD_REG = 0x66    # EC command register
EC_DATA_REG = 0x62   # EC data register
EC_STATUS_REG = 0x64 # EC status register
EC_IBF = 0x02       # Input Buffer Full bit (wait for 0 before write)
EC_OBF = 0x01       # Output Buffer Full bit (wait for 1 before read)

# Module-level state.
_PROTOCOL = "4e4f"
_driver = None
_driver_lock = threading.Lock()
_last_error = ""
_last_error_code = 0
_sys_path_cache = None
_ec_mutex = None

# OLS DLL state (method 1 — direct ctypes calls into WinRing0x64.dll).
_ols_dll = None           # ctypes CDLL handle
_ols_available = False    # True if InitializeOls succeeded
_ols_access_method = ""   # "ols" | "scm" | "standard" — which one is active


# ---------------------------------------------------------------------------
# ctypes helpers
# ---------------------------------------------------------------------------
def _kernel() -> ctypes.CDLL:
    return ctypes.windll.kernel32


def _advapi() -> ctypes.CDLL:
    return ctypes.windll.advapi32


def _is_valid_handle(h) -> bool:
    if h is None or h == 0:
        return False
    return getattr(h, "value", h) != INVALID_HANDLE_VALUE


# ---------------------------------------------------------------------------
# Locate binaries
# ---------------------------------------------------------------------------
def _locate_winring0_dll() -> str:
    """Return path to the vendored WinRing0x64.dll."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "blocks", "ryzenadj-win64", "WinRing0x64.dll"),
        os.path.join(here, "..", "..", "_internal", "blocks", "ryzenadj-win64",
                     "WinRing0x64.dll"),
        os.path.join(getattr(sys, "_MEIPASS", ""), "blocks", "ryzenadj-win64",
                     "WinRing0x64.dll"),
        os.path.join(os.path.dirname(sys.executable), "blocks", "ryzenadj-win64",
                     "WinRing0x64.dll"),
    ]
    cur = here
    for _ in range(5):
        cand = os.path.join(cur, "blocks", "ryzenadj-win64", "WinRing0x64.dll")
        if cand not in candidates:
            candidates.append(cand)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for c in candidates:
        if c and os.path.exists(c):
            return os.path.abspath(c)
    return ""


def _locate_winring0_sys() -> str:
    """Return path to the bundled WinRing0x64.sys (the kernel driver binary)."""
    dll = _locate_winring0_dll()
    if dll:
        sys_path = os.path.join(os.path.dirname(dll), "WinRing0x64.sys")
        if os.path.exists(sys_path):
            return os.path.abspath(sys_path)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "blocks", "ryzenadj-win64", "WinRing0x64.sys"),
        os.path.join(here, "..", "..", "_internal", "blocks", "ryzenadj-win64",
                     "WinRing0x64.sys"),
        os.path.join(getattr(sys, "_MEIPASS", ""), "blocks", "ryzenadj-win64",
                     "WinRing0x64.sys"),
        os.path.join(os.path.dirname(sys.executable), "blocks", "ryzenadj-win64",
                     "WinRing0x64.sys"),
    ]
    ma_dirs = [
        os.path.join(os.environ.get("ProgramFiles", ""), "MotionAssistant"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "MotionAssistant"),
        os.path.join(os.environ.get("ProgramData", ""), "MotionAssistant"),
        os.path.join(os.path.dirname(sys.executable)),
        os.path.join(os.path.dirname(sys.executable), ".."),
    ]
    for d in ma_dirs:
        if d:
            candidates.append(os.path.join(d, "WinRing0x64.sys"))
    cur = here
    for _ in range(5):
        cand = os.path.join(cur, "blocks", "ryzenadj-win64", "WinRing0x64.sys")
        if cand not in candidates:
            candidates.append(cand)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for c in candidates:
        if c and os.path.exists(c):
            return os.path.abspath(c)
    return ""


# ===========================================================================
# Method 1: OLS DLL direct calls (ctypes into WinRing0x64.dll exports)
# ===========================================================================
def _init_ols_dll() -> bool:
    """Try to load WinRing0x64.dll and call InitializeOls.

    Returns True if OLS is usable (GetDllStatus == 0 or 2).
    This is the SAME code path MA uses internally via its ``Ols`` COM object;
    we just call the C exports directly via ctypes.
    """
    global _ols_dll, _ols_available
    if _ols_dll is not None:
        return _ols_available
    _ols_available = False
    dll_path = _locate_winring0_dll()
    if not dll_path or not os.path.exists(dll_path):
        return False
    try:
        _ols_dll = ctypes.CDLL(dll_path)
        # Set up function signatures (stdcall convention for WinRing0).
        _ols_dll.InitializeOls.restype = ctypes.c_bool
        _ols_dll.InitializeOls.argtypes = []
        _ols_dll.GetDllStatus.restype = ctypes.c_uint
        _ols_dll.GetDllStatus.argtypes = []
        _ols_dll.WriteIoPortByte.restype = ctypes.c_bool
        _ols_dll.WriteIoPortByte.argtypes = [ctypes.c_uint16, ctypes.c_uint8]
        _ols_dll.ReadIoPortByte.restype = ctypes.c_bool
        _ols_dll.ReadIoPortByte.argtypes = [ctypes.c_uint16,
                                              ctypes.POINTER(ctypes.c_uint8)]
    except Exception:
        _ols_dll = None
        return False
    try:
        ok = _ols_dll.InitializeOls()
        if not ok:
            return False
        status = _ols_dll.GetDllStatus()
        # 0 = OK, 2 = Already loaded (both fine)
        if status in (0, 2):
            _ols_available = True
            return True
        # 3 = unsupported platform, etc. — clean up
        _ols_available = False
        return False
    except Exception:
        _ols_available = False
        return False


def _ols_write_port(port: int, value: int) -> bool:
    """Write a byte via OLS DLL (Method 1)."""
    if not (_ols_dll and _ols_available):
        return False
    try:
        return bool(_ols_dll.WriteIoPortByte(port & 0xFFFF, value & 0xFF))
    except Exception:
        return False


def _ols_read_port(port: int):
    """Read a byte via OLS DLL (Method 1)."""
    if not (_ols_dll and _ols_available):
        return None
    try:
        val = ctypes.c_uint8(0)
        if _ols_dll.ReadIoPortByte(port & 0xFFFF, ctypes.byref(val)):
            return val.value
        return None
    except Exception:
        return None


# ===========================================================================
# Method 2: SCM Driver (MA's Driver class, DeviceIoControl)
# ===========================================================================
class _WinRing0Driver:
    """Loads WinRing0x64.sys as SCM service ``WinRing0_1_2_0``, issues
    port-I/O through ``DeviceIoControl``.  Verbatim from MA."""

    def __init__(self, sys_path: str) -> None:
        self.sys_path = sys_path
        self.handle = None
        self._install_argtypes()

    @staticmethod
    def _install_argtypes() -> None:
        k = _kernel()
        a = _advapi()
        k.CreateFileW.restype = ctypes.c_void_p
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.DeviceIoControl.restype = wintypes.BOOL
        k.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        a.OpenSCManagerW.restype = wintypes.HANDLE
        a.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        a.CreateServiceW.restype = wintypes.HANDLE
        a.CreateServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                     ctypes.c_void_p, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                     wintypes.LPCWSTR]
        a.OpenServiceW.restype = wintypes.HANDLE
        a.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
        a.StartServiceW.restype = wintypes.BOOL
        a.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPCWSTR]
        a.CloseServiceHandle.restype = wintypes.BOOL
        a.CloseServiceHandle.argtypes = [wintypes.HANDLE]

    def open(self) -> bool:
        h = _kernel().CreateFileW(
            DEVICE_NAME, GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
        if not _is_valid_handle(h):
            return False
        self.handle = h
        return True

    def install(self) -> bool:
        a = _advapi()
        scm = a.OpenSCManagerW(None, None, SC_MANAGER_ALL_ACCESS)
        if not _is_valid_handle(scm):
            return False
        try:
            svc = a.CreateServiceW(
                scm, SERVICE_NAME, SERVICE_NAME, SERVICE_ALL_ACCESS,
                SERVICE_KERNEL_DRIVER, SERVICE_DEMAND_START, SERVICE_ERROR_NORMAL,
                self.sys_path, None, None, None, None, None)
            if not _is_valid_handle(svc):
                svc = a.OpenServiceW(scm, SERVICE_NAME, SERVICE_ALL_ACCESS)
            if not _is_valid_handle(svc):
                return False
            try:
                a.StartServiceW(svc, 0, None)
            finally:
                a.CloseServiceHandle(svc)
        finally:
            a.CloseServiceHandle(scm)
        return self.open()

    def io_control(self, ioctl: int, in_buf, in_len: int,
                   out_buf, out_len: int) -> bool:
        if not self.handle:
            return False
        returned = wintypes.DWORD(0)
        ok = _kernel().DeviceIoControl(
            self.handle, ioctl, in_buf, in_len, out_buf, out_len,
            ctypes.byref(returned), None)
        return ok != 0

    def write_io_port_byte(self, port: int, value: int) -> bool:
        class _In(ctypes.Structure):
            _pack_ = 1
            _fields_ = [("Port", ctypes.c_uint32), ("Value", ctypes.c_uint8)]
        inp = _In(Port=ctypes.c_uint32(port & 0xFFFF),
                  Value=ctypes.c_uint8(value & 0xFF))
        return self.io_control(OLS_IOCTL_WRITE_IO_PORT_BYTE,
                               ctypes.byref(inp), ctypes.sizeof(inp), None, 0)

    def read_io_port_byte(self, port: int):
        inport = ctypes.c_uint32(port & 0xFFFF)
        out = ctypes.c_uint32(0)
        ok = self.io_control(OLS_IOCTL_READ_IO_PORT_BYTE,
                              ctypes.byref(inport), 4, ctypes.byref(out), 4)
        return (out.value & 0xFF) if ok else None

    @property
    def is_open(self) -> bool:
        return _is_valid_handle(self.handle)

    def close(self) -> None:
        if self.handle:
            _kernel().CloseHandle(self.handle)
            self.handle = None


def _ensure_driver() -> bool:
    global _driver, _last_error, _last_error_code, _sys_path_cache
    if _driver is not None:
        return _driver.is_open
    with _driver_lock:
        if _driver is not None:
            return _driver.is_open
        _last_error = ""
        _last_error_code = 0
        sys_path = _locate_winring0_sys()
        _sys_path_cache = sys_path
        if not sys_path or not os.path.exists(sys_path):
            _last_error = "WinRing0x64.sys 未找到（打包缺失）"
            return False
        drv = _WinRing0Driver(sys_path)
        if drv.open():
            _driver = drv
            return True
        open_err = ctypes.GetLastError()
        if drv.install():
            _driver = drv
            return True
        install_err = ctypes.GetLastError()
        code = install_err or open_err or 0
        _last_error_code = code
        if code == 5:
            _last_error = ("需要管理员权限：请右键 exe「以管理员身份运行」"
                           "（或在管理员终端启动 python run.py）")
        elif code in (577, 1168, 0x237):
            _last_error = ("驱动被系统拒绝加载（错误码 %d）：多半是 Windows"
                           "「内核隔离 / 内存完整性(HVCI)」拦截了驱动。" % code)
        elif code in (1072, 1073):
            _last_error = (f"WinRing0 服务已存在但无法启动（错误码 {code}）")
        else:
            _last_error = (f"WinRing0 驱动加载失败（open_err={open_err}，"
                           f"install_err={install_err}）")
        return False


# ===========================================================================
# Method 3: Standard EC ports (MA's ECIOWriteByte / ECIORead)
# ===========================================================================
def _scm_port_write(port: int, value: int) -> bool:
    """Write via SCM driver (Method 2)."""
    drv = _driver
    if drv is None or not drv.is_open:
        return False
    return drv.write_io_port_byte(port, value)


def _scm_port_read(port: int):
    """Read via SCM driver (Method 2)."""
    drv = _driver
    if drv is None or not drv.is_open:
        return None
    return drv.read_io_port_byte(port)


def _ec_poll_ibf(timeout_ms: int = 200) -> bool:
    """Poll EC status port until IBF (Input Buffer Full) clears."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        st = _port_read(EC_STATUS_REG)
        if st is not None and (st & EC_IBF) == 0:
            return True
        time.sleep(0.001)
    return False


def _ec_poll_obf(timeout_ms: int = 200) -> bool:
    """Poll EC status port until OBF (Output Buffer Full) sets."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        st = _port_read(EC_STATUS_REG)
        if st is not None and (st & EC_OBF) != 0:
            return True
        time.sleep(0.001)
    return False


def ec_write_standard(reg: int, value: int) -> bool:
    """MA's ``ECIOWriteByte`` / ``TryWriteByte`` — standard EC port write.

    Sequence (from Gyro_FanControl.cs:1650-1663):
      WaitWrite → Write(0x66, 0x81) → WaitWrite → Write(0x62, reg)
      → WaitWrite → Write(0x62, value)

    Uses EC command register 0x66 and data register 0x62 with IBF polling.
    This is the method Zotac_G0A1W uses; it bypasses SIO entirely and talks
    to the embedded controller's command interface directly.
    """
    if not _ensure_driver():
        return False
    # 0x81 = WR_BYTE command
    if not (_ec_poll_ibf() and _scm_port_write(EC_CMD_REG, 0x81)):
        return False
    if not (_ec_poll_ibf() and _scm_port_write(EC_DATA_REG, reg & 0xFF)):
        return False
    if not (_ec_poll_ibf() and _scm_port_write(EC_DATA_REG, value & 0xFF)):
        return False
    return True


def ec_read_standard(reg: int):
    """MA's ``ECIORead`` / ``TryReadByte`` — standard EC port read.

    Sequence (from Gyro_FanControl.cs:1633-1647):
      WaitWrite → Write(0x66, 0x80) → WaitWrite → Write(0x62, reg)
      → WaitRead → Read(0x62)
    """
    if not _ensure_driver():
        return None
    # 0x80 = RD_BYTE command
    if not (_ec_poll_ibf() and _scm_port_write(EC_CMD_REG, 0x80)):
        return None
    if not (_ec_poll_ibf() and _scm_port_write(EC_DATA_REG, reg & 0xFF)):
        return None
    if not _ec_poll_obf():
        return None
    return _scm_port_read(EC_DATA_REG)


# ===========================================================================
# EC mutex (MA's EcMutex — coordinate with running MA instance)
# ===========================================================================
def _ensure_ec_mutex():
    global _ec_mutex
    if _ec_mutex is not None:
        return _ec_mutex
    try:
        h = ctypes.windll.kernel32.CreateMutexW(None, 0, EC_MUTEX_NAME)
        if _is_valid_handle(h):
            _ec_mutex = h
    except Exception:
        _ec_mutex = None
    return _ec_mutex


def _ec_locked():
    """Context manager: acquire MA's named EC mutex (2s timeout)."""
    class _Ctx:
        def __enter__(self):
            self.m = _ensure_ec_mutex()
            self.ok = False
            if self.m is not None:
                try:
                    rc = ctypes.windll.kernel32.WaitForSingleObject(self.m, 2000)
                    self.ok = rc in (0, 0x80)
                except Exception:
                    self.ok = False
            return self

        def __exit__(self, *exc):
            if self.m is not None and self.ok:
                try:
                    ctypes.windll.kernel32.ReleaseMutex(self.m)
                except Exception:
                    pass
            return False
    return _Ctx()


# ===========================================================================
# Port I/O dispatch — picks the best available method
# ===========================================================================
def _detect_best_access() -> str:
    """Probe available methods and return the best one.

    Priority: ols > scm > (standard is separate, not default for ECRam).
    Sets global ``_ols_access_method``.
    """
    global _ols_access_method
    # Try OLS first (identical to MA's Ols COM object path)
    if _init_ols_dll():
        _ols_access_method = "ols"
        return "ols"
    # Fall back to SCM driver
    if _ensure_driver():
        _ols_access_method = "scm"
        return "scm"
    _ols_access_method = ""
    return ""


def _port_write(port: int, value: int) -> bool:
    """Write a byte to an I/O port using the best available method."""
    if _ols_access_method == "ols":
        return _ols_write_port(port, value)
    # Default: SCM driver
    return _scm_port_write(port, value)


def _port_read(port: int):
    """Read a byte from an I/O port using the best available method."""
    if _ols_access_method == "ols":
        return _ols_read_port(port)
    return _scm_port_read(port)


# ===========================================================================
# EC RAM register sequences — verbatim from MA ECRamWrite* / Read*
# ===========================================================================
def _ecram_write(base_addr: int, base_data: int, address: int, data: int) -> bool:
    """MA ECRamWrite* sequence, byte-for-byte from Gyro_FanControl.cs:1682-1717.

    The only difference between 4E4F and 2E2F protocols is the SIO base ports
    (0x4E/0x4F vs 0x2E/0x2F); the sub-register selects (0x11/0x10/0x12)
    are identical.
    """
    value = (address >> 8) & 0xFF
    value2 = address & 0xFF
    steps = [
        (base_addr, IDX_HI),
        (base_data, DAT_LO),
        (base_addr, IDX_LO),
        (base_data, value),
        (base_addr, IDX_HI),
        (base_data, DAT_HI),
        (base_addr, IDX_LO),
        (base_data, value2),
        (base_addr, IDX_HI),
        (base_data, DAT_VAL),
        (base_addr, IDX_LO),
        (base_data, data),
    ]
    with _ec_locked():
        for port, val in steps:
            if not _port_write(port, val):
                return False
    return True


def _ecram_read(base_addr: int, base_data: int, address: int):
    """MA ECRamRead* sequence, byte-for-byte from Gyro_FanControl.cs:1720-1756."""
    value = (address >> 8) & 0xFF
    value2 = address & 0xFF
    steps = [
        (base_addr, IDX_HI),
        (base_data, DAT_LO),
        (base_addr, IDX_LO),
        (base_data, value),
        (base_addr, IDX_HI),
        (base_data, DAT_HI),
        (base_addr, IDX_LO),
        (base_data, value2),
        (base_addr, IDX_HI),
        (base_data, DAT_VAL),
        (base_addr, IDX_LO),
    ]
    with _ec_locked():
        for port, val in steps:
            if not _port_write(port, val):
                return None
        return _port_read(base_data)


def ECRamWrite4E4F(address: int, data: int) -> bool:
    return _ecram_write(REG_4E, REG_4F, address, data)


def ECRamWrite2E2F(address: int, data: int) -> bool:
    return _ecram_write(REG_2E, REG_2F, address, data)


def ECRamRead4E4F(address: int):
    return _ecram_read(REG_4E, REG_4F, address)


def ECRamRead2E2F(address: int):
    return _ecram_read(REG_2E, REG_2F, address)


# ===========================================================================
# Public API — used by blocks/fan.py
# ===========================================================================
def set_ec_protocol(protocol: str) -> None:
    """Select EC register protocol: "4e4f" (default) or "2e2f" (Win4)."""
    global _PROTOCOL
    p = (protocol or "4e4f").lower()
    _PROTOCOL = "2e2f" if p.startswith("2") else "4e4f"


def write_ec(addr: int, value: int) -> bool:
    """Write a byte to an EC RAM address via current protocol."""
    global _last_error
    # Ensure we have an access method (auto-detect on first call)
    if not _ols_access_method:
        _detect_best_access()
    if _ols_access_method == "" and not _ensure_driver():
        _last_error = last_error() or "无可用访问方法（驱动未加载）"
        return False
    if _PROTOCOL == "2e2f":
        ok = ECRamWrite2E2F(addr & 0xFFFF, value & 0xFF)
    else:
        ok = ECRamWrite4E4F(addr & 0xFFFF, value & 0xFF)
    if not ok:
        _last_error = "EC 写入被跳过（驱动繁忙或互斥超时，请稍后重试）"
    return ok


def read_ec(addr: int):
    """Read a byte from an EC RAM address via current protocol."""
    if not _ols_access_method:
        _detect_best_access()
    if _ols_access_method == "" and not _ensure_driver():
        return None
    if _PROTOCOL == "2e2f":
        return ECRamRead2E2F(addr & 0xFFFF)
    return ECRamRead4E4F(addr & 0xFFFF)


def health_check() -> bool:
    """True if any EC access method is working."""
    if _ols_access_method == "ols":
        return True
    return _ensure_driver()


def is_admin() -> bool:
    """True if the current process is elevated."""
    try:
        a = _advapi()
        a.OpenProcessToken.restype = wintypes.BOOL
        a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.HANDLE)]
        a.GetTokenInformation.restype = wintypes.BOOL
        a.GetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
        _kernel().CloseHandle.restype = wintypes.BOOL
        _kernel().CloseHandle.argtypes = [wintypes.HANDLE]
        TOKEN_QUERY = 0x8
        TokenElevation = 20
        token = wintypes.HANDLE()
        if not a.OpenProcessToken(_kernel().GetCurrentProcess(), TOKEN_QUERY,
                                   ctypes.byref(token)):
            return False
        elevation = wintypes.DWORD()
        retlen = wintypes.DWORD()
        try:
            ok = a.GetTokenInformation(token, TokenElevation,
                                       ctypes.byref(elevation),
                                       ctypes.sizeof(elevation),
                                       ctypes.byref(retlen))
            return bool(ok and elevation.value != 0)
        finally:
            _kernel().CloseHandle(token)
    except Exception:
        return False


def last_error() -> str:
    return _last_error


def diag_info() -> dict:
    open_ok = _driver is not None and _driver.is_open
    return {
        "is_admin": is_admin(),
        "getdllstatus": None,
        "method": _ols_access_method or ("scm" if open_ok else ""),
        "dll_path": _sys_path_cache or _locate_winring0_sys(),
        "last_error": _last_error if not (open_ok or _ols_available) else "",
        "last_error_code": _last_error_code,
        "ols_available": _ols_available,
    }


# ===========================================================================
# Device auto-detection (MA-style: map system model → protocol + registers)
# ===========================================================================
# GPD ships MA per-device; each model hardcodes its SIO protocol and EC
# register addresses.  We consolidate those mappings into one table and match
# the runtime model string against it.
#
# Tuple: (match_substring_lowercase, board_quirk, protocol, enable_addr,
#          pwm_addr, rpm_addr)
#   - enable_addr == 0 → no separate "manual enable" register (write PWM direct)
#   - protocol      → "4e4f" (SIO ports 0x4E/0x4F) or "2e2f" (0x2E/0x2F)
#   - rpm_addr      → EC RAM address of the fan tachometer LOW byte (16-bit
#                     little-endian RPM read from rpm_addr/rpm_addr+1).  On ITE
#                     ECs the tach usually sits right after the PWM register, so
#                     our defaults are pwm_addr + 1.  If a device reports garbage
#                     RPM the user can override ec_rpm_addr in the fan edit panel.
FAN_DEVICE_MAP: list[tuple] = [
    ("win max 2", "wm2", "4e4f", 629, 6153, 6154),
    ("win mini", "win_mini", "4e4f", 0, 1146, 1147),
    ("win 4", "win4", "2e2f", 0, 49937, 49938),
    ("duo", "duo", "4e4f", 0, 1147, 1148),
]

# Normalized alias tokens per device (lowercase, alnum-only).
#
# 关键事实（根因）：GPD 的真实 WMI/DMI 机型字符串不是人读名（"GPD Win Mini"
# / "GPD Win Max 2"），而是硬件板号（SMBIOS Type 1 Product Name，即注册表
# HKLM\...\SystemInformation\SystemProductName）。例如 Win Mini HX370 的机型
# 串是 "G1617-02"，里面根本没有 "winmini" / "winmax" 这些词。旧实现只匹配
# 人读名，于是检测永远落空、回退到默认 wm2 —— 这正是「Win Mini 每次重装都被
# 自动识别成 WM2」的根因。
#
# 板号对照（来源：Linux 主线 hwmon 驱动 drivers/hwmon/gpd-fan.c 的 dmi_table）：
#   Win Mini   (7640U/8640U/8840U) = G1617-01
#   Win Mini   (HX370)             = G1617-02 / G1617-02-L
#   Win 4      (6800U/7840U)       = G1618-04
#   Win Max 2  (6800U~8840U)       = G1619-04
#   Win Max 2  (HX370)             = G1619-05
#   Duo                            = G1622-01 / G1622-01-L
#   Pocket 4                       = G1628-04 / G1628-04-L（复用 win_mini 寄存器）
# 人读名 token 保留作兜底（个别早期/特殊固件可能真的上报 "GPD WinMini"）。
#
# 注意 Win 4 的歧义：G1618-04 同时覆盖 6800U（SIO 2E2F）与 7840U（复用 wm2
# 的 4E4F 寄存器）。当前无基板版本字段无法区分，统一按 6800U→win4 处理；
# 7840U 用户可在编辑面板手动锁定 wm2。
_DEVICE_ALIASES: dict[str, list[str]] = {
    "wm2":      ["winmax2", "winmax", "gpdwinmax2",
                 "g161904", "g161905"],
    "win_mini": ["winmini", "gpdwinmini",
                 "g161701", "g161702", "g161702l",
                 "g162804", "g162804l"],
    "win4":     ["win4", "gpdwin4", "win4g", "g161804"],
    "duo":      ["duo", "gpdduo", "g162201", "g162201l"],
}


def _norm_model(s: str) -> str:
    """Normalize a model string for matching: lowercase, drop non-alnum."""
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _read_system_model_registry() -> str:
    """Fast path: read the SMBIOS system model straight from the registry.

    Zero subprocess, microsecond latency, and reliable even while the app is
    still booting (a PowerShell cold-start can time out when the startup sampler
    PowerShells are all competing).  Returns ``""`` on any failure (non-Windows,
    key missing, permission).
    """
    try:
        import winreg
    except Exception:  # noqa: BLE001
        return ""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\SystemInformation")
        try:
            parts: list[str] = []
            for name in ("SystemManufacturer", "SystemProductName"):
                try:
                    val, _ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                if val:
                    parts.append(str(val))
            return " ".join(parts)
        finally:
            winreg.CloseKey(key)
    except Exception:  # noqa: BLE001
        return ""


def _read_system_model_powershell() -> str:
    """Fallback: WMI via one-shot PowerShell (only when the registry key above
    is missing / corrupted)."""
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$cs=Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue;"
        "$bb=Get-CimInstance Win32_BaseBoard -ErrorAction SilentlyContinue;"
        "$si=Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\SystemInformation' -ErrorAction SilentlyContinue;"
        "$m=@();"
        "if($cs){$m+=[string]$cs.Manufacturer;$m+=[string]$cs.Model};"
        "if($bb){$m+=[string]$bb.Product};"
        "if($si){if($si.SystemManufacturer){$m+=[string]$si.SystemManufacturer};"
        "if($si.SystemProductName){$m+=[string]$si.SystemProductName}};"
        "[Console]::Out.WriteLine(($m -join ' '))"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return (proc.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def read_system_model() -> str:
    """Read the machine model string (registry fast path → PowerShell fallback).

    Returns the SMBIOS ``Manufacturer + ProductName`` (or the fuller WMI
    concatenation via the fallback), or ``""`` on total failure.
    """
    if sys.platform != "win32":
        return ""
    model = _read_system_model_registry()
    if model:
        return model
    return _read_system_model_powershell()


def detect_device() -> dict:
    """Auto-detect the GPD device and return its EC register mapping.

    Returns a dict (always present, never raises):
        {
          "matched": bool,       # True if the model matched a known mapping
          "model": str,          # raw WMI model string (diagnostic)
          "board_quirk": str,    # "wm2" / "win_mini" / "win4" / "duo"
          "protocol": str,       # "4e4f" / "2e2f"
          "enable_addr": int,    # 0 = no separate enable register
          "pwm_addr": int,       # EC RAM address for fan speed (PWM)
          "rpm_addr": int,       # EC RAM address for fan tachometer low byte
        }
    """
    result: dict = {
        "matched": False,
        "model": "",
        "board_quirk": "wm2",
        "protocol": "4e4f",
        "enable_addr": 629,
        "pwm_addr": 6153,
        "rpm_addr": 6154,
    }
    model = read_system_model()
    if not model:
        return result
    result["model"] = model
    norm = _norm_model(model)
    for substr, quirk, proto, enable, pwm, rpm in FAN_DEVICE_MAP:
        # Match on the (space-stripped) display substring AND on the normalized
        # alias tokens, so both "GPD Win Max 2" and "GPD WinMax2" hit WM2.
        tokens = [substr.replace(" ", "")] + list(_DEVICE_ALIASES.get(quirk, []))
        if any(tok and tok in norm for tok in tokens):
            result.update(
                matched=True,
                board_quirk=quirk,
                protocol=proto,
                enable_addr=int(enable),
                pwm_addr=int(pwm),
                rpm_addr=int(rpm),
            )
            break
    return result


def read_fan_rpm(rpm_addr: int) -> int | None:
    """Read the current fan RPM from the EC tachometer (16-bit LE at
    ``rpm_addr`` / ``rpm_addr+1``).

    Returns an int RPM, or ``None`` when the address is 0 / EC is unavailable /
    the readback is implausible (<=0 or > 30000).  Never raises.
    """
    if not rpm_addr:
        return None
    if not health_check():
        return None
    try:
        lo = read_ec(rpm_addr)
        hi = read_ec(rpm_addr + 1)
    except Exception:  # noqa: BLE001
        return None
    if lo is None or hi is None:
        return None
    rpm = (hi << 8) | lo
    if rpm <= 0 or rpm > 30000:
        return None
    return rpm


# ===========================================================================
# Self-test + brute-force fan probe
# ===========================================================================
def self_test() -> dict:
    """Comprehensive diagnostic: transport + register probe + write verify."""
    result = {
        "is_admin": is_admin(),
        "sys_path": _locate_winring0_sys(),
        "dll_path": _locate_winring0_dll(),
        "driver_open": False,
        "error_code": 0,
        "error_msg": "",
        "ec_readback": None,
        "fan_probe": {},
        "ols_status": "",
        "access_method": "",
    }

    # Detect best access method
    method = _detect_best_access()
    result["access_method"] = method
    result["ols_status"] = ("可用" if _ols_available
                            else ("DLL不存在" if not _ols_dll
                                  else f"GetDllStatus={_get_ols_status()}"))

    if method == "ols":
        result["driver_open"] = True  # OLS implies driver is loaded
    elif method == "scm":
        result["driver_open"] = True
    else:
        result["error_msg"] = _last_error
        result["error_code"] = _last_error_code
        return result

    # Basic transport test: read reg 629
    try:
        rb = read_ec(629)
        result["ec_readback"] = rb
    except Exception as exc:
        result["ec_readback"] = "read_error:" + str(exc)

    # Fan register probe (all known MA addresses, both protocols)
    probe_addrs = [
        (629,   "reg_629_WM2_enable",   "WM2"),
        (6153,  "reg_6153_WM2_speed",   "WM2"),
        (1146,  "reg_1146_WinMini_speed","WinMini/Duo"),
        (1147,  "reg_1147_Duo_speed2",  "Duo"),
        (49937, "reg_49937_Win4_speed",  "Win4"),
    ]
    saved_proto = _PROTOCOL
    for proto_name, ram_write, ram_read in [
        ("4e4f", ECRamWrite4E4F, ECRamRead4E4F),
        ("2e2f", ECRamWrite2E2F, ECRamRead2E2F),
    ]:
        pdata = {}
        for addr, label, device in probe_addrs:
            try:
                val = ram_read(addr)
                pdata[label] = {"value": val, "device": device,
                                "hex": f"0x{addr:X}"}
            except Exception as exc:
                pdata[label] = {"value": None, "error": str(exc)}
        result["fan_probe"][proto_name] = pdata

    # Write+readback tests
    for test_name, addr, proto_fn_w, proto_fn_r in [
        ("wm2_write_test", 6153, ECRamWrite4E4F, ECRamRead4E4F),
        ("winmini_write_test", 1146, ECRamWrite4E4F, ECRamRead4E4F),
        ("win4_write_test", 49937, ECRamWrite2E2F, ECRamRead2E2F),
    ]:
        try:
            original = proto_fn_r(addr)
            test_val = 0xAA + list(result.keys()).index(test_name)
            if proto_fn_w(addr, test_val):
                verify = proto_fn_r(addr)
                proto_fn_w(addr, original if original is not None else 0)
                result[test_name] = {
                    "original": original, "wrote": test_val,
                    "readback": verify, "match": verify == test_val,
                }
            else:
                result[test_name] = {"error": f"{proto_fn_w.__name__}({addr}) returned False"}
        except Exception as exc:
            result[test_name] = {"error": str(exc)}

    # Standard EC port test (Method 3)
    try:
        orig_74 = ec_read_standard(0x74)
        ec_write_standard(0x74, 0x01)
        rb74 = ec_read_standard(0x74)
        ec_write_standard(0x74, orig_74 if orig_74 is not None else 0)
        result["standard_ec_test"] = {
            "reg_74_original": orig_74, "reg_74_after_write": rb74,
            "write_ok": rb74 == 1,
        }
    except Exception as exc:
        result["standard_ec_test"] = {"error": str(exc)}

    set_ec_protocol(saved_proto)
    return result


def _get_ols_status() -> int:
    if _ols_dll is None:
        return -1
    try:
        return int(_ols_dll.GetDllStatus())
    except Exception:
        return -1


def brute_force_fan_test() -> dict:
    """Brute-force fan control test: try EVERY known method and report results.

    This is the diagnostic of last resort when normal writes don't spin the fan.
    For each method, we write a distinctive PWM value and ask the user whether
    the fan speed actually changed.

    Returns a dict describing each attempt; the UI should present this to the
    user and let them confirm which method worked.
    """
    # Ensure we have at least one working access method
    _detect_best_access()
    results = {"methods": [], "note": ""}

    # Collect all methods to try
    attempts = []

    # --- Method A: WM2 via ECRamWrite4E4F (current default) ---
    attempts.append(("A:WM2 ECRam4E4F(629=1,6153=180)",
                      lambda: (ECRamWrite4E4F(629, 1) and ECRamWrite4E4F(6153, 180)),
                      lambda: ECRamWrite4E4F(629, 0)))

    # --- Method B: WinMini via ECRamWrite4E4F(1146) ---
    attempts.append(("B:WinMini ECRam4E4F(1146=200)",
                      lambda: ECRamWrite4E4F(1146, 200),
                      lambda: ECRamWrite4E4F(1146, 0)))

    # --- Method C: Win4 via ECRamWrite2E2F(49937) ---
    attempts.append(("C:Win4 ECRam2E2F(49937=200)",
                      lambda: ECRamWrite2E2F(49937, 200),
                      lambda: ECRamWrite2E2F(49937, 0)))

    # --- Method D: Standard EC port (like Zotac) ---
    attempts.append(("D:StandardEC(74=1,75=180)",
                      lambda: (ec_write_standard(0x74, 1) and ec_write_standard(0x75, 180)),
                      lambda: (ec_write_standard(0x74, 0) and ec_write_standard(0x75, 0))))

    # --- Method E: Standard EC port with different registers ---
    attempts.append(("E:StandardEC(64=1,65=180)",
                      lambda: (ec_write_standard(0x64, 1) and ec_write_standard(0x65, 180)),
                      lambda: (ec_write_standard(0x64, 0) and ec_write_standard(0x65, 0))))

    for label, do_it, undo_it in attempts:
        entry = {"label": label, "write_ok": False, "error": None}
        try:
            ok = do_it()
            entry["write_ok"] = bool(ok)
            if not ok:
                entry["error"] = "write returned False"
        except Exception as exc:
            entry["error"] = str(exc)
        results["methods"].append(entry)
        # Don't undo yet — let user observe fan speed change
        # The UI will tell user to check fan, then click OK to restore

    results["note"] = ("已依次尝试上述每种方法写入风扇寄存器。"
                       "请观察风扇转速是否有变化，然后点确定恢复自动控制。")
    return results


def brute_force_restore() -> None:
    """Restore BIOS auto control after brute force test."""
    try:
        ECRamWrite4E4F(629, 0)          # WM2 disable manual
        ECRamWrite4E4F(1146, 0)         # WinMini auto
        ECRamWrite2E2F(49937, 0)        # Win4 auto
        ec_write_standard(0x74, 0)       # Standard EC restore
        ec_write_standard(0x75, 0)
    except Exception:
        pass


def shutdown() -> None:
    """Clean up all resources."""
    global _driver, _ols_dll, _ols_available, _last_error
    with _driver_lock:
        if _driver is not None:
            try:
                _driver.close()
            except Exception:
                pass
        _driver = None
    if _ols_dll is not None:
        try:
            # No explicit cleanup export in WinRing0.dll; just drop reference
            pass
        except Exception:
            pass
        _ols_dll = None
    _ols_available = False
    _last_error = ""
