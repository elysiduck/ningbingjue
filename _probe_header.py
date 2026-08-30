import os
from ningbingjue.platform.ryzenadj import RyzenAdj, _GpuUtilMonitor

# 模拟 power.py 启动路径：先 ensure_started 再读一次（与 power.py __init__ 一致）
ra = RyzenAdj("dummy.exe")
_GpuUtilMonitor.instance().ensure_started()
v = ra.read_gpu_utilization()
print("first read ->", v)

log = os.path.join("logs", "gpu_util.log")
print("--- gpu_util.log ---")
try:
    with open(log, "r", encoding="utf-8", errors="ignore") as f:
        print(f.read() or "<EMPTY FILE>")
except FileNotFoundError:
    print("<FILE NOT FOUND>")
