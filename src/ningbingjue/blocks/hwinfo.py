# -*- coding: utf-8 -*-
"""blocks/hwinfo.py — HWiNFO 监控区块（部署 + 五层状态检测 + 一键重启）。

定位
====
只读观测 + 手动干预，绝不偷偷动 HWiNFO。行为边界由 grill 决策定死：

  * **不自动拉起**：HWiNFO 必须管理员权限才能读到传感器（作者 Martin 论坛原话：
    "it cannot run without Admin rights"）。NBJ 若静默 Popen，只会得到一个
    「进程在跑但读数全无」的半残实例。所以启动权永远在用户手上。
  * **不自动 kill**：用户可能正开着 HWiNFO 的传感器窗口在看数据，自动杀掉体验
    很坏。12h 到期只提示，用户点「重启 HWiNFO」按钮才动手。
  * **提前预警 + 事后兜底**：12h 倒计时来自进程启动时间（会被手动重开 / 重启
    重置，所以只能当预警）；真正的判据是共享内存的 pollTime 新鲜度。

卡片 UI 见 ``ui/panels.py::HwinfoPanel``。

与风扇卡片的关系
================
``blocks/fan.py`` 的 ``TempSource="hwinfo"`` 会把 HWiNFO 温度喂进风扇曲线
（这是本模块允许进入控制闭环的唯一例外，见 ``core/hwinfo_smi.py`` docstring 的
护栏说明）。HWiNFO 挂掉时 fan 会**静默回落到 ACPI**，而 ACPI 比 CPU 结温低
10~20℃ —— 「80℃ 风扇仍低转」的老坑。因此本卡片必须常驻可见，让回落无处藏身。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Any

from ..core.color.palette import COLOR_TEAL
from ..core.gui import _gui_method
from ..platform import hwinfo_deploy, hwinfo_status
from ..platform.hwinfo_deploy import EXE_NAME
from ..platform.hwinfo_status import HwinfoStatus
from ..ui.panels import _show_block_toast
from .base import BlockBlueprint

DEFAULT_COLOR = COLOR_TEAL

CREATE_NO_WINDOW = 0x08000000
SW_SHOWNORMAL = 1

# 探测节流：面板每秒都会 sync()，但真探测（进程扫描 + 共享内存映射）最多
# 每 PROBE_TTL 秒一次，避免把手柄轮询和呼吸动画的时间片吃掉。
PROBE_TTL = 6.0

# 重启后等 HWiNFO 初始化并重新建立共享内存的宽限时间（秒）。
RESTART_SETTLE_SECS = 6.0


class HwinfoBlock:
    """HWiNFO 监控区块。

    所有对外方法都不抛异常——单卡片失败不能拖垮整个 HUD。
    """

    def __init__(self, config: Any = None) -> None:
        self.config = config
        self.ui: dict[str, Any] = {}
        self._status = HwinfoStatus()
        self._last_probe_ts = 0.0
        self._lock = threading.RLock()

        # 自部署结果（后台线程写入，仅主线程读取显示）
        self.deploy_state: "tuple[str, str]" = ("idle", "")
        self._deploy_started = False

        self.toast_text = ""
        self.toast_success = False
        # 重启进行中：按钮置灰 + 文案变更，避免连点。
        self.restarting = False

    # -- 注册 -------------------------------------------------------------
    def register(self, registry: Any) -> None:
        bp = BlockBlueprint(
            block_type="hwinfo",
            name="HWiNFO 监控",
            color=DEFAULT_COLOR,
            card=None,
            hooks=self._build_hooks(),
        )
        registry.register(bp.to_block())

    def _build_hooks(self) -> dict[str, Any]:
        return {
            "GetCardHeight": lambda: 150,
            "OnRefresh": self.refresh,
            "OnSaveConfig": self.save_config,
            "OnLoadConfig": self.load_config,
            "OnPowerResume": self.on_power_resume,
        }

    # -- 状态探测 ---------------------------------------------------------
    def probe_status(self, force: bool = False) -> HwinfoStatus:
        """取当前状态（带 TTL 节流）。面板每秒调用也只会每 6s 真探一次。"""
        with self._lock:
            now = time.monotonic()
            if not force and (now - self._last_probe_ts) < PROBE_TTL:
                return self._status
            try:
                self._status = hwinfo_status.probe()
            except Exception:
                # 探测链本身崩了也要保底：退回一个「未知」状态，UI 不至于花屏。
                self._status = HwinfoStatus(state="stopped", text="HWiNFO 状态未知")
            self._last_probe_ts = now
            return self._status

    @property
    def status_obj(self) -> HwinfoStatus:
        return self.probe_status()

    def status(self) -> str:
        """一行状态文本，供冒烟报告 / 命令行输出。"""
        st = self.probe_status()
        if st.state == "ok" and st.remaining_s > 0:
            return f"{st.text} · v{st.version or '?'} · 剩余 {st.remaining_text()}"
        if st.version:
            return f"{st.text} · v{st.version}"
        return st.text

    @_gui_method
    def refresh(self) -> None:
        """OnRefresh / 手动刷新：强制重探一次并重绘卡片。"""
        self.probe_status(force=True)
        self._paint()

    def _paint(self) -> None:
        """让面板重绘（面板自身每秒 sync 也会刷，这里只是及时推一次）。"""
        try:
            panel = self.ui.get("card")
            if panel is not None and hasattr(panel, "sync"):
                panel.sync()
        except Exception:
            pass

    # -- 自部署 -----------------------------------------------------------
    def ensure_deploy_async(self) -> None:
        """启动期调用：后台线程跑一次自部署，不阻塞 UI 启动。

        与 ``rtss.ensure_rtss_async`` 同构。**不负责拉起 HWiNFO**（见模块
        docstring），只保证「要么系统有、要么我们释放了一份」。
        """
        if sys.platform != "win32" or self._deploy_started:
            return
        self._deploy_started = True
        try:
            threading.Thread(target=self._deploy_worker,
                             name="HwinfoDeploy", daemon=True).start()
        except Exception:
            pass

    def _deploy_worker(self) -> None:
        try:
            state, text = hwinfo_deploy.ensure_hwinfo()
            self.deploy_state = (state, text)
            hwinfo_deploy._log(f"自部署结果 state={state} text={text}")
        except Exception as e:
            self.deploy_state = ("missing", "自部署异常")
            hwinfo_deploy._log(f"自部署异常: {e!r}")

    # -- 一键重启 ---------------------------------------------------------
    def restart_hwinfo(self) -> bool:
        """杀掉并重新以管理员权限拉起 HWiNFO。

        这是免费版 12 小时限制的唯一自动化解法——重启会把共享内存的计时归零
        （INI 里 ``SensorsSM=1`` 会在启动时重新打开共享内存）。

        走 ``ShellExecuteW("runas")`` 提权，**会弹 UAC**（HWiNFO 没管理员读不到
        传感器，没有办法绕过）。这也是 grill 决策里「半托管」的含义：动手由用户
        点按钮触发，弹窗是预期行为而不是缺陷。

        返回是否成功发出拉起请求（不保证 HWiNFO 最终起来了——UAC 可能被取消）。
        """
        if sys.platform != "win32":
            self.show_toast("✗ 仅 Windows 支持", success=False)
            return False

        st = self.probe_status(force=True)
        exe = st.exe_path
        if not exe or not os.path.exists(exe):
            self.show_toast("✗ 未找到 HWiNFO64.exe", success=False)
            return False

        self.restarting = True
        self._paint()
        try:
            # 1) 杀掉现有实例。/F 强制：HWiNFO 缩在托盘时普通关闭可能无效。
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", EXE_NAME],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=10, creationflags=CREATE_NO_WINDOW,
                )
            except Exception:
                pass
            # 进程退出需要一点时间，太快拉起会被判为已有一个实例。
            time.sleep(1.0)
            hwinfo_status.reset_pid_cache()

            # 2) 提权拉起。ShellExecuteW 返回值 > 32 表示成功。
            import ctypes
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            rc = shell32.ShellExecuteW(
                None, "runas", exe, None, os.path.dirname(exe), SW_SHOWNORMAL
            )
            if rc <= 32:
                self.restarting = False
                self._paint()
                self.show_toast(
                    f"✗ 拉起失败（ShellExecute rc={rc}，UAC 可能被取消）",
                    success=False,
                )
                return False
        except Exception as e:
            self.restarting = False
            self._paint()
            self.show_toast(f"✗ 重启异常：{e!r}", success=False)
            return False

        # 3) 等它初始化完再重探，让卡片立刻反映真实状态（而不是「未运行」）。
        threading.Thread(target=self._settle_after_restart,
                         name="HwinfoRestart", daemon=True).start()
        return True

    def _settle_after_restart(self) -> None:
        try:
            time.sleep(RESTART_SETTLE_SECS)
            self.probe_status(force=True)
        except Exception:
            pass
        finally:
            self.restarting = False
            try:
                self._paint()
            except Exception:
                pass

    # -- 休眠恢复 ---------------------------------------------------------
    def on_power_resume(self) -> bool | None:
        """休眠恢复后重探一次。

        休眠不会让 HWiNFO 退出，但共享内存可能在恢复后短暂失联；且进程 uptime
        在休眠期间照常累计，12h 倒计时的读数需要刷新。仅清缓存强制重探，
        不做任何自动干预。
        """
        try:
            self.probe_status(force=True)
            return True
        except Exception:
            return False

    # -- 持久化 -----------------------------------------------------------
    def save_config(self) -> None:
        # 本区块无用户可配状态：HWiNFO 的启停、共享内存开关都由 HWiNFO 自己
        # 的 INI / 注册表托管。保留钩子以满足 Hooks 契约（app.save_all 会遍历调用）。
        pass

    def load_config(self) -> None:
        pass

    # -- toast ------------------------------------------------------------
    @_gui_method
    def show_toast(self, text: str, success: bool = True) -> None:
        self.toast_text = text
        self.toast_success = success
        _show_block_toast(self.ui, text, success)
