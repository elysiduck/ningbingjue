"""WinRing0 加载诊断（在目标 Windows 机器上运行，最好右键「以管理员身份运行」）。

打印：
  - 当前进程是否管理员 (IsUserAnAdmin)
  - 找到的 WinRing0x64.dll 路径
  - _ensure_loaded 结果
  - InitializeOls 返回值 + GetDllStatus 真实值
  - health_check 结果 + last_error
  - SCM 中 WinRing0_1_2_0 服务的状态/ImagePath（若存在）

GetDllStatus 含义（WinRing0 官方）：
  0 = 成功
  1 = 失败（未初始化）
  2 = 驱动未加载 (OLS_DLL_DRIVER_NOT_LOADED)   ← 最常见，多半是 CreateService 权限不足
  3 = 不支持的平台
  4 = 找不到驱动文件 (OLS_DLL_DRIVER_NOT_FOUND) ← dll 找不到同目录的 .sys
  5 = 驱动已卸载
  9 = 未知错误
"""
from __future__ import annotations
import ctypes
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
import ningbingjue.platform.ec_io as ec_io  # noqa: E402


def svc_status() -> str:
    try:
        import subprocess
        out = subprocess.run(
            ["sc", "qc", "WinRing0_1_2_0"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or out.stderr.strip()
    except Exception as e:  # noqa: BLE001
        return f"(sc query failed: {e})"


def main() -> None:
    print("=== WinRing0 诊断 ===")
    print("platform      :", sys.platform)
    print("executable    :", sys.executable)
    print("is_admin      :", ec_io.is_admin())

    dll = ec_io._locate_winring0_dll()
    print("dll path      :", dll)
    if dll:
        print("  dll exists  :", os.path.exists(dll))
        sys_path = os.path.join(os.path.dirname(dll), "WinRing0x64.sys")
        print("  sys beside  :", sys_path, "-> exists:", os.path.exists(sys_path))

    print("--- SCM 服务 WinRing0_1_2_0 ---")
    print(svc_status())

    print("--- 尝试加载 + InitializeOls ---")
    ok = ec_io._ensure_loaded()
    print("_ensure_loaded:", ok)
    if ec_io._InitializeOls is not None:
        try:
            rc = ec_io._InitializeOls()
            print("InitializeOls:", rc)
            st = ec_io._GetDllStatus()
            print("GetDllStatus :", st)
        except Exception as e:  # noqa: BLE001
            print("InitializeOls exception:", repr(e))
    else:
        print("API surface 未就绪, last_error:", ec_io.last_error())

    print("--- health_check ---")
    print("health_check  :", ec_io.health_check())
    print("last_error    :", ec_io.last_error())
    print("=== 诊断结束 ===")


if __name__ == "__main__":
    main()
