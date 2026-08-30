# -*- coding: utf-8 -*-
"""
gpu_cpu_diag.py —— 掌机 GPU/CPU 读取独立诊断脚本（零项目依赖，仅用标准库）

用途：
  复现并定位「自动 GPU 永远 warmed=False / 读不到 GPU 利用率」的根因。
  app 的 _GpuUtilMonitor 与 record_load.py 用【逐字节相同】的 PowerShell 脚本，
  但 app 启动时会经 launch.bat 自提权（RunAs admin），record_load.bat 直接双击（普通会话）。
  本脚本同时做：
    1) 检测当前是否提权（elevated）
    2) 单次 Get-Counter 探 GPU / CPU（含 stderr，能直接看到报错）
    3) 检查 'GPU Engine' 计数器集在当前会话是否存在
    4) 复现 app 的长驻 PowerShell 循环（捕获 stderr + BOOT 哨兵 + try/catch 写文件）
  关键：把 stderr 接出来（app 里是 DEVNULL，等于蒙眼），并在循环前先写一行 BOOT，
        这样能区分「进程根本没启动 / 启动但循环里抛错 / 正常出数」三种情况。

  掌机上怎么跑：
  a) 普通跑（验证计数器本身可用）：
       runtime/python/python.exe gpu_cpu_diag.py
  b) 提权跑（复现 app 的失败现场，最关键！）：
       右键 gpu_cpu_diag.py 所在文件夹的空白处 -> 在终端中打开 ->
       右键终端标题栏 -> 以管理员身份运行 -> 再执行上面的命令
     或：开始菜单搜 cmd -> 右键「以管理员身份运行」-> cd 到本目录 -> python gpu_cpu_diag.py
  跑完把控制台输出 + gpu_cpu_diag.log 发回来即可。
"""

import subprocess
import sys
import os
import time
import threading

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gpu_cpu_diag.log")
_PS = [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"]
# 兜底：若上面路径不存在则直接用 "powershell.exe"（靠 PATH）


def _ps_exe():
    for p in _PS:
        if os.path.exists(p):
            return p
    return "powershell.exe"


def is_elevated() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _ts():
    return time.strftime("%H:%M:%S")


def log(msg: str):
    line = f"[{_ts()}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run_capture_once(label: str, script: str, timeout: float = 25.0):
    """单次执行一段 PowerShell，把 stdout+stderr 都接出来并打印。"""
    log(f"=== [{label}] 单次执行（stderr 已捕获）===")
    try:
        proc = subprocess.run(
            [_ps_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=timeout,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if out:
            for ln in out.splitlines():
                log(f"  OUT: {ln}")
        else:
            log("  OUT: <空>")
        if err:
            for ln in err.splitlines():
                log(f"  ERR: {ln}")
        else:
            log("  ERR: <空>")
        log(f"  rc={proc.returncode}")
    except subprocess.TimeoutExpired as e:
        log(f"  TIMEOUT(>{timeout}s): stdout={(e.stdout or '')[:300]!r} stderr={(e.stderr or '')[:300]!r}")
    except Exception as e:
        log(f"  EXC: {e!r}")


def run_loop_harness(duration: float = 18.0):
    """复现 app 的长驻 PowerShell 循环（与 _GpuUtilMonitor 同款 Popen 参数），
    但：stderr 接出来、循环前写 BOOT 哨兵、循环体与整段都 try/catch 并把异常写 stderr。"""
    log(f"=== [loop] 复现 app 长驻监控（时长 {duration}s，stderr 已捕获）===")
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
try {
  [Console]::Out.WriteLine(('BOOT pid=' + $pid + ' elevated=' + ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)))
  [Console]::Out.Flush()
  $n = 0
  while ($true) {
    try {
      $vals = (Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction Stop).CounterSamples
      $sum = 0
      if ($vals) { $sum = ($vals | Measure-Object CookedValue -Sum).Sum }
      if ($null -eq $sum) { $sum = 0 }
      [Console]::Out.WriteLine(('GPU ' + $sum))
      [Console]::Out.Flush()
    } catch {
      [Console]::Error.WriteLine(('LOOP_ERR: ' + $_.Exception.Message))
    }
    Start-Sleep -Milliseconds 1500
    $n++
    if ($n -ge 40) { break }
  }
} catch {
  [Console]::Error.WriteLine(('FATAL: ' + $_.Exception.Message))
}
"""
    try:
        proc = subprocess.Popen(
            [_ps_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        log(f"  SPAWN_FAILED: {e!r}")
        return

    log(f"  已启动 pid={proc.pid}")
    buf_lock = threading.Lock()
    lines_out, lines_err = [], []

    def pump(pipe, sink, tag):
        try:
            for line in pipe:
                s = line.rstrip("\n").rstrip("\r")
                with buf_lock:
                    sink.append(s)
                log(f"  {tag}: {s}")
        except Exception as e:
            with buf_lock:
                sink.append(f"<{tag}_EXC {e!r}>")
            log(f"  {tag}_EXC: {e!r}")

    t1 = threading.Thread(target=pump, args=(proc.stdout, lines_out, "OUT"), daemon=True)
    t2 = threading.Thread(target=pump, args=(proc.stderr, lines_err, "ERR"), daemon=True)
    t1.start()
    t2.start()

    # 在等待期间也检测进程是否提前死掉
    deadline = time.time() + duration
    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            log(f"  进程在 {duration - (deadline - time.time()):.1f}s 内提前退出 rc={rc}")
            break
        time.sleep(0.5)

    if proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass

    t1.join(timeout=3)
    t2.join(timeout=3)

    with buf_lock:
        has_boot = any(s.startswith("BOOT") for s in lines_out)
        has_gpu = any(s.startswith("GPU ") for s in lines_out)
        has_loop_err = any(s.startswith("LOOP_ERR") or s.startswith("FATAL") for s in lines_err)
    log(f"  结论: BOOT哨兵出现={has_boot}  收到GPU数据行={has_gpu}  PowerShell报错={has_loop_err}")
    if has_boot and not has_gpu and has_loop_err:
        log("  >> 进程启动了，但每次 Get-Counter 都抛错 → 当前会话的 'GPU Engine' 计数器不可读（提权/会话差异）。这就是 app warmed=False 的根因。")
    elif has_boot and has_gpu:
        log("  >> 正常出数，说明本环境下长驻监控可用 → app 的问题在 Python 侧（reader/单例未重启），不在计数器。")
    elif not has_boot:
        log("  >> 连 BOOT 都没收到 → PowerShell 进程在循环前就崩了（解析/模块/管道问题），看 ERR。")
    log(f"  原始: OUT行数={len(lines_out)} ERR行数={len(lines_err)}")


def main():
    # 清空旧日志
    try:
        if os.path.exists(LOG_PATH):
            os.remove(LOG_PATH)
    except Exception:
        pass

    log(f"#### gpu_cpu_diag 启动 pid={os.getpid()} elevated={is_elevated()} ####")
    if not is_elevated():
        log("提示：当前是【普通会话】。record_load.bat 也是普通会话且能读到 GPU，所以这里应能看到 GPU 数据。")
        log("      要复现 app 的失败，请【以管理员身份运行】本脚本后再跑一次对比！")
    else:
        log("提示：当前是【管理员/提权】会话 —— 这正是 app 通过 launch.bat 自提权后的运行环境，最可能出现 warmed=False。")

    # 1) 计数器集是否存在
    run_capture_once(
        "listset",
        r"try { (Get-Counter -ListSet 'GPU Engine' -ErrorAction Stop).CounterSetName } catch { 'LISTSET_ERR: ' + $_.Exception.Message }",
        timeout=20,
    )

    # 2) 单次 GPU
    run_capture_once(
        "gpu_single",
        r"try { $c = Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction Stop; ($c.CounterSamples | ForEach-Object { $_.InstanceName + '=' + $_.CookedValue.ToString('F2') }) -join ' ' } catch { 'GPU_ERR: ' + $_.Exception.Message }",
        timeout=25,
    )

    # 3) 单次 CPU 总 + 每核
    run_capture_once(
        "cpu_single",
        r"try { $t = (Get-Counter '\Processor(_Total)\% Processor Time' -ErrorAction Stop).CounterSamples; 'CPU_TOTAL=' + $t.CookedValue.ToString('F2'); $cores = (Get-Counter '\Processor(*)\% Processor Time' -ErrorAction Stop).CounterSamples | Where-Object { $_.InstanceName -ne '_Total' }; ($cores | ForEach-Object { $_.InstanceName + '=' + $_.CookedValue.ToString('F2') }) -join ' ' } catch { 'CPU_ERR: ' + $_.Exception.Message }",
        timeout=25,
    )

    # 4) 复现 app 长驻循环
    run_loop_harness(duration=18.0)

    log("#### 完成。把以上输出 + gpu_cpu_diag.log 发回即可。####")


if __name__ == "__main__":
    main()
