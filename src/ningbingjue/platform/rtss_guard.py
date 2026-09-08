"""RTSS 运行状态守护（独立低频线程，与采样链完全解耦）。

为什么需要它：自动锁帧 / 锁帧的「补下发」本质要重新写 ``Profiles\\Global`` 并让
RTSS 热重载；但 RTSS 是个用户态进程，可能被用户关掉、被清理工具杀掉、或偶发崩溃。
一旦它不在了，锁帧就成了静默失效（HUD 还显示「已锁定」但游戏照跑）。本守护负责
**探活 → 拉起 → 补下发**，把它维持在「应该活着就在」的状态。

设计约束（grill 决策）：
  * 独立低频线程，与 MetricSampler 采样回调完全解耦——绝不挂在采样链里，避免
    拉起一个可能卡住的子进程时冻结 GPU 浮动/风扇/锁帧整条链。
  * 仅在「锁帧或自动锁帧启用」时才守（context.enabled）；都没开就 idle，不碰 RTSS，
    避免平白常驻一个进程的守护开销。
  * 探活失败 → ``Popen`` 拉起（**不 wait**）→ 等 3s → 补下发一次 profile。
  * 连续失败达上限 → 指数退避到 60s，并把状态置 ``deployed_failed``，**绝不无限
    重试刷进程**（避免把 RTSS 拉起几十次把机器拖垮）。
  * RTSS 自身应走 ``HKCU\\...\\Run`` 开机自启；NBJ 只做补漏，不是主要拉起者。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

CREATE_NO_WINDOW = 0x08000000

DEFAULT_INTERVAL = 8.0       # 正常探测间隔（秒）
DEFAULT_MAX_RETRIES = 3      # 连续失败达此数 → 退避
BACKOFF_INTERVAL = 60.0      # 退避间隔（秒）
RELAUNCH_WAIT = 3.0          # 拉起后等待 RTSS 初始化（秒）


class RtssGuard:
    def __init__(self, rtss_cli, context, on_state=None,
                 interval: float = DEFAULT_INTERVAL,
                 max_retries: int = DEFAULT_MAX_RETRIES) -> None:
        self.rtss_cli = rtss_cli
        # context: 无参 callable → dict{enabled:bool, limit:int, deploy_dir:str|None}
        self._context = context
        self._on_state = on_state
        self.interval = float(interval)
        self.max_retries = int(max_retries)

        self._thread: "threading.Thread | None" = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.state: "tuple[str, str]" = ("idle", "")
        self._fail_count = 0

    # -- 公共控制 ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not self._is_windows():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="RtssGuard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        th = self._thread
        if th is not None:
            try:
                th.join(timeout=2.0)
            except Exception:
                pass
        self._thread = None

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- 内部 --------------------------------------------------------------
    def _is_windows(self) -> bool:
        import sys
        return sys.platform == "win32"

    def _set_state(self, state: str, text: str) -> None:
        with self._lock:
            self.state = (state, text)
        if self._on_state is not None:
            try:
                self._on_state(state, text)
            except Exception:
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            # 退避态：用 BACKOFF_INTERVAL 等待；否则用 interval。
            wait = BACKOFF_INTERVAL if self.state[0] == "deployed_failed" else self.interval
            if self._stop.wait(wait):
                break
            try:
                self._tick()
            except Exception:
                # 守护线程自身绝不能崩，否则失去兜底。
                pass

    def _tick(self) -> None:
        ctx = self._context() if callable(self._context) else {}
        enabled = bool(ctx.get("enabled", False))
        if not enabled:
            # 锁帧都没开，RTSS 无需强制在线；重置失败计数避免误退避。
            self._fail_count = 0
            if self.state[0] != "idle":
                self._set_state("idle", "")
            return

        if not self.rtss_cli.is_rtss_installed():
            self._set_state("missing", "RTSS 未安装")
            return

        running = self.rtss_cli.is_rtss_running()
        if running:
            self._fail_count = 0
            if self.state[0] != "running":
                self._set_state("running", "守护：RTSS 运行中")
            return

        # 未运行 → 尝试拉起 + 补下发。
        ok = self._relaunch(ctx)
        if ok:
            self._fail_count = 0
            self._set_state("running", "守护：已恢复 RTSS")
        else:
            self._fail_count += 1
            if self._fail_count >= self.max_retries:
                self._set_state(
                    "deployed_failed",
                    f"RTSS 未就绪（连续 {self._fail_count} 次拉起失败）",
                )
            else:
                self._set_state(
                    "installed",
                    f"RTSS 未运行，守护重试中（{self._fail_count}/{self.max_retries}）",
                )

    def _relaunch(self, ctx: dict) -> bool:
        deploy_dir = ctx.get("deploy_dir") or self.rtss_cli._resolve_rtss_dir() \
            if hasattr(self.rtss_cli, "_resolve_rtss_dir") else ctx.get("deploy_dir")
        exe = os.path.join(deploy_dir, "RTSS.exe") if deploy_dir else None
        if not exe or not os.path.exists(exe):
            return False
        try:
            # 拉起 RTSS（不 wait：UI/后台都不应被一个进程阻塞）。RTSS 自带托盘与
            # 自启，这里只是补漏——它挂了我们就补拉一次。
            subprocess.Popen([exe], creationflags=CREATE_NO_WINDOW)
        except Exception:
            return False
        # 给 RTSS 一点初始化时间（加载 profile、挂钩）。不精确但足够。
        time.sleep(RELAUNCH_WAIT)
        # 补下发当前 profile：锁帧开启则把当前 fps 写回去，否则写 0（解除）。
        try:
            limit = int(ctx.get("limit", 0) or 0)
            self.rtss_cli.set_limiter(ctx.get("enabled", False))
            self.rtss_cli.set_limit(limit)
        except Exception:
            pass
        # 以「现在是否在跑」作为拉起是否成功的最终判据。
        return bool(self.rtss_cli.is_rtss_running())
