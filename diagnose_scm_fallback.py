"""SCM Fallback 诊断脚本 — 验证 WinRing0 SCM + DeviceIoControl 路径。

在**管理员终端**里运行：
    python diagnose_scm_fallback.py

它会：
  1. 尝试 Path A (InitializeOls) 并报告 GetDllStatus
  2. 若 status=3 → 自动尝试 Path B (SCM + DeviceIoControl)
  3. 若 SCM 成功 → 做一次真实的 EC 端口读写测试（读 SIO 0x4E）
  4. 报告完整诊断结果

注意：必须管理员！SCM 的 CreateService 需要管理员权限。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from ningbingjue.platform.ec_io import (
    health_check, diag_info, is_admin,
    _locate_winring0_dll, _locate_winring0_sys,
    _scm_open_manager, _scm_install_driver, _scm_open_device,
    _scm_uninstall_driver, _scm_close_service_handle, _unload_scm,
    _dev_handle, _scm_loaded, _scm_method,
    _IOCTL_READ_PORT, _IOCTL_WRITE_PORT,
    _device_io_control, _write_port_byte, _read_port_byte,
    last_error,
)

def main():
    print("=" * 60)
    print("WinRing0 SCM Fallback 诊断")
    print("=" * 60)

    # 1. Basic info
    print(f"\n[1] 基本信息:")
    print(f"  Python: {sys.executable}")
    print(f"  管理员: {is_admin()}")
    dll = _locate_winring0_dll()
    sys_p = _locate_winring0_sys()
    print(f"  DLL:   {dll or '(未找到)'}")
    print(f"  SYS:   {sys_p or '(未找到)'}")

    if not is_admin():
        print("\n⚠️  非管理员！SCM 路径需要管理员权限。")
        print("   请在管理员终端运行此脚本。")
        return

    # 2. Path A: InitializeOls
    print(f"\n[2] Path A: InitializeOls (COM 接口)")
    # Force re-init by calling health_check
    ols_ok = health_check()
    d = diag_info()
    st = d.get("getdllstatus")
    method = d.get("method", "")
    print(f"  GetDllStatus: {st}")
    print(f"  结果: {'成功' if ols_ok else '失败'}")
    if not ols_ok:
        print(f"  错误: {last_error()}")

    # 3. If OLS failed with status=3, try SCM
    if method != "scm":
        print(f"\n[3] Path B: SCM (服务方式加载) — 尝试 fallback...")
        if not sys_p:
            print(f"  ✗ 找不到 WinRing0x64.sys，无法尝试 SCM")
            return

        scm_ok = _try_load_scm(sys_p) if hasattr('_try_load_scm') or True else None
        # Actually call through ec_io's internal
        from ningbingjue.platform import ec_io as ec_mod
        # Reset and retry health_check which will now try SCM
        scm_ok = ec_mod.health_check()
        d2 = ec_mod.diag_info()
        method2 = d2.get("method", "")
        print(f"  方法: {method2.upper() if method2 else '(无)'}")
        print(f"  SCM 已加载: {_scm_loaded}")
        print(f"  设备句柄: {_dev_handle}")
        print(f"  结果: {'成功' if scm_ok else '失败: ' + last_error()}")
    else:
        print(f"\n[3] SCM 已是活跃方法，跳过重复初始化")
        scm_ok = True

    # 4. Port I/O test (if either path succeeded)
    active_method = _scm_method
    print(f"\n[4] 端口 I/O 测试 (active={active_method or '无'})")
    if not (health_check()):
        print(f"  ✗ 无可用 I/O 路径，跳过端口测试")
        return

    # Test reading SIO index port (0x4E) — this should always succeed on any EC
    test_port = 0x4E
    val = _read_port_byte(test_port)
    print(f"  读端口 0x{test_port:02X} (SIO 索引口): 0x{val&0xFF:02X}" if val is not None else f"  读端口 0x{test_port:02X}: 失败")

    # If read worked, try EC RAM access (read addr 0x0000 via 4E/4F protocol)
    if val is not None:
        print(f"\n[5] EC RAM 协议测试 (SIO 0x4E/0x4F LDN 切换)")
        from ningbingjue.platform.ec_io import _ec_ram_read_4e4f
        # Read EC address 0x0000 (usually chip ID or similar)
        ec_val = _ec_ram_read_4e4f(0x0000)
        if ec_val is not None:
            print(f"  EC_RAM[0x0000] = 0x{ec_val & 0xFFFF:04X}  ✓ EC 通信正常")
        else:
            print(f"  EC_RAM[0x0000] = (读取失败)")

        # Try write+read-back on a safe address (fan register area, but just testing protocol)
        # We won't actually change fan speed here - just verify writes don't crash
        from ningbingjue.platform.ec_io import _ec_ram_write_4e4f
        # Write current value back (no-op effectively)
        if ec_val is not None:
            wr_ok = _ec_ram_write_4e4f(0x0000, ec_val & 0xFF)
            print(f"  写回 EC_RAM[0x0000]=0x{ec_val&0xFF:02X}: {'OK' if wr_ok else 'FAIL'}")

    # Cleanup
    print(f"\n[6] 清理")
    _unload_scm()
    print(f"  SCM 已卸载")

    print(f"\n{'=' * 60}")
    if scm_ok or ols_ok:
        print("结论: WinRing0 至少有一条路径可用 ✓")
        print(f"活跃方法: {(active_method or 'OLS').upper()}")
    else:
        print("结论: 所有路径均不可用 ✗")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
