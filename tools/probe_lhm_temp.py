#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
probe_lhm_temp.py — 验证 LibreHardwareMonitor 能否在本机读到 CPU 结温，并与 HWiNFO 真值 A/B。

用法（掌机、管理员 PowerShell 里）：
    py tools/probe_lhm_temp.py

前置条件：
    1) 已安装并运行 LibreHardwareMonitor（需管理员；设置里 WMI 提供程序默认开启）。
    2) 可选：HWiNFO 开着且开了 Shared Memory，用于和 LHM 数字直接对照。

判读：
    脚本会列出 LHM WMI 里所有温度类 / 名字含 CPU|Core 的传感器。
    其中“名字含 CPU、值在 30~100℃、随负载变化”的那行，就是要把风扇曲线绑定的结温。
    把它和 HWiNFO 的 CPU(Tctl/Tdie) 对照，差几度内即视为对齐成功。

注意：本脚本只“读”，不改任何配置、不写 EC。纯诊断用。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _ps(cmd: str) -> tuple[str, str]:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout or "", r.stderr or ""
    except Exception as e:  # noqa: BLE001
        return "", f"subprocess 失败: {e}"


def lhm_sensors():
    """从 LHM WMI provider 取温度类传感器。返回 [(name, sensortype, value)]。"""
    out, err = _ps(
        "Get-CimInstance -Namespace root\\LibreHardwareMonitor -ClassName Sensor "
        "| Where-Object {$_.SensorType -eq 'Temperature' -or $_.Name -match 'CPU|Core'} "
        "| ForEach-Object { \"$($_.Name)|$($_.SensorType)|$($_.Value)\" }"
    )
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        name, st, val = parts
        try:
            v = float(val)
        except (TypeError, ValueError):
            v = None
        rows.append((name.strip(), st.strip(), v))
    return rows, err


def hwinfo_temp():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from ningbingjue.core.hwinfo_smi import read_telemetry
        t = read_telemetry().temp_cpu
        return t if t is not None else "(HWiNFO 无数据)"
    except Exception as e:  # noqa: BLE001
        return f"(无 HWiNFO 对照: {e})"


def main() -> None:
    print("=== LibreHardwareMonitor CPU 温度探针 ===\n")
    rows, err = lhm_sensors()

    if not rows:
        print("[FAIL] 未从 root\\LibreHardwareMonitor 读到任何传感器。")
        if err.strip():
            print("  WMI 错误:", err.strip()[:300])
        print("  可能原因（按概率）：")
        print("    1) LHM 没以管理员运行 → 驱动/WMI 没起来；")
        print("    2) LHM 设置里 WMI 提供程序没开；")
        print("    3) Core Isolation(内存完整性) 拦了 LHM 的 WinRing0 驱动 → 主界面也读不到 CPU 温。")
        print("  请先确认 LHM 主界面能看到 CPU 温度，再跑本脚本。")
    else:
        print(f"{'传感器名':44} {'类型':12} {'值(℃)':>8}")
        print("-" * 66)
        for name, st, v in rows:
            print(f"{name[:44]:44} {st:12} {str(v):>8}")

    print()
    print("HWiNFO CPU(Tctl/Tdie) 对照:", hwinfo_temp())
    print()
    print("下一步：把上面 LHM 里“名字含 CPU、值随负载变、落在 30~100”的那行告诉我，")
    print("我据此在 fan.py 加 temp_source='lhm' 分支并接进 SharedMonitor。")


if __name__ == "__main__":
    main()
