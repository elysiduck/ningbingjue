"""ctypes 直调 RTSSHooks64.dll，替代 PowerShell 后端设置 RTSS 全局帧率限制。

证据（来自随包 ``RivaTuner Statistics Server.zip`` 内 RTSSHooks64.dll 的 PE
导出表，已用 PE 解析确认存在，裸名与 ``rtss_set_limit.ps1`` 中 C# DllImport 一致）：

  * ``void LoadProfile(LPCSTR profile)``
  * ``void SaveProfile(LPCSTR profile)``
  * ``void UpdateProfiles(void)``

调用约定：x64 下 stdcall / cdecl / fastcall 同 ABI，无差异；``CharSet=Ansi``
→ 字符串按 ANSI（bytes）传递。

本模块只负责「写全局 profile 的 Limit=」+「调 DLL 热重载」。任何环节失败
（DLL 缺失 / 符号缺失 / 写权限被拒 / 调用异常）一律返回 ``False``，由上层
（``Win32RtssCli.set_limit``）降级到 PowerShell 后端——PowerShell 会处理
Program Files 下的 UAC 自提权等复杂情况。

待上机验证项（开发机无 RTSS，无法在此确认）：
  * ``LoadProfile`` 的 profile 参数接受「完整路径」还是「profile 名」。
    此处与 ps1 保持一致传完整路径（``<rtss_dir>\\Profiles\\Global``）。
  * 是否必须 Load→Save→Update 三步齐调，还是 Save+Update 足够。沿用 ps1 三步。
  * ANSI 编码（``mbcs``）写入在用户实际 RTSS 版本下是否被正确读取。

可靠性修复（grill 决策）：
  * **目录配对**：``_running_rtss_dir()`` 返回「运行中 RTSS.exe 的真实目录」，
    作为 DLL / Profiles\\Global / 运行中进程三者配对的真相源——无论用户跑的
    是系统版还是 NBJ 部署版，只要 RTSS.exe 在跑，就改它那份 profile + 调它那份
    DLL，保证下发真正命中运行中实例（否则会改错目录 → 假成功）。
  * **读回验证**：写完 Limit → 热重载后，重读 profile 确认 ``Limit=<目标值>``
    仍在。若 RTSS 把内存旧值回写冲掉（LoadProfile 未生效的典型表现），读回值
    不等于目标值 → 返回 False → 上层降级 PS，UI 不再撒谎。
"""

from __future__ import annotations

import ctypes
import os
import re
import time

CREATE_NO_WINDOW = 0x08000000

# 真实导出符号（PE 导出表已确认存在；顺序即调用顺序）。
_NATIVE_EXPORTS = ("LoadProfile", "SaveProfile", "UpdateProfiles")

# 写盘用：匹配整行 Limit=... 以便就地替换。
_LIMIT_RE = re.compile(r"(?m)^\s*Limit\s*=.*$")
# 读回验证用：单行精确匹配并取出数字（Limit=<n>）。
_LIMIT_VAL_RE = re.compile(r"(?m)^\s*Limit\s*=\s*(\d+)\s*$")


def _log(msg: str) -> None:
    try:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        d = os.path.join(root, "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "nbj_rtss.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [native] {msg}\n")
    except Exception:
        pass


def _running_rtss_dir() -> "str | None":
    """返回当前运行中 RTSS.exe 的真实安装目录（exe 所在目录）。

    这是「DLL / Profiles\\Global / 运行中进程」三者配对的真相源：
    无论用户跑的是系统版还是 NBJ 部署版，只要 RTSS.exe 在跑，就改它那份
    profile + 调它那份 DLL，保证下发真正命中运行中实例。没在跑返回 None。
    """
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            nm = (p.info.get("name") or "")
            if nm.lower() == "rtss.exe":
                exe = p.info.get("exe")
                if exe and os.path.isfile(exe):
                    return os.path.dirname(exe)
    except Exception:
        pass
    return None


def _load_rtss_hooks(dll_path: str):
    """加载 RTSSHooks64.dll 并取出三个导出函数。

    失败时抛异常（符号缺失 → AttributeError；加载失败 → OSError 等），由调用方
    捕获后降级到 PowerShell。
    """
    # x64 下 CDLL（cdecl）与 WinDLL（stdcall）等价，用 CDLL 避免 windll 名字查找
    # 的额外修饰。argtypes/restype 仅作显式声明，防止 ctypes 默认假设错误。
    dll = ctypes.CDLL(dll_path)
    load_profile = getattr(dll, "LoadProfile")   # AttributeError → 符号缺失
    save_profile = getattr(dll, "SaveProfile")
    update_profiles = getattr(dll, "UpdateProfiles")
    load_profile.argtypes = [ctypes.c_char_p]
    load_profile.restype = None
    save_profile.argtypes = [ctypes.c_char_p]
    save_profile.restype = None
    update_profiles.argtypes = []
    update_profiles.restype = None
    return load_profile, save_profile, update_profiles


def _write_global_limit(profile_path: str, limit: int) -> None:
    """就地替换 ``Profiles\\Global`` 里的 ``Limit=<n>``（保留 ANSI 编码，与 ps1 一致）。

    找不到 ``Limit=`` 行则追加一条。``limit<=0`` 视为解除（写 0）。
    写权限被拒（Program Files 且非管理员）会抛 ``PermissionError``，由调用方
    捕获后降级 PowerShell（PS 会自提权）。
    """
    enc = "mbcs"  # Windows ANSI，对应 ps1 的 [System.Text.Encoding]::Default
    with open(profile_path, "r", encoding=enc, errors="replace") as fh:
        content = fh.read()
    replacement = "Limit=" + str(int(limit))
    new_content = _LIMIT_RE.sub(replacement, content)
    if new_content == content:
        # 极少数情况：profile 里没有 Limit= 行，追加一条
        new_content = content.rstrip() + "\r\n" + replacement + "\r\n"
    with open(profile_path, "w", encoding=enc) as fh:
        fh.write(new_content)


def _read_global_limit(profile_path: str) -> "int | None":
    """读 ``Profiles\\Global`` 里的 ``Limit=<n>`` 值；找不到/读不了返回 None。

    用于下发后读回验证，确认我们写的 Limit 确实落到文件（不被 RTSS 内存回写
    冲掉）。返回 int；profile 无 Limit 行或解析失败返回 None。编码与
    ``_write_global_limit`` 一致（mbcs）。
    """
    try:
        with open(profile_path, "r", encoding="mbcs", errors="replace") as fh:
            for line in fh:
                m = _LIMIT_VAL_RE.match(line)
                if m:
                    return int(m.group(1))
    except Exception:
        return None
    return None


def _rtss_alive() -> bool:
    """轻量探活：psutil 快路径；不可用则乐观放行（交给后续异常捕获/守护线程）。"""
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() == "rtss.exe":
                return True
        return False
    except Exception:
        return True


def native_apply_limit(rtss_dir: str, limit: int, rtss_running: bool | None = None) -> bool:
    """尝试用 ctypes 原生路径设置 RTSS 全局帧率限制。

    参数：
      * rtss_dir      RTSS 安装/部署目录（应来自运行中进程，见 ``_running_rtss_dir``）。
      * limit         目标帧率上限；0 = 解除锁定。
      * rtss_running  由调用方传入的探活结果（复用其缓存，避免重复扫描）。
                      True=已知在跑；False=已知没跑（直接降级 PS，避免假成功）；
                      None=未探（本函数内部用 psutil 再探一次）。

    返回 True 表示已尽力下发**且读回验证通过**（Limit 确实落到文件）；
    任何异常或读回不一致返回 False，调用方应降级到 PowerShell。
    """
    if rtss_running is False:
        # 明确知道没跑：native 发了也不会生效，直接降级 PS（PS 会拉起+写+重载）。
        return False
    try:
        dll_path = os.path.join(rtss_dir, "RTSSHooks64.dll")
        profile_path = os.path.join(rtss_dir, "Profiles", "Global")
        if not (os.path.exists(dll_path) and os.path.exists(profile_path)):
            return False
        if rtss_running is None and not _rtss_alive():
            return False
        # 1) 写 Limit 到全局 profile（用户可写目录零 UAC；Program Files 抛
        #    PermissionError → 下方 except → 返回 False → 降级 PS 自提权）。
        _write_global_limit(profile_path, limit)
        # 2) ctypes 热重载通知 RTSS 重新加载 profile。
        load_profile, save_profile, update_profiles = _load_rtss_hooks(dll_path)
        prof_b = profile_path.encode("mbcs")
        load_profile(prof_b)
        save_profile(prof_b)
        update_profiles()
        # 3) 读回验证：确认 Limit 真的落在文件里（被 RTSS 内存回写冲掉=判失败）。
        applied = _read_global_limit(profile_path)
        if applied != limit:
            _log(f"读回不符 target={limit} applied={applied} dir={rtss_dir} → 判失败降级")
            return False
        return True
    except Exception:
        return False
