import os, time
from ningbingjue.platform.ryzenadj import RyzenAdj

ra = RyzenAdj("dummy_nonexistent.exe")
print("available:", ra.available)
# 调用一次读取，应触发 gpu_util.log 首行写入
v1 = ra.read_gpu_utilization()
print("first read ->", v1)
time.sleep(2.0)
v2 = ra.read_gpu_utilization()
print("second read ->", v2)
time.sleep(6.0)
v3 = ra.read_gpu_utilization()
print("third read (after warmup) ->", v3)

log = os.path.join("logs", "gpu_util.log")
print("--- gpu_util.log contents ---")
try:
    with open(log, "r", encoding="utf-8", errors="ignore") as f:
        print(f.read() or "<EMPTY FILE>")
except FileNotFoundError:
    print("<FILE NOT FOUND>")
