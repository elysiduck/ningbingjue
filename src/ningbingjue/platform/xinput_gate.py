"""platform/xinput_gate.py — 手柄隔离管理器（双模式）

凝冰结魄窗口获得焦点时，让目标游戏进程读不到手柄状态。两种模式：

**DLL 劫持模式**（单机游戏，零反作弊风险）
  fake xinput1_3.dll 部署到游戏目录，通过命名事件 Local\\NingBingJueGamepadGate
  通知 fake DLL 是否拦截 XInputGetState。凝冰结魄前台 → fake DLL 返回
  ERROR_DEVICE_NOT_CONNECTED；失焦 → 透传真实状态。凝冰结魄自身用 SDL2
  直接读物理手柄，不受影响。

**设备禁用模式**（反作弊游戏，零反作弊风险）
  凝冰结魄前台时用 SetupAPI 禁用物理手柄 HID 设备 → 游戏读不到手柄；
  失焦时重新启用。凝冰结魄自身的 SDL2 句柄也会失效，HUD 模式需用键盘操作。
  需要管理员权限。完全不碰游戏进程，反作弊无法检测。

fallback：命名事件打开失败（凝冰结魄未启动）→ fake DLL 透传 → 游戏正常工作。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from ctypes import wintypes
from typing import Optional


# ---- 命名事件 ----

GATE_EVENT_NAME = "Local\\NingBingJueGamepadGate"

# ---- fake DLL 文件名 ----

FAKE_DLL_NAMES = ("xinput1_3.dll", "xinput1_4.dll", "XInput9_1_0.dll")

# ---- 反作弊标记文件（小写），用于自动检测模式 ----

ANTICHEAT_MARKERS = (
    "mhyprot2.sys", "mhyprot.sys",            # 米哈游（原神/崩铁）
    "easyanticheat.sys", "easyanticheat_x64.sys",  # EAC
    "bedaisy.sys", "battleye.exe",            # BattlEye
    "anticheat",                               # 通用
    "eac_launcher.exe",                        # EAC launcher
)

# ---- SetupAPI 常量 ----

_DIGCF_PRESENT = 0x00000002
_DIGCF_ALLCLASSES = 0x00000004
_DIF_PROPERTYCHANGE = 0x00000012
_DICS_ENABLE = 1
_DICS_DISABLE = 2
_DICS_FLAG_GLOBAL = 1
_SPDRP_DEVICEDESC = 0x00000000
_SPDRP_HARDWAREID = 0x00000001
_SPDRP_COMPATIBLEIDS = 0x00000002

# 手柄设备关键字（小写匹配）
_GAMEPAD_KEYWORDS = (
    "gamepad", "joystick", "controller", "gaming",
    "ig_",      # Xbox XInput 设备标识（HID\VID_045E&PID_028E&IG_00）
    "xinput",
)


# ---- ctypes 结构体 ----

class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", ctypes.c_byte * 16),  # GUID (16 bytes)
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),      # PULONG
    ]


class _SP_CLASSINSTALL_HEADER(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InstallFunction", wintypes.DWORD),  # DI_FUNCTION
    ]


class _SP_PROPCHANGE_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ClassInstallHeader", _SP_CLASSINSTALL_HEADER),
        ("StateChange", wintypes.DWORD),  # DICS_STATE
        ("Scope", wintypes.DWORD),        # DICS_SCOPE
        ("HwProfile", wintypes.DWORD),    # 0 = current profile
    ]


# ---- 库引用 ----

_kernel32 = ctypes.windll.kernel32
_setupapi = ctypes.windll.setupapi
_shell32 = ctypes.windll.shell32


def is_admin() -> bool:
    """当前进程是否以管理员权限运行。"""
    try:
        return bool(_shell32.IsUserAnAdmin())
    except Exception:
        return False


def _bundled_dll_dir() -> str:
    """定位打包的 fake DLL 所在目录。

    源码运行：src/ningbingjue/vendor/xinput_gate/
    冻结 exe（onedir）：_internal/ningbingjue/vendor/xinput_gate/ 或同级
    """
    # 源码：相对于本文件
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.normpath(os.path.join(here, "..", "vendor", "xinput_gate"))
    if os.path.exists(os.path.join(candidate, FAKE_DLL_NAMES[0])):
        return candidate
    # 冻结 exe：sys._MEIPASS（PyInstaller onedir/onefile）
    base = getattr(sys, "_MEIPASS", None)
    if base:
        for rel in ("ningbingjue/vendor/xinput_gate", "vendor/xinput_gate",
                     "xinput_gate", "_internal/xinput_gate"):
            p = os.path.join(base, rel)
            if os.path.exists(os.path.join(p, FAKE_DLL_NAMES[0])):
                return p
    # exe 上爬
    exe_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else None
    if exe_dir:
        for rel in ("_internal/xinput_gate", "xinput_gate",
                     "ningbingjue/vendor/xinput_gate"):
            p = os.path.join(exe_dir, rel)
            if os.path.exists(os.path.join(p, FAKE_DLL_NAMES[0])):
                return p
    return candidate  # 返回最可能的路径（即使不存在，让调用方报错）


class XInputGate:
    """手柄隔离管理器（双模式）。

    生命周期：
      gate = XInputGate()           # 创建命名事件
      gate.set_active(True, mode)   # 凝冰结魄获得焦点
      gate.set_active(False, mode)  # 凝冰结魄失焦
      gate.close()                  # 退出，确保设备恢复

    模式：
      "dll_hijack"     — DLL 劫持（单机游戏），HUD 模式手柄可用
      "device_disable"  — 设备禁用（反作弊游戏），HUD 模式用键盘
    """

    def __init__(self) -> None:
        self._event: Optional[int] = None  # HANDLE
        self._create_event()
        # 设备禁用模式：记录已禁用的设备 DevInst，退出/失焦时恢复
        self._disabled_devinsts: list[int] = []

    # ---- 命名事件 ----

    def _create_event(self) -> None:
        """创建命名事件（手动复位，初始非信号态）。

        事件信号态 = 凝冰结魄前台（fake DLL 拦截 XInputGetState）。
        事件非信号态 = 凝冰结魄失焦/未启动（fake DLL 透传真实状态）。
        """
        try:
            _kernel32.CreateEventW.argtypes = [
                ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, ctypes.c_wchar_p,
            ]
            _kernel32.CreateEventW.restype = ctypes.c_void_p
            _kernel32.SetEvent.argtypes = [ctypes.c_void_p]
            _kernel32.SetEvent.restype = wintypes.BOOL
            _kernel32.ResetEvent.argtypes = [ctypes.c_void_p]
            _kernel32.ResetEvent.restype = wintypes.BOOL
            _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            _kernel32.CloseHandle.restype = wintypes.BOOL
            # bManualReset=TRUE, bInitialState=FALSE
            h = _kernel32.CreateEventW(None, True, False, GATE_EVENT_NAME)
            if h:
                self._event = h
        except Exception:
            self._event = None

    def _set_event(self, foreground: bool) -> None:
        if not self._event:
            return
        try:
            if foreground:
                _kernel32.SetEvent(self._event)
            else:
                _kernel32.ResetEvent(self._event)
        except Exception:
            pass

    # ---- 公共 API ----

    def set_active(self, foreground: bool, mode: str = "dll_hijack") -> None:
        """凝冰结魄前台/失焦时调用，按模式分发。

        Args:
            foreground: True = 凝冰结魄获得焦点，False = 失焦
            mode: "dll_hijack" 或 "device_disable"
        """
        if mode == "dll_hijack":
            self._set_event(foreground)
        elif mode == "device_disable":
            if foreground:
                self._disable_physical_gamepad()
            else:
                self._enable_physical_gamepad()

    # ---- DLL 劫持模式 ----

    def deploy_to_game(self, game_exe_path: str) -> bool:
        """把 fake DLL 复制到游戏 exe 同目录。

        Returns True if all DLLs copied successfully.
        """
        game_dir = os.path.dirname(game_exe_path)
        if not game_dir or not os.path.isdir(game_dir):
            return False
        src_dir = _bundled_dll_dir()
        ok = True
        for name in FAKE_DLL_NAMES:
            src = os.path.join(src_dir, name)
            dst = os.path.join(game_dir, name)
            try:
                if os.path.exists(src):
                    shutil.copy2(src, dst)
                else:
                    ok = False
            except Exception:
                ok = False
        return ok

    def remove_from_game(self, game_exe_path: str) -> bool:
        """从游戏目录删除 fake DLL。"""
        game_dir = os.path.dirname(game_exe_path)
        if not game_dir:
            return False
        ok = True
        for name in FAKE_DLL_NAMES:
            p = os.path.join(game_dir, name)
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                ok = False
        return ok

    def is_deployed(self, game_exe_path: str) -> bool:
        """检查游戏目录是否已部署 fake DLL。"""
        game_dir = os.path.dirname(game_exe_path)
        if not game_dir:
            return False
        return os.path.exists(os.path.join(game_dir, FAKE_DLL_NAMES[0]))

    # ---- 设备禁用模式 ----

    def _find_gamepad_devinsts(self) -> list[int]:
        """枚举所有 HID 设备，返回手柄类设备的 DevInst 列表。

        识别逻辑：获取设备的 HardwareID / CompatibleIDs / DeviceDesc，
        检查是否包含手柄关键字（gamepad/joystick/controller/ig_/xinput）。
        """
        _setupapi.SetupDiGetClassDevsW.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, wintypes.DWORD,
        ]
        _setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p  # HDEVINFO
        _setupapi.SetupDiEnumDeviceInfo.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_SP_DEVINFO_DATA),
        ]
        _setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
        _setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_SP_DEVINFO_DATA),
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        _setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
        _setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
        _setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

        # INVALID_HANDLE_VALUE
        INVALID = ctypes.c_void_p(-1).value
        hdev = _setupapi.SetupDiGetClassDevsW(
            None, None, None, _DIGCF_ALLCLASSES | _DIGCF_PRESENT,
        )
        if not hdev or hdev == INVALID:
            return []

        results: list[int] = []
        did = _SP_DEVINFO_DATA()
        did.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
        index = 0
        try:
            while _setupapi.SetupDiEnumDeviceInfo(hdev, index, ctypes.byref(did)):
                index += 1
                if self._devinst_is_gamepad(hdev, did):
                    results.append(did.DevInst)
        except Exception:
            pass
        finally:
            _setupapi.SetupDiDestroyDeviceInfoList(hdev)
        return results

    def _devinst_is_gamepad(self, hdev: int, did: _SP_DEVINFO_DATA) -> bool:
        """检查单个设备是否是手柄。"""
        for prop in (_SPDRP_HARDWAREID, _SPDRP_COMPATIBLEIDS, _SPDRP_DEVICEDESC):
            text = self._get_device_registry_property(hdev, did, prop)
            if text:
                low = text.lower()
                for kw in _GAMEPAD_KEYWORDS:
                    if kw in low:
                        return True
        return False

    def _get_device_registry_property(
        self, hdev: int, did: _SP_DEVINFO_DATA, prop: int,
    ) -> str:
        """读取设备注册表属性（REG_SZ 或 REG_MULTI_SZ）。"""
        data_type = wintypes.DWORD(0)
        buf = ctypes.create_unicode_buffer(1024)
        required = wintypes.DWORD(0)
        ok = _setupapi.SetupDiGetDeviceRegistryPropertyW(
            hdev, ctypes.byref(did), prop,
            ctypes.byref(data_type),
            buf, ctypes.sizeof(buf), ctypes.byref(required),
        )
        if not ok:
            # 可能是空间不够（required > sizeof(buf)），再试一次
            if required.value > 0:
                buf2 = ctypes.create_unicode_buffer(required.value // 2 + 2)
                ok = _setupapi.SetupDiGetDeviceRegistryPropertyW(
                    hdev, ctypes.byref(did), prop,
                    ctypes.byref(data_type),
                    buf2, ctypes.sizeof(buf2), ctypes.byref(required),
                )
                if ok:
                    return buf2.value
            return ""
        return buf.value

    def _set_device_state(self, devinst: int, enable: bool) -> bool:
        """禁用/启用单个设备（通过 DevInst 重新定位）。"""
        # 需要重新枚举找到这个 DevInst 对应的 SP_DEVINFO_DATA
        hdev = _setupapi.SetupDiGetClassDevsW(
            None, None, None, _DIGCF_ALLCLASSES | _DIGCF_PRESENT,
        )
        if not hdev or hdev == ctypes.c_void_p(-1).value:
            return False

        _setupapi.SetupDiSetClassInstallParamsW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_SP_DEVINFO_DATA),
            ctypes.c_void_p, wintypes.DWORD,
        ]
        _setupapi.SetupDiSetClassInstallParamsW.restype = wintypes.BOOL
        _setupapi.SetupDiCallClassInstaller.argtypes = [
            wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(_SP_DEVINFO_DATA),
        ]
        _setupapi.SetupDiCallClassInstaller.restype = wintypes.BOOL

        found = False
        did = _SP_DEVINFO_DATA()
        did.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
        index = 0
        try:
            while _setupapi.SetupDiEnumDeviceInfo(hdev, index, ctypes.byref(did)):
                index += 1
                if did.DevInst != devinst:
                    continue
                found = True
                params = _SP_PROPCHANGE_PARAMS()
                params.ClassInstallHeader.cbSize = ctypes.sizeof(_SP_CLASSINSTALL_HEADER)
                params.ClassInstallHeader.InstallFunction = _DIF_PROPERTYCHANGE
                params.StateChange = _DICS_ENABLE if enable else _DICS_DISABLE
                params.Scope = _DICS_FLAG_GLOBAL
                params.HwProfile = 0
                ok1 = _setupapi.SetupDiSetClassInstallParamsW(
                    hdev, ctypes.byref(did),
                    ctypes.byref(params), ctypes.sizeof(params),
                )
                ok2 = _setupapi.SetupDiCallClassInstaller(
                    _DIF_PROPERTYCHANGE, hdev, ctypes.byref(did),
                )
                return bool(ok1 and ok2)
        except Exception:
            pass
        finally:
            _setupapi.SetupDiDestroyDeviceInfoList(hdev)
        return found

    def _disable_physical_gamepad(self) -> bool:
        """禁用所有物理手柄设备。需管理员权限。"""
        if not is_admin():
            return False
        devinsts = self._find_gamepad_devinsts()
        if not devinsts:
            return False
        all_ok = True
        for di in devinsts:
            if self._set_device_state(di, enable=False):
                self._disabled_devinsts.append(di)
            else:
                all_ok = False
        return all_ok

    def _enable_physical_gamepad(self) -> bool:
        """重新启用之前禁用的物理手柄设备。"""
        if not self._disabled_devinsts:
            return True
        all_ok = True
        for di in self._disabled_devinsts:
            if not self._set_device_state(di, enable=True):
                all_ok = False
        self._disabled_devinsts.clear()
        return all_ok

    # ---- 反作弊自动检测 ----

    @classmethod
    def detect_anticheat(cls, game_exe_path: str) -> bool:
        """扫描游戏目录是否有已知反作弊文件。

        Returns True 如果检测到反作弊标记（建议用设备禁用模式）。
        """
        game_dir = os.path.dirname(game_exe_path)
        if not game_dir or not os.path.isdir(game_dir):
            return False
        for root, _dirs, files in os.walk(game_dir):
            # 限制深度，避免扫描超大游戏目录
            depth = root[len(game_dir):].count(os.sep)
            if depth > 3:
                continue
            for f in files:
                fl = f.lower()
                if fl in ANTICHEAT_MARKERS:
                    return True
                # 部分反作弊文件名带版本号，用前缀匹配
                for marker in ANTICHEAT_MARKERS:
                    if fl.startswith(marker):
                        return True
        return False

    # ---- 清理 ----

    def close(self) -> None:
        """退出时调用：恢复物理手柄 + 关闭事件句柄。"""
        if self._disabled_devinsts:
            self._enable_physical_gamepad()
        if self._event:
            try:
                _kernel32.CloseHandle(self._event)
            except Exception:
                pass
            self._event = None


__all__ = ["XInputGate", "is_admin", "GATE_EVENT_NAME", "FAKE_DLL_NAMES"]
