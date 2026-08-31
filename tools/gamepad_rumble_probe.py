#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gamepad_rumble_probe.py — 手柄震动(rumble)可行性探针 (SPIKE, 不碰主程序)

目的
----
验证「长按阈值单次脉冲」方案在你 GPD 内置手柄上到底出不出声。这是用户想做的
手柄反馈里风险最高、未证实的一半：GPD 部分机型的马达只走厂商驱动，SDL2 摸不到；
且反作弊(device_disable)模式下物理手柄被禁用 → 那时既读不到也震不了。

本探针**复用 gamepad.py 同一条 SDL2 加载链**（pygame 的 SDL2.dll →
SDL_InitSubSystem(0x00002000) → SDL_GameControllerOpen），所以验出来的结果对主程序
直接有效，不是另起一套。

判定优先级
----------
1. SDL_JoystickHasRumble == 0  → SDL 自己说这手柄没马达 → 走 SDL 路径必不可用。
2. HasRumble == 1 但物理无反应 → GPD 固件没把马达接到 SDL 通道 → 同样不可用。
3. HasRumble == 1 且物理有反应 → 阈值单次脉冲方案可行，可接进主程序。

注意：本探针走的是**原生 SDL**，完全绕过 xinput_gate 的 DLL 劫持与 device_disable，
所以它能驱动 SDL 手柄时就代表 dll_hijack 模式下也能驱动；但**反作弊 device_disable
模式下物理设备被禁用**，本探针也读不到 —— 那属于另一类不可用，需在主程序里单独处理。

运行（在真机、手柄已连接时）：python tools/gamepad_rumble_probe.py
"""

from __future__ import annotations

import ctypes
import os
import sys

c_int = ctypes.c_int
c_uint = ctypes.c_uint
c_void_p = ctypes.c_void_p
c_char_p = ctypes.c_char_p

_SDL_INIT_GAMECONTROLLER = 0x00002000


class _SDLVersion(ctypes.Structure):
    _fields_ = [("major", ctypes.c_uint8),
                ("minor", ctypes.c_uint8),
                ("patch", ctypes.c_uint8)]


def _candidate_sdl2_dlls():
    """按优先级 yield (label, path)，path=None 表示「退化到系统 PATH 的 SDL2」。

    优先用**凝冰结魄 bundled runtime 自带的 SDL2.dll**（与生产 gamepad.py 加载的
    是同一个文件），这样无论用户用哪个 python 解释器跑本探针，验出来的震动结果都对
    主程序直接有效 —— 不再依赖「当前 python 能否 import pygame」。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    # ① bundled runtime 的 pygame（生产同款，最优先）
    bundled = os.path.normpath(os.path.join(
        here, "..", "runtime", "python", "Lib", "site-packages",
        "pygame", "SDL2.dll"))
    if os.path.exists(bundled):
        yield ("bundled runtime 的 pygame (生产同款)", bundled)
    # ② 当前解释器能 import 的 pygame
    try:
        import pygame  # noqa: F401
        p = os.path.join(os.path.dirname(pygame.__file__), "SDL2.dll")
        if os.path.exists(p):
            yield ("当前解释器的 pygame", p)
    except Exception:
        pass
    # ③ 系统 PATH 中的 SDL2（通常不独立存在，仅作兜底）
    yield ("系统 PATH 中的 SDL2", None)


def _find_sdl2_dll():
    """返回 (label, path)：优先生产同款 bundled DLL，path=None 表示退化到 PATH。"""
    for label, path in _candidate_sdl2_dlls():
        if path is None or os.path.exists(path):
            return label, path
    return "系统 PATH 中的 SDL2", None


def _verdict(rumble_ok: bool, has_rumble: int, physical: str,
             rumble_rc_ok: bool) -> str:
    if not rumble_ok:
        return ("VERDICT: SDL 加载/初始化失败或该手柄不可识别 → "
                "rumble 方案在此设备上不可用，建议砍掉震动半。")
    if has_rumble == 0:
        return ("VERDICT: SDL_JoystickHasRumble == 0（SDL 报告该手柄无马达）→ "
                "SDL 路径下 rumble 不可用。仍想做反馈就只保留视觉消失半。")
    a = physical.strip().lower()
    if a in ("n", "no", "否", "无"):
        return ("VERDICT: SDL 认为有马达(HasRumble==1)但物理无反应 → "
                "GPD 固件未把马达接到 SDL 通道，rumble 实际不可用。砍掉震动半。")
    if a in ("y", "yes", "是", "有"):
        extra = ("" if rumble_rc_ok else
                 "（注意：本次 SDL_GameControllerRumble 返回非0，但您确认有震动，"
                 "以物理为准）")
        return ("VERDICT: HasRumble==1 且物理有震动 → 阈值单次脉冲方案可行，"
                "可接进主程序（记得 device_disable 反作弊模式下仍不可用）。" + extra)
    # 空输入 / 未确认：绝不默认成功
    rc_note = ("" if rumble_rc_ok else
               "（本次 SDL_GameControllerRumble 返回非0，可能是下发失败，更需物理确认）")
    return ("VERDICT: HasRumble==1（SDL 报有马达）但物理震动未确认" + rc_note +
            " → 请在你的 GPD 真机上实跑：握住手柄、对提示输入 y/n 确认；"
            "若毫无反应则 rumble 不可用，只留视觉半。")


def main() -> int:
    label, path = _find_sdl2_dll()
    if path:
        print(f"[1] SDL2 DLL: {label} -> {path}")
    else:
        print(f"[1] SDL2 DLL: {label}（将退化到裸名 CDLL('SDL2')）")
    try:
        dll = ctypes.CDLL(path) if path else ctypes.CDLL("SDL2")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] 无法加载 SDL2: {e}")
        print("[INFO] 本探针优先加载凝冰结魄 bundled runtime 自带的 SDL2.dll"
              "（与生产 gamepad.py 同款）。")
        print("       若上面 [1] 显示的是「系统 PATH 中的 SDL2」，说明没找到 bundled"
              " DLL，请改用 bundled runtime 运行：")
        here = os.path.dirname(os.path.abspath(__file__))
        rt = os.path.normpath(os.path.join(here, "..", "runtime", "python",
                                           "python.exe"))
        print(f"         {rt}")
        print(_verdict(False, 0, ""))
        return 2

    # 版本
    try:
        dll.SDL_GetVersion.argtypes = [ctypes.POINTER(_SDLVersion)]
        dll.SDL_GetVersion.restype = None
        v = _SDLVersion()
        dll.SDL_GetVersion(ctypes.byref(v))
        print(f"[2] SDL 版本: {v.major}.{v.minor}.{v.patch}")
        sdl_version = (v.major, v.minor, v.patch)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 读不到 SDL 版本: {e}")
        sdl_version = (0, 0, 0)

    dll.SDL_InitSubSystem.argtypes = [c_uint]
    dll.SDL_InitSubSystem.restype = c_int
    dll.SDL_QuitSubSystem.argtypes = [c_uint]
    dll.SDL_QuitSubSystem.restype = None
    dll.SDL_NumJoysticks.restype = c_int
    dll.SDL_IsGameController.argtypes = [c_int]
    dll.SDL_IsGameController.restype = c_int
    dll.SDL_GameControllerOpen.argtypes = [c_int]
    dll.SDL_GameControllerOpen.restype = c_void_p
    dll.SDL_GameControllerName.argtypes = [c_void_p]
    dll.SDL_GameControllerName.restype = c_char_p
    dll.SDL_GameControllerGetJoystick.argtypes = [c_void_p]
    dll.SDL_GameControllerGetJoystick.restype = c_void_p
    dll.SDL_GameControllerClose.argtypes = [c_void_p]
    dll.SDL_GameControllerClose.restype = None
    dll.SDL_GetError.restype = c_char_p


def _rumble(dll, h, low: int, high: int, ms: int) -> int:
    """发一次 rumble 并打印返回码 + SDL_GetError，便于区分失败原因。"""
    rc = dll.SDL_GameControllerRumble(h, c_uint(low), c_uint(high), c_uint(ms))
    err = dll.SDL_GetError()
    err_s = err.decode("utf-8", "replace").strip() if err else ""
    tag = "（已下发）" if rc == 0 else f"（失败: {err_s or '无错误串'}）"
    print(f"       → SDL_GameControllerRumble({ms}ms, lo={low:#06x}, "
          f"hi={high:#06x}) 返回 {rc} {tag}")
    return rc

    if dll.SDL_InitSubSystem(_SDL_INIT_GAMECONTROLLER) != 0:
        err = dll.SDL_GetError()
        print(f"[FAIL] SDL_InitSubSystem(GAMECONTROLLER) 失败: "
              f"{err.decode('utf-8', 'replace') if err else '?'}")
        print(_verdict(False, 0, ""))
        return 2

    # 可选函数（SDL 2.0.9+ 才有）
    has_funcs = True
    try:
        dll.SDL_JoystickHasRumble.argtypes = [c_void_p]
        dll.SDL_JoystickHasRumble.restype = c_int
        dll.SDL_JoystickHasRumbleTriggers.argtypes = [c_void_p]
        dll.SDL_JoystickHasRumbleTriggers.restype = c_int
        dll.SDL_GameControllerRumble.argtypes = [c_void_p, c_uint, c_uint, c_uint]
        dll.SDL_GameControllerRumble.restype = c_int
    except Exception:  # noqa: BLE001
        has_funcs = False

    if sdl_version < (2, 0, 9):
        print("[WARN] SDL < 2.0.9，SDL_GameControllerRumble 可能不存在。")
        has_funcs = has_funcs and False

    n = dll.SDL_NumJoysticks()
    print(f"[3] 手柄数量 SDL_NumJoysticks = {n}")
    if n <= 0:
        print("[FAIL] 未检测到任何手柄。请先连接 GPD 内置手柄（或外接手柄）再跑。")
        print(_verdict(False, 0, ""))
        dll.SDL_QuitSubSystem(_SDL_INIT_GAMECONTROLLER)
        return 2

    reported = []
    any_rumble = False
    for i in range(n):
        if dll.SDL_IsGameController(i):
            h = dll.SDL_GameControllerOpen(i)
            if not h:
                print(f"  [{i}] GameController 打开失败")
                continue
            nm = dll.SDL_GameControllerName(h)
            name = nm.decode("utf-8", "replace") if nm else f"<idx {i}>"
            has_r = -1
            has_rt = -1
            if has_funcs:
                jh = dll.SDL_GameControllerGetJoystick(h)
                if jh:
                    has_r = dll.SDL_JoystickHasRumble(jh)
                    has_rt = dll.SDL_JoystickHasRumbleTriggers(jh)
            print(f"  [{i}] 名称={name!r}  HasRumble={has_r}  "
                  f"HasRumbleTriggers={has_rt}")
            reported.append((i, name, h, has_r))

            # 发一次 400ms 强脉冲，让用户肉验
            if has_funcs:
                rc = _rumble(dll, h, 0xFFFF, 0xFFFF, 400)
                if rc == 0:
                    any_rumble = True

    # 让用户确认物理反应
    print("\n[4] 请现在用手握住手柄，确认刚才是否感到震动：")
    print("    - 若 GPD 内置手柄在脉冲瞬间有震动 → 输入 y")
    print("    - 若毫无反应 → 输入 n")
    print("    - 若想再发几轮脉冲重验 → 输入 r")
    try:
        ans = input("    你的确认 (y/n/r): ").strip()
    except EOFError:
        ans = ""

    if ans.lower() == "r":
        print("重发 3 轮 400ms 脉冲（每轮间隔 600ms）…")
        import time
        for r in range(3):
            for (i, _name, h, _hr) in reported:
                try:
                    _rumble(dll, h, 0xFFFF, 0xFFFF, 400)
                except Exception as e:  # noqa: BLE001
                    print(f"       [重发 {i}] 异常: {e}")
            time.sleep(1.0)
        try:
            ans = input("    再确认 (y/n): ").strip()
        except EOFError:
            ans = ""

    # 收尾
    for (_i, _name, h, _hr) in reported:
        try:
            dll.SDL_GameControllerClose(h)
        except Exception:  # noqa: BLE001
            pass
    dll.SDL_QuitSubSystem(_SDL_INIT_GAMECONTROLLER)

    has_rumble_overall = 1 if any(r[3] == 1 for r in reported) else 0
    print("\n" + "=" * 60)
    print(_verdict(True, has_rumble_overall, ans, any_rumble))
    print("=" * 60)
    print("把上面的 VERDICT 行贴回对话即可，我据此决定震动半是否接进主程序。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
