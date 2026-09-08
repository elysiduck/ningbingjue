"""进程冻结 · 解冻失败现场诊断（模块版，供 FreezeEditPanel 按钮调用）。

自包含、零三方依赖（仅 ctypes / subprocess / stdlib），Windows only。

场景：游戏（如绝区零）冻结成功、解冻失败/卡住/窗口回不来时，在运行
凝冰结魄的机器上复现一次失败后，从「进程冻结」编辑页点击「解冻诊断」按钮
（或命令行跑 tools/diag_unfreeze.py），输出完整现场供分析：
  * 冻结组（frozen_groups.json / frozen_pids.txt）逐成员：进程存活？窗口句柄？
  * 当前可见窗口快照
  * logs/vdesktop.log 尾部
  * 报告写入 <root>/diag_unfreeze_<时间戳>.txt

只读（fix=False）不改变任何游戏/系统状态。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import time
from typing import Callable

SW_MINIMIZE = 6
SW_RESTORE = 9
PROCESS_SUSPEND_RESUME = 0x0800

_log: Callable[[str], None] = lambda msg: None  # 由 run_diag 注入


def _log_line(msg: str) -> None:
    _log(msg)


def _sh(cmd: list[str], timeout: int = 15) -> str:
    try:
        r = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=False, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return (r.stdout or b"").decode("utf-8", "replace")
    except Exception as e:
        return f"<ERR {e!r}>"


def _enum_visible_windows() -> dict[int, list[int]]:
    """{pid: [hwnd, ...]} 所有可见顶层窗口（ctypes）。"""
    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    out: dict[int, list[int]] = {}

    def _cb(hwnd: int, _l: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.setdefault(pid.value, []).append(int(hwnd))
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return out


def _window_titles(hwnds: list[int]) -> str:
    user32 = ctypes.windll.user32
    titles = []
    for hwnd in hwnds:
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length and length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        titles.append(f"{hwnd}({title!r})")
    return ", ".join(titles) if titles else "(无)"


def _pid_alive(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(0x0400, False, pid)
    if not h:
        return False
    kernel32.CloseHandle(h)
    return True


def _try_resume(pid: int) -> tuple[bool, str]:
    """NtResumeProcess 尝试恢复（仅 --fix / fix=True 时用）。"""
    ntdll = ctypes.windll.ntdll
    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not h:
        return False, "OpenProcess(SUSPEND_RESUME) 失败（权限/进程退出）"
    try:
        ret = ntdll.NtResumeProcess(h)
        if ret == 0:
            return True, "NtResumeProcess OK"
        return False, f"NtResumeProcess 返回 {ret:#x}"
    finally:
        kernel32.CloseHandle(h)


def _move_to_current(pid: int) -> tuple[bool, str]:
    """PowerShell Move-Window 把窗口移回当前桌面（仅 --fix 时用，尽力而为）。"""
    ps = (
        "$ErrorActionPreference='Stop'\n"
        "try { Import-Module VirtualDesktop -Force -DisableNameChecking "
        "-ErrorAction Stop } catch { Write-Output 'VD_ERR:module'; exit 3 }\n"
        "$p = Get-Process -Id %d -ErrorAction SilentlyContinue\n"
        "if (-not $p -or $p.MainWindowHandle -eq 0) "
        "{ Write-Output 'VD_ERR:no-hwnd'; exit 4 }\n"
        "Move-Window -Hwnd $p.MainWindowHandle -Desktop (Get-CurrentDesktop)\n"
        "Write-Output 'VD_OK'\n" % int(pid)
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", ps],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        return r.returncode == 0 and "VD_OK" in out, out[:120]
    except Exception as e:
        return False, repr(e)


def _find_frozen_files(root: str) -> tuple[str | None, str | None]:
    cands = [root, os.getcwd()]
    gj = pt = None
    for d in cands:
        if gj is None and os.path.isfile(os.path.join(d, "frozen_groups.json")):
            gj = os.path.join(d, "frozen_groups.json")
        if pt is None and os.path.isfile(os.path.join(d, "frozen_pids.txt")):
            pt = os.path.join(d, "frozen_pids.txt")
    return gj, pt


def _load_frozen(root: str) -> list[tuple[int, str, list[int]]]:
    """[(primary_pid, name, [pids...]), ...]"""
    gj, pt = _find_frozen_files(root)
    groups: list[tuple[int, str, list[int]]] = []
    if gj:
        try:
            with open(gj, encoding="utf-8") as fh:
                data = json.load(fh)
            for g in data if isinstance(data, list) else []:
                pids = [int(x) for x in (g.get("pids") or []) if str(x).isdigit()]
                if pids:
                    groups.append((int(g.get("pid", pids[0])),
                                   g.get("name", ""), pids))
        except Exception as e:
            _log_line(f"frozen_groups.json 读取失败: {e!r}")
    if pt:
        try:
            with open(pt, encoding="utf-8") as fh:
                pids = [int(x) for x in fh.read().split() if x.strip().isdigit()]
            if pids and not groups:
                groups.append((pids[0], "", pids))
        except Exception as e:
            _log_line(f"frozen_pids.txt 读取失败: {e!r}")
    return groups


def run_diag(root: str, fix: bool = False) -> str:
    """运行解冻诊断，返回完整报告文本（不写文件）。

    ``root`` 凝冰结魄安装目录（exe 或 layout.json 所在目录）。
    ``fix`` 为 True 时会对冻结组成员尝试真实 resume + 移回窗口
    （会改变游戏状态！默认 False 只读）。
    """
    lines: list[str] = []
    ts = time.strftime("%H:%M:%S")

    def log(msg: str) -> None:
        lines.append(f"[{ts}] {msg}")

    global _log
    _log = log

    log("==== 凝冰结魄 解冻现场诊断 ====")
    log(f"root 目录: {root}")
    log(f"模式: {'FIX（会改变游戏状态）' if fix else '只读'}")

    groups = _load_frozen(root)
    if not groups:
        log("[!] 未找到 frozen_groups.json / frozen_pids.txt ——"
            "冻结持久化可能在其它目录，请用 --root 指定")
    else:
        log(f"冻结组数: {len(groups)}")

    wins = _enum_visible_windows()
    log(f"当前可见顶层窗口: {len(wins)} 个")

    for idx, (primary, name, pids) in enumerate(groups):
        log(f"--- 组[{idx}] 主进程={primary} 名称={name!r} "
            f"成员={pids} (共{len(pids)}个) ---")
        for pid in pids:
            alive = _pid_alive(pid)
            hwnds = wins.get(pid, [])
            log(f"  pid={pid}: 存活={alive} 窗口={_window_titles(hwnds)}")
            if fix and alive:
                ok, msg = _try_resume(pid)
                log(f"    --fix resume: {ok} ({msg})")
                if hwnds:
                    ok2, msg2 = _move_to_current(pid)
                    log(f"    --fix move_to_current: {ok2} ({msg2})")

    vd_log = os.path.join(root, "logs", "vdesktop.log")
    if os.path.isfile(vd_log):
        with open(vd_log, encoding="utf-8", errors="replace") as fh:
            lines_all = fh.read().splitlines()
        log(f"--- logs/vdesktop.log 尾部 {min(40, len(lines_all))} 行 ---")
        for line in lines_all[-40:]:
            log("  " + line)
    else:
        log(f"[!] {vd_log} 不存在（可能没启用虚拟桌面功能）")

    return "\n".join(lines)


def run_diag_to_file(root: str, fix: bool = False) -> str:
    """运行诊断并写入 ``<root>/diag_unfreeze_<时间戳>.txt``，返回报告路径。

    报告写入失败时返回空串（调用方应提示查看安装目录日志）。
    """
    text = run_diag(root, fix=fix)
    report = os.path.join(
        root, f"diag_unfreeze_{time.strftime('%Y%m%d_%H%M%S')}.txt")
    try:
        with open(report, "w", encoding="utf-8") as fh:
            fh.write(text)
        return report
    except Exception:
        return ""


__all__ = ["run_diag", "run_diag_to_file"]
