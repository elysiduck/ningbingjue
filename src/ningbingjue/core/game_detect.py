"""core/game_detect.py — 共享的游戏进程探测（纯 stdlib + ctypes，无 PyQt）。

被两块复用：
  - blocks/freeze.py 的候选列表判定（is_game_process）
  - test-launch/py-script/measure_game_load.py 的 --auto 模式（find_foreground_game）

设计约束：本模块不得 import 任何会触发重型依赖（PyQt 等）的模块，
以便 measure 脚本在不加载整个应用包的情况下直接 import。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

# --------------------------------------------------------------------------
# Windows API 句柄
# --------------------------------------------------------------------------
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi    = ctypes.WinDLL("psapi",    use_last_error=True)
user32   = ctypes.WinDLL("user32",   use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ           = 0x0010


# --------------------------------------------------------------------------
# ctypes 原型声明
# --------------------------------------------------------------------------
class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime",  ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32)]


_OpenProcess = kernel32.OpenProcess
_OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_OpenProcess.restype  = wintypes.HANDLE

_CloseHandle = kernel32.CloseHandle
_CloseHandle.argtypes  = [wintypes.HANDLE]
_CloseHandle.restype   = wintypes.BOOL

_EnumProcesses = psapi.EnumProcesses
_EnumProcesses.argtypes = [
    ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
_EnumProcesses.restype  = wintypes.BOOL

_GetModuleFileNameExW = psapi.GetModuleFileNameExW
_GetModuleFileNameExW.argtypes = [
    wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD,
]
_GetModuleFileNameExW.restype  = wintypes.DWORD

_GetForegroundWindow = user32.GetForegroundWindow
_GetForegroundWindow.argtypes = []
_GetForegroundWindow.restype  = wintypes.HWND

_GetWindowThreadProcessId = user32.GetWindowThreadProcessId
_GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_GetWindowThreadProcessId.restype  = wintypes.DWORD


# --------------------------------------------------------------------------
# 低层 helpers
# --------------------------------------------------------------------------
def _open(pid: int) -> int:
    return _OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)


def get_exe_by_pid(pid: int) -> str | None:
    """按 PID 取 exe 文件名（含 .exe）。打不开/取不到返回 None。"""
    h = _open(pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(512)
        n = _GetModuleFileNameExW(h, None, buf, 512)
        if n:
            return os.path.basename(buf.value)
        return None
    finally:
        _CloseHandle(h)


def find_pid_by_exe(target_exe: str) -> int | None:
    """按 exe 文件名（大小写不敏感，自动补 .exe）找第一个匹配的 PID。"""
    target = target_exe.lower()
    if not target.endswith(".exe"):
        target += ".exe"
    ARRAYSIZE = 4096
    pids = (wintypes.DWORD * ARRAYSIZE)()
    needed = wintypes.DWORD()
    ok = _EnumProcesses(ctypes.cast(pids, ctypes.POINTER(wintypes.DWORD)),
                        ctypes.sizeof(pids), ctypes.byref(needed))
    if not ok:
        return None
    count = min(needed.value // ctypes.sizeof(wintypes.DWORD), ARRAYSIZE)
    for i in range(count):
        pid = pids[i]
        if pid == 0:
            continue
        h = _open(pid)
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(512)
            n = _GetModuleFileNameExW(h, None, buf, 512)
            if n and os.path.basename(buf.value).lower() == target:
                return pid
        finally:
            _CloseHandle(h)
    return None


def find_foreground_pid() -> int | None:
    """返回当前前台窗口所属进程 PID；无前台窗口返回 None。"""
    hwnd = _GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value if pid.value else None


# --------------------------------------------------------------------------
# 游戏判定
# --------------------------------------------------------------------------
def is_game_process(name: str) -> bool:
    """按关键字表判断一个 exe 名是否为游戏进程（大小写不敏感）。

    关键字表镜像自原 PowerShell Is-GameProcess，覆盖常见 3A / 网游 /
    启动器 / 引擎特征名。命中即视为游戏，无需维护白名单。

    已收紧的高误报标记（2026-07-26）：``game``（会命中 Windows 游戏平台服务
    如 GameBar / GameInputSvc）、``.x64.exe`` / ``.win64.exe``（命中海量非游戏
    的 64 位 exe，如 ``python.x64.exe``）、``unity``（命中 Unity Hub / Unity
    Editor 等开发工具）。这些项会污染"游戏优先"候选层，故移除；保留
    ``game.exe``（精确匹配，几乎只可能是游戏）。
    2026-07-30 进一步收紧：``wow`` 裸子串会误命中 ``splwow64.exe``（32 位打印
    驱动宿主，仅 24MB，被当成 World of Warcraft 混进冻结候选），改为精确标记
    ``wow.exe``，只命中真正的 WoW 客户端 ``WoW.exe``，不再误伤 ``splwow64``。
    2026-08-03 进一步收紧：``r6`` 裸子串会误命中 ``rtsshooksloader64.exe`` 等
    含 "r6"（如 "loader64" 中的 "r6"）的进程，被当成 Rainbow Six 混进冻结/
    前台游戏判定。而真正的 R6 客户端 exe 是 ``RainbowSix.exe``，靠 ``rainbow``
    标记即可命中，``r6`` 从来没匹配到真游戏、只会制造误报，故移除。
    """
    n = (name or "").lower()
    if not n:
        return False
    game_markers = (
        "steam", "epic", "playnite", "gog", "launcher", "battle",
        "overwatch", "valorant", "league", "csgo", "cs2", "elden", "cyberpunk",
        "witcher", "starfield", "baldurs", "monster", "hogwarts", "spider",
        "godofwar", "horizon", "ghost", "diablo", "wow.exe", "ffxiv", "gta", "rdr",
        "apex", "destiny", "warframe", "rainbow", "pubg", "rust", "arksv",
        "arksa", "dayz", "terraria", "stardew", "minecraft", "javaw", "bedrock",
        "roblox", "fortnite", "fallguys", "rocket", "game.exe",
        # 掌机 / 主流网游补充（2026-08-23）：绝区零 ZZZ 的 exe 为
        # ZenlessZoneZero.exe，原标记表无 "zenless"，导致自动锁帧 game_ok
        # 恒 False。补充 miHoYo / 库洛 / Game Science 等高频标记，长尾仍靠
        # 冻结卡片「标为游戏」白名单兜底。
        "zenless", "honkai", "starrail", "wuthering", "blackmyth",
        # 原神 GenshinImpact.exe（2026-08-23 补：exe 名不含任何旧标记，
        # 此前 game_ok 对原神也恒 False，自动锁帧同样失效）。
        "genshin",
    )
    return any(m in n for m in game_markers)


def find_foreground_game(whitelist: list[str] | None = None) -> tuple[int, str] | None:
    """返回 (pid, exe名) 如果前台窗口是游戏；否则 None。

    判定：前台进程 exe 在白名单中 或 is_game_process 命中。
    白名单可选——不传时纯靠关键字 + 前台窗口自动识别。
    """
    pid = find_foreground_pid()
    if pid is None:
        return None
    name = get_exe_by_pid(pid)
    if not name:
        return None
    wl: set[str] = set()
    if whitelist:
        for w in whitelist:
            ww = (w.lower() if w.lower().endswith(".exe") else w.lower() + ".exe")
            wl.add(ww)
    if (name.lower() in wl) or is_game_process(name):
        return pid, name
    return None


def enum_game_processes() -> list[tuple[int, str]]:
    """返回当前所有被 ``is_game_process`` 命中的 ``(pid, exe)`` 列表。

    用于录制工具的多进程跟踪：一次枚举系统全部进程，按 exe 名过滤出游戏
    进程（过滤掉系统 / 显卡 / 后台服务）。无游戏进程时返回空列表。
    """
    out: list[tuple[int, str]] = []
    ARRAYSIZE = 4096
    pids = (wintypes.DWORD * ARRAYSIZE)()
    needed = wintypes.DWORD()
    ok = _EnumProcesses(ctypes.cast(pids, ctypes.POINTER(wintypes.DWORD)),
                        ctypes.sizeof(pids), ctypes.byref(needed))
    if not ok:
        return out
    count = min(needed.value // ctypes.sizeof(wintypes.DWORD), ARRAYSIZE)
    for i in range(count):
        pid = pids[i]
        if pid == 0:
            continue
        name = get_exe_by_pid(pid)
        if name and is_game_process(name):
            out.append((pid, name))
    return out
