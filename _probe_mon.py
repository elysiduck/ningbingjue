import os, time
from ningbingjue.platform.ryzenadj import RyzenAdj, _GpuUtilMonitor

ra = RyzenAdj("dummy.exe")
# 模拟 power.py __init__ 启动路径
_GpuUtilMonitor.instance().ensure_started()
for i in range(4):
    v = ra.read_gpu_utilization()
    print(f"read#{i} -> {v}")
    time.sleep(2.0)

log = os.path.join("logs", "gpu_util.log")
print("--- gpu_util.log ---")
try:
    with open(log, "r", encoding="utf-8", errors="ignore") as f:
        print(f.read() or "<EMPTY FILE>")
except FileNotFoundError:
    print("<FILE NOT FOUND>")
