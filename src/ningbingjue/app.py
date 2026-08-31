"""Application orchestrator — startup flow of the original ``ds8ZZZ.ps1``.

Boot sequence (headless-capable):
1. load ``layout.json`` (+ apply Theme palette overrides)
2. build the BlockRegistry and NavigationController
3. discover/register blocks (all built-ins: rtss / procmon / freeze /
   ls / power / tile)
4. apply the active theme (propagates to every block's ``OnThemeChange``)
5. call each block's ``OnLoadConfig``
6. wire global shims (``Set-RTSSLock`` etc.) for backward-compatible calls

The GUI form is only created when ``headless=False`` on a Windows host; the
core engine + logic run identically without it.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any, Callable

# 各区块类改为在 discover_builtin_blocks() 内按需惰性导入（import / 构造 / register
# 任一层失败都只记日志跳过，绝不拖垮整 boot —— 单卡片损坏不再导致程序无法启动）。
from .core.color.state import apply_palette_overrides
from .core.color.styles import load_all_button_data, save_all_button_data
from .core.config import ConfigManager
from .core.navigation import NavigationController
from .core.registry import BlockRegistry
from .core.theme import ThemeManager
from .ui._pagination import pg_prune_empty_pages
from .platform.lossless_scaling import LosslessScaling, MockLosslessScaling
from .platform.process_manager import MockProcessManager, ProcessManager, Win32ProcessManager
from .platform.ryzenadj import MockRyzenAdj, RyzenAdj
from .platform.rtss_cli import MockRtssCli, RtssCli, Win32RtssCli
from .platform.virtual_desktop import (
    MockVirtualDesktopManager,
    VirtualDesktopManager,
    Win32VirtualDesktopManager,
)


def _boot_stage_log(msg: str) -> None:
    """boot() 分阶段耗时打点：追加到项目根 launch.log（与 run.py 的 log() 同一
    文件，方便用同一份时间轴对齐前后阶段）。打点本身任何失败都静默吞掉，
    绝不影响启动。用途：定位「启动特别久」的热点阶段（2026-08-30 grill 确认）。
    """
    try:
        # app.py 位于 <项目根>/src/ningbingjue/ → 上跳 3 级 = 项目根。
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        with open(os.path.join(root, "launch.log"), "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%H:%M:%S") + " [boot耗时] " + msg + "\n")
    except Exception:  # noqa: BLE001
        pass


def build_rtss_cli(script_root: str) -> RtssCli:
    # 新后端：随包自带的参数化 PowerShell 脚本（取代失效的 rtss-cli.exe）。
    # 它直接编辑 RTSS 全局 profile + 调 RTSSHooks64.dll 热重载，规避 rtss-cli
    # 与本地 RTSS 版本/API 不匹配导致的「锁不住」。
    vendored = os.path.join(
        os.path.dirname(__file__), "blocks", "RTSS", "rtss_set_limit.ps1"
    )
    if os.path.exists(vendored):
        return Win32RtssCli(vendored)
    # 回退：原始脚本文件夹（script_root/RTSS），兼容 legacy 备份目录。
    legacy = os.path.join(script_root, "RTSS", "rtss_set_limit.ps1")
    if os.path.exists(legacy):
        return Win32RtssCli(legacy)
    return MockRtssCli()


def build_process_manager() -> ProcessManager:
    try:
        pm = Win32ProcessManager()
        if pm.available:
            return pm
    except Exception:
        pass
    return MockProcessManager()


def build_ryzenadj(script_root: str) -> RyzenAdj:
    exe = os.path.join(script_root, "blocks", "ryzenadj-win64", "ryzenadj.exe")
    if os.path.exists(exe):
        return RyzenAdj(exe)
    return MockRyzenAdj()


def build_lossless(script_root: str) -> LosslessScaling:
    settings = os.path.join(script_root, "LS设置", "settings.xml")
    presets = os.path.join(script_root, "LS设置")
    if os.path.exists(settings) or os.path.isdir(presets):
        return LosslessScaling(settings, presets, settings + ".profile")
    return MockLosslessScaling()


def build_virtual_desktop(script_root: str | None = None) -> VirtualDesktopManager:
    """Virtual-desktop window mover (freeze -> background, unfreeze -> current).

    Uses the real Win32-backed implementation on Windows; falls back to a Mock
    on non-Windows hosts / when PowerShell is unavailable so the core engine
    stays testable headless.  Mirrors ``build_process_manager``."""
    try:
        # Log location policy: prefer the App's own working directory
        # (``script_root`` — the same folder that holds ``layout.json``) so the
        # log lives right next to the project and is trivially readable (by both
        # the user and tooling).  Fall back to AppData\\Local\\ningbingjue only
        # when script_root is unknown / unwritable.  The exact path is surfaced
        # in the UI on failure so it is never "invisible".
        log_dir = None
        if script_root:
            try:
                cand = os.path.join(script_root, "logs")
                os.makedirs(cand, exist_ok=True)
                log_dir = cand
            except Exception:
                log_dir = None
        if not log_dir:
            log_dir = os.path.join(os.path.expanduser("~"), "AppData", "Local",
                                   "ningbingjue")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "vdesktop.log")
        vd = Win32VirtualDesktopManager(log_path=log_path)
        if vd.available:
            # Always write a boot marker so the log ALWAYS exists, then drop the
            # doctor script next to it for interactive diagnosis / install.
            vd.log_boot()
            vd.write_diagnostic(log_dir)
            return vd
    except Exception:
        pass
    return MockVirtualDesktopManager()


# hook name -> backward-compatible global shim name (per block type)
_SHIM_MAP: dict[str, dict[str, str]] = {
    "rtss": {
        "OnRefresh": "Set-RTSSLock",
        "OnLoadConfig": "Load-RTSSConfig",
        "OnSaveConfig": "Save-RTSSConfig",
        "OnApplyLock": "Apply-Lock",
        "OnToggleLock": "Toggle-Lock",
    },
    "procmon": {
        "OnRefresh": "Refresh-ProcMonPanel",
        "OnLoadConfig": "Load-ProcMonConfig",
        "OnSaveConfig": "Save-ProcMonConfig",
        "OnToggleLock": "Toggle-ProcMonScript",
    },
    "freeze": {
        "OnRefresh": "Refresh-FreezePanel",
    },
    "ls": {
        "OnRefresh": "Refresh-LSPanel",
        "OnLoadConfig": "Load-LSPreset",
    },
    "power": {
        "OnRefresh": "Update-PowerStatus",
        "OnSaveConfig": "Save-PowerConfig",
        "OnLoadConfig": "Load-PowerConfig",
        "OnApplyLock": "Apply-PowerSettings",
    },
    "tile": {
        "OnRefresh": "Layout-TilePanel",
        "OnSaveConfig": "Save-TileConfigs",
        "OnLoadConfig": "Load-TileConfigs",
    },
}

# block type -> {shim name: method-name on the live block instance}
_PUBLIC_MAP: dict[str, dict[str, str]] = {
    "freeze": {
        "Handle-FreezeHotkey": "handle_hotkey",
        "Handle-FreezeEnter": "on_item_confirm",
    },
    "procmon": {
        "Toggle-ProcMonScript": "toggle_config",
    },
    "ls": {
        "Load-LSPreset": "_load_by_name",
    },
    "power": {
        "Toggle-PowerLock": "toggle_tdp_lock",
        "Toggle-GpuLock": "toggle_gpu_lock",
        "Toggle-GpuFloat": "toggle_gpu_float",
        "Apply-PowerSettings": "apply_power",
        "Confirm-PowerSettings": "show_confirm",
    },
    "tile": {
        "Invoke-TileLaunch": "launch_focused",
    },
}


class Application:
    def __init__(self, script_root: str, config_path: str | None = None, headless: bool = True) -> None:
        self.script_root = script_root
        # Default the config file to <script_root>/layout.json (the original
        # script's persistence target).  Without this, ``ConfigManager.save()``
        # sees ``path is None`` and returns early — so tiles (and every other
        # block's config) never persisted to disk.
        if not config_path:
            config_path = os.path.join(script_root, "layout.json")
        self.config_path = config_path
        self.headless = headless
        self.config_manager = ConfigManager(config_path)
        self.registry = BlockRegistry()
        self.theme_manager = ThemeManager(self.registry)
        self.nav: NavigationController | None = None
        # Set by the HUD window so gamepad/keyboard page switches (and the
        # page-bar buttons) can rebuild the visible panels + refresh the
        # page indicator.  PyQt6-only callback; safe to leave None headless.
        self._page_changed_cb: Any = None
        self.blocks: dict[str, Any] = {}
        self.shims: dict[str, Any] = {}

    # -- config ------------------------------------------------------------
    def load_config(self, path: str | None = None) -> None:
        self.config_manager.load(path or self.config_path)
        apply_palette_overrides(self.config_manager.config.theme)

    # -- blocks ------------------------------------------------------------
    def _load_block(self, key: str, block: "Block") -> "Block | None":
        """把已构造好的区块注册进 registry 与 self.blocks。

        调用方（discover_builtin_blocks）已在 try 中完成 import + 构造，
        此处只负责 register / 落盘引用；万一 register 抛错，异常会向上冒泡
        到调用方的 try 被捕获，从而单卡片失败不影响其它卡片。
        """
        block.register(self.registry)
        self.blocks[key] = block
        return block

    def _log_block_failure(self, key: str, exc: Exception) -> None:
        """单区块加载失败的统一日志：记文件 + 打印，方便用户定位哪张卡没起来。"""
        logging.getLogger("ningbingjue").error(
            "区块 %r 加载失败，已跳过（不影响其它卡片）：%r", key, exc)
        print(f"[discover] \u2717 区块 '{key}' 加载失败，已跳过：{exc}")

    def discover_builtin_blocks(
        self, stage_cb: "Callable[[str], None] | None" = None
    ) -> None:
        # stage_cb：boot() 传入的耗时打点回调（可选）。每张卡构造完记一段，
        # launch.log 里即可看出「11 秒到底花在哪张卡」。默认 None 保持兼容。
        def _mark(key: str) -> None:
            if stage_cb is not None:
                try:
                    stage_cb("区块 " + key)
                except Exception:  # noqa: BLE001
                    pass

        cfg = self.config_manager.config
        pm = build_process_manager()
        _mark("build_process_manager")

        # One ryzenadj instance shared by power + rtss (rtss reads the shared
        # MetricSampler for load, and the power block for the GPU auto-lock freq).
        ryzenadj = build_ryzenadj(self.script_root)
        _mark("build_ryzenadj")

        # power 块先建：rtss 自动锁帧需要读取 power.gpu_current_freq。
        # power 可能加载失败 \u2192 初值 None，下游 provider 用 bool(power) 短路保护。
        power: "Block | None" = None

        # 游戏白名单：复用冻结卡片「标为游戏」维护的 undervolt 白名单，使自动
        # 锁帧能识别 ZZZ 等漏路游戏（game_ok 判定）。运行时 self 为 MainWindow
        # 实例时才有该方法，故用 hasattr 保护，避免基类/测试环境 AttributeError。
        game_whitelist_provider: Callable[[], list[str]] | None = None
        if hasattr(self, "undervolt_get_whitelist"):
            game_whitelist_provider = lambda: self.undervolt_get_whitelist()

        # ---- 各区块「独立加载」：import / 构造 / register 任一失败只记日志跳过 ----
        # ---- 不中止整 boot。这是「功能卡片与程序解耦」的硬保障：未来任何一张 ----
        # ---- 新卡写得再离谱，也至多自己静默不加载，绝不会再让启动卡死。 ----

        # power 块先建：rtss 自动锁频需要读取 power.gpu_current_freq。
        try:
            from .blocks.power import PowerBlock
            power = self._load_block("power", PowerBlock(cfg, ryzenadj))
        except Exception as e:  # noqa: BLE001 - 单卡片隔离，吞掉一切异常
            self._log_block_failure("power", e)
        _mark("power")

        # GPU 自动锁频是否落到地板：直接注入布尔，消除 rtss 内硬编码的 400
        # 魔法数（地板 = power.GPU_MIN，改 GPU_MIN 时自动跟随）。CPU 占用 & GPU
        # 负载仍走全应用唯一的共享指标器 MetricSampler，不在此另起采样。
        # power 为 None（加载失败）时短路，避免二次崩溃。
        gpu_freq_provider: Callable[[], bool] | None = None
        if sys.platform == "win32":
            gpu_freq_provider = (
                lambda: bool(power) and power.gpu_current_freq <= power.GPU_MIN
            )

        try:
            from .blocks.rtss import RtssBlock
            self._load_block(
                "rtss",
                RtssBlock(
                    build_rtss_cli(self.script_root), self.script_root,
                    gpu_freq_provider=gpu_freq_provider,
                    game_whitelist_provider=game_whitelist_provider,
                ),
            )
        except Exception as e:  # noqa: BLE001
            self._log_block_failure("rtss", e)
        _mark("rtss")

        try:
            from .blocks.procmon import ProcMonBlock
            self._load_block("procmon", ProcMonBlock(cfg, pm))
        except Exception as e:  # noqa: BLE001
            self._log_block_failure("procmon", e)
        _mark("procmon")

        try:
            from .blocks.freeze import FreezeBlock
            self._load_block(
                "freeze",
                FreezeBlock(
                    self.script_root, pm,
                    vdesktop=build_virtual_desktop(self.script_root), config=cfg,
                ),
            )
        except Exception as e:  # noqa: BLE001
            self._log_block_failure("freeze", e)
        _mark("freeze")

        try:
            from .blocks.ls import LSBlock
            self._load_block("ls", LSBlock(build_lossless(self.script_root)))
        except Exception as e:  # noqa: BLE001
            self._log_block_failure("ls", e)
        _mark("ls")

        try:
            from .blocks.tile import TileBlock
            self._load_block("tile", TileBlock(cfg, pm))
        except Exception as e:  # noqa: BLE001
            self._log_block_failure("tile", e)
        _mark("tile")

        # 降压控制：轻量虚拟区块（真正的 UndervoltController 由主窗口装配）。
        # 放在 tile 之后注册，卡片顺序上落在最后，用户可在布局编辑器里自由拖动。
        try:
            from .blocks.undervolt import UndervoltBlock
            self._load_block("undervolt", UndervoltBlock())
        except Exception as e:  # noqa: BLE001
            self._log_block_failure("undervolt", e)
        _mark("undervolt")

        # 性能监控：轻量虚拟区块（只读实时聚合，由主窗口 perfmon_snapshot 提供）。
        # 紧跟 undervolt 注册，卡片顺序上落在末尾。
        try:
            from .blocks.perfmon import PerfMonBlock
            self._load_block("perfmon", PerfMonBlock())
        except Exception as e:  # noqa: BLE001
            self._log_block_failure("perfmon", e)
        _mark("perfmon")

        # SMT 热核隔离：真实区块（拓扑识别 + CPU 集合施加）。
        # 放在 perfmon 之后注册，卡片顺序上落在末尾，用户可在布局编辑器里自由拖动。
        try:
            from .blocks.smt import SmtBlock
            self._load_block("smt", SmtBlock(cfg, pm))
        except Exception as e:  # noqa: BLE001
            self._log_block_failure("smt", e)
        _mark("smt")

        # 风扇控制：移植 MotionAssistant 的 EC 路径，走 WinRing0 + 跨进程互斥。
        # 紧跟 power 注册：与电源设置语义相关；用户可在布局编辑器里自由拖动。
        # EC 写入失败（Core Isolation / 非管理员）\u2192 卡片仍显示，写入静默跳过。
        try:
            from .blocks.fan import FanBlock
            self._load_block("fan", FanBlock(cfg))
        except Exception as e:  # noqa: BLE001
            self._log_block_failure("fan", e)
        _mark("fan")

        # 任务列表：主页直接增删 + agent 自动填写。轻量真实区块（持有任务列表
        # 内存态 + 配置持久化）。
        try:
            from .blocks.tasklist import TaskListBlock
            self._load_block("tasklist", TaskListBlock(cfg))
        except Exception as e:  # noqa: BLE001
            self._log_block_failure("tasklist", e)
        _mark("tasklist")

        # 更新内容：展示上一次打包 -> 本次打包之间的更新。只读外部 changelog.json。
        try:
            from .blocks.changelog import ChangelogBlock
            self._load_block("changelog", ChangelogBlock(cfg))
        except Exception as e:  # noqa: BLE001
            self._log_block_failure("changelog", e)
        _mark("changelog")

        # 电源计划：移植 YeManCC 的电源计划管理（3 个预设 .pow 固定 GUID 导入 +
        # 用户导入/移除 + 切换 + 高亮当前）。需 app 引用做 sync_all / save_all。
        # plans_dir 用区块自身的模块相对路径（blocks/powerplans/），无需手动指定。
        try:
            from .blocks.powerplan import PowerPlanBlock
            # probe_in_background：GUI 模式下把 __init__ 的 powercfg 串行探测
            # （实测 3.82s，boot 最大热点）挪到后台线程；headless/测试保持同步。
            self._load_block(
                "powerplan",
                PowerPlanBlock(cfg, app=self,
                               probe_in_background=not self.headless))
        except Exception as e:  # noqa: BLE001
            self._log_block_failure("powerplan", e)
        _mark("powerplan")

        # HWiNFO 监控：传感器数据源（进程/共享内存/12h 限制）状态观测。
        # 放在 powerplan 之后、preset 之前注册：它是**只读观测 + 一键重启**，
        # 不参与 preset 的快照维度；而 preset 仍必须是最后一个注册的区块。
        # 卡片只做提示，绝不自动拉起/杀掉 HWiNFO（见 blocks/hwinfo.py）。
        try:
            # NBJ_SKIP_HWINFO=1 可彻底跳过本区块（调试用：本区块会探测外部进程
            # 与共享内存，在排查启动耗时 / 干扰性问题时可一键摘除）。
            if os.environ.get("NBJ_SKIP_HWINFO") != "1":
                from .blocks.hwinfo import HwinfoBlock
                blk = self._load_block("hwinfo", HwinfoBlock(cfg))
                # 首次运行自部署（后台线程，不阻塞启动）。仅在有内置包时才真释放文件。
                if blk is not None:
                    blk.ensure_deploy_async()
        except Exception as e:  # noqa: BLE001
            self._log_block_failure("hwinfo", e)
        _mark("hwinfo")

        # 配置预设：自由预设系统（替代原六档调度）。必须在 power/rtss/fan/powerplan
        # 之后注册，套用时通过各源区块的 OnApplyPreset 钩子回填。
        try:
            from .blocks.preset import PresetBlock
            self._load_block("preset", PresetBlock(cfg, app=self))
        except Exception as e:  # noqa: BLE001
            self._log_block_failure("preset", e)
        _mark("preset")

    def _wire_shims(self) -> None:
        # hook-based shims
        for block_type, mapping in _SHIM_MAP.items():
            block = self.registry.get(block_type)
            if not block:
                continue
            for hook_name, shim_name in mapping.items():
                hook = block.hook(hook_name)
                if hook:
                    self.shims[shim_name] = hook
        # public method entry-point shims (hotkeys etc.)
        for block_type, mapping in _PUBLIC_MAP.items():
            block = self.blocks.get(block_type)
            if block is None:
                continue
            for shim_name, method_name in mapping.items():
                method = getattr(block, method_name, None)
                if callable(method):
                    self.shims[shim_name] = method

    # -- boot --------------------------------------------------------------
    def boot(self, config_path: str | None = None) -> "Application":
        # 启动耗时打点：每完成一个阶段记一行到 launch.log（本段耗时 + 累计），
        # 用于定位「启动特别久」的热点。打点失败静默，不影响任何流程。
        _t0 = time.perf_counter()
        _state = {"last": _t0}

        def _stage(name: str) -> None:
            now = time.perf_counter()
            _boot_stage_log(
                f"{name} · 本段={now - _state['last']:.2f}s · 累计={now - _t0:.2f}s")
            _state["last"] = now

        # 自修复：清理上一版（被强杀）遗留的孤儿 PowerShell 监视器进程，
        # 避免它们累积占用性能计数器子系统、拖垮本次启动（详见
        # core/perf_probe.kill_orphaned_monitors）。
        try:
            from .core.perf_probe import kill_orphaned_monitors
            n = kill_orphaned_monitors()
            if n:
                print(f"[boot] 已清理 {n} 个遗留监视器进程")
        except Exception:
            pass
        _stage("清理孤儿监视器")
        self.load_config(config_path)
        _stage("load_config")
        # Load persisted button styles / L1 presets / binding map (if present).
        # Stored under <script_root>/buttons/*.json (mirrors button-editor.ps1).
        try:
            load_all_button_data(self.script_root)
        except Exception:
            pass
        _stage("load_button_data")
        self.nav = NavigationController(self.registry, self.config_manager, self)
        _stage("NavigationController")
        self.discover_builtin_blocks(_stage)
        for block_type in self.registry:
            block = self.registry.get(block_type)
            hook = block.hook("OnLoadConfig") if block else None
            if hook:
                try:
                    hook()
                except Exception:
                    pass
        _stage("OnLoadConfig 钩子(全部区块)")
        self._wire_shims()
        # RTSS 首次运行自部署 + 守护：后台线程跑（释放文件 + RTSS.exe /I 自注册
        # 可能耗时数秒），不阻塞启动。非 Windows 直接 no-op。部署完再启动守护兜底。
        try:
            rtss_block = self.blocks.get("rtss")
            if rtss_block is not None and hasattr(rtss_block, "ensure_rtss_async"):
                rtss_block.ensure_rtss_async()
        except Exception:
            pass
        _stage("wire_shims + RTSS 自部署调度")
        # 配置已通过 OnLoadConfig 载入内存并落盘。六档自动调度（Task A）已移除，
        # 改为「配置预设」纯手动应用，故启动不再广播 OnPowerSourceChange。
        try:
            self.save_all()
        except Exception:
            pass
        _stage("save_all")
        if not self.headless:
            self._build_gui()
        _stage("_build_gui（窗口+全部面板构建）")
        _boot_stage_log(
            f"boot 完成 · 总计 {time.perf_counter() - _t0:.2f}s")
        return self

    def _build_gui(self) -> None:
        # Imported lazily: requires PyQt6 + a Windows display.  Builds and shows
        # the real overlay window; the host's QApplication event loop must already
        # be running (see run.py).
        from .ui.app_window import build_overlay

        self._window = build_overlay(self)
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    # -- convenience delegation ------------------------------------------
    def switch_page(self, direction: int) -> None:
        # 单页时肩键/翻页键翻页是纯 no-op：不动导航状态、也不触发页重建回调，
        # 避免对当前唯一页面做任何无谓刷新（毕竟只有一页，没什么可翻的）。
        if self.nav and self.nav.total_pages() <= 1:
            return
        if self.nav:
            self.nav.switch_page(direction)
            if self._page_changed_cb:
                self._page_changed_cb()

    def goto_page(self, index: int) -> None:
        # 单页时页码按钮/数字键跳转同样是 no-op（见 switch_page 注释）。
        if self.nav and self.nav.total_pages() <= 1:
            return
        if self.nav:
            self.nav.goto_page(index)
            if self._page_changed_cb:
                self._page_changed_cb()

    def refresh_powerplan_if_visible(self) -> None:
        """进入电源计划卡片所在页 / 唤醒前台 / AC-DC 切换时刷新该卡片。

        统一入口：所有触发路径（导航漏斗、前台唤醒、电源事件）都收口到这里，
        仅当电源计划卡片在当前可见页才真正探测系统 + 重绘。异常一律吞掉，
        绝不因电源计划刷新失败而拖垮主流程。"""
        try:
            if self.nav is None:
                return
            if "powerplan" not in self.nav.current_page_cards():
                return
            blk = self.blocks.get("powerplan")
            if blk is not None and hasattr(blk, "refresh_and_sync"):
                blk.refresh_and_sync()
        except Exception:
            pass

    def switch_zone(self, direction: int) -> None:
        if self.nav:
            self.nav.switch_zone(direction)

    def enter_item_mode(self) -> None:
        if self.nav:
            self.nav.enter_item_mode()

    def exit_item_mode(self) -> None:
        if self.nav:
            self.nav.exit_item_mode()

    def move_item_focus(self, direction: int) -> None:
        if self.nav:
            self.nav.move_item_focus(direction)

    def save_all(self) -> None:
        # 重入保护（致命 boot 卡死的根因修复）。
        # 区块的 OnSaveConfig 钩子（如 电源计划/配置预设 的 save_config）曾直接回调
        # app.save_all，形成「save_all → 钩子 → save_all」无限递归；而上面的
        # try/except Exception 又把 RecursionError 吞掉、继续循环，导致 boot() 永不
        # 返回、进程卡在 "Starting NingBingJue..."。这里用 _saving 标志把重入调用变成
        # 安全 no-op，从根本上保证「单个区块的 save 实现有误也绝不会挂死整个程序」——
        # 这正是「功能卡片与主题解耦、新增功能不应崩溃凝冰结魄」的设计底线。
        if getattr(self, "_saving", False):
            return
        self._saving = True
        try:
            for block_type in self.registry:
                block = self.registry.get(block_type)
                hook = block.hook("OnSaveConfig") if block else None
                if hook:
                    try:
                        hook()
                    except Exception:
                        pass
            self.config_manager.save()
            # Persist the two-layer button preset system (L2 styles / L1 presets /
            # binding map) alongside layout.json — mirrors button-editor.ps1.
            try:
                save_all_button_data(self.script_root)
            except Exception:
                pass
        finally:
            self._saving = False

    def sync_all(self) -> None:
        """刷新所有卡片面板（落盘无关，纯 UI 重绘 + 值回填）。

        供「配置预设」「电源计划」这类跨卡片联动区块在应用预设后调用：先把新值
        写入各源区块（通过 OnApplyPreset 钩子），再 sync_all 让 power/rtss/fan/
        powerplan 各卡片立即反映新值，无需重启。GUI 缺失时（headless/测试）静默
        跳过。"""
        w = getattr(self, "_window", None)
        if w is not None and hasattr(w, "sync_all"):
            try:
                w.sync_all()
            except Exception:
                pass

    def fire_hook(self, name: str) -> list[tuple[str, Any]]:
        """统一触发所有区块的某个标准 hook，返回 [(block_type, result), ...]。

        用于 OnPowerResume 这类「广播」型 hook：休眠恢复后需要让每个曾下发过
        硬件配置的区块重新下发一次（SMU/EC 在休眠后会重置）。单个区块异常不
        影响其它区块。返回结果列表方便调用方做汇总反馈（如 toast）。
        """
        results: list[tuple[str, Any]] = []
        for block_type in self.registry:
            block = self.registry.get(block_type)
            hook = block.hook(name) if block else None
            if not hook:
                continue
            try:
                results.append((block_type, hook()))
            except Exception:
                results.append((block_type, None))
        return results

    # -- layout editing (the original's 编辑模式 / Build-EditLayout) -------
    def ordered_block_types(self) -> list[str]:
        """Visible block types in display order (config.Cards, falling back
        to registration order), excluding hidden cards.

        ``Cards`` is de-duplicated (the original ``layout.json`` can list a
        block type twice, e.g. a stray duplicate ``tile``), so the overlay
        never builds the same panel twice.
        """
        cfg = self.config_manager.config
        cards = list(dict.fromkeys(cfg.cards)) if cfg.cards else []
        for t in self.registry:
            if t not in cards:
                cards.append(t)
        hidden = set(getattr(cfg, "hidden_cards", []) or [])
        return [t for t in cards if t in self.registry and t not in hidden]

    def full_block_order(self) -> list[str]:
        """Every registered block type in display order (incl. hidden).

        Extra tile blocks (``config.extra_tile_blocks``) each get their own
        entry using ``tile#0`` / ``tile#1`` naming so the layout editor
        shows one row per block — matching the PS1 ``$global:AllCards``.
        """
        cfg = self.config_manager.config
        cards: list[str] = []
        seen = set()
        for c in (cfg.cards or []):
            if c not in seen:
                seen.add(c)
                cards.append(c)
        for t in self.registry:
            if t not in seen:
                seen.add(t)
                cards.append(t)
        result: list[str] = []
        for t in cards:
            if t not in self.registry:
                continue
            if t == "tile":
                result.append("tile")
                extra = getattr(cfg, "extra_tile_blocks", None) or []
                for i in range(len(extra)):
                    result.append(f"tile#{i}")
            else:
                result.append(t)
        return result

    def apply_layout(self, order: list[str], visible: dict[str, bool],
                     theme: str | None, style: str | None = None,
                     page_breaks: list[int] | None = None) -> None:
        """Persist a new layout (mirrors the original ``Save-Layout``).

        ``order`` is the full desired block order (may contain ``tile#N``
        entries for extra tile blocks).  ``visible`` maps each block type to
        whether it should be shown.  Theme / button style are applied and
        propagated to every block's ``OnThemeChange``.

        ``style`` is optional: when omitted (``None``) the button style is
        left untouched — the dedicated :class:`ButtonEditPanel` now owns
        button-style persistence independently.

        ``page_breaks`` (optional) are *expanded-order* page-start indices
        (the layout editor works in the same expanded space as
        ``full_block_order``).  They are normalised (deduped, forced to start
        at 0, clamped to the order length) before being stored, so the HUD's
        ``render_pages()`` always yields valid, non-overlapping segments.
        """
        cfg = self.config_manager.config
        # Normalise tile#N → tile for storage, and DE-DUPLICATE so ``tile``
        # appears exactly once.  The HUD expands it back into the base panel
        # + one panel per extra tile block (see ``render_pages`` / ``_build_panels``).
        seen: set[str] = set()
        contracted: list[str] = []
        for t in order:
            base = t.split("#")[0] if "#" in t else t
            if base in self.registry and base not in seen:
                seen.add(base)
                contracted.append(base)
        cfg.cards = contracted
        cfg.hidden_cards = [
            t for t in cfg.cards
            if not visible.get(t, True) and
            not any(visible.get(k, False) for k in visible if k.startswith(t + "#"))
        ]
        if style:
            cfg.button_style = style
        if page_breaks is not None:
            # Normalise in the expanded space (len(order) == expanded length).
            br = sorted({int(x) for x in page_breaks
                         if 0 <= int(x) <= len(order)})
            if not br or br[0] != 0:
                br = [0] + [b for b in br if b != 0]
            # 不封顶 + 自动消失空页：保存时彻底删掉会产生空页（0 张卡）的
            # 断点，保证持久化的布局里每个页面至少有 1 张卡片。
            br = pg_prune_empty_pages(len(order), br)
            cfg.page_breaks = br
        self.save_all()

    # -- page/zone view (single source of truth for HUD + editor) --------
    def render_pages(self) -> list[list[str]]:
        """Split the *expanded* block order into pages using ``page_breaks``.

        Returns a list of pages, each a list of tokens from
        ``full_block_order()`` (e.g. ``["freeze", "ls", "tile", "tile#0"]``).
        Pages are **不封顶** (no per-page cap) — a page can hold any number
        of cards; only the user-inserted breaks split pages. A page that ends
        up with 0 cards is dropped (never rendered as an empty page), keeping
        the HUD consistent with the layout editor.  This is what the main HUD
        renders and what the layout editor's triangle move buttons operate on.
        """
        order = self.full_block_order()
        if not order:
            return []
        # 主 HUD 不渲染被隐藏的卡片（编辑器仍走 full_block_order 展示全部行，
        # 隐藏行变暗）。先把分页断点索引从「含隐藏卡」的 full 顺序映射到「去掉
        # 隐藏卡」的可见顺序，使隐藏卡两侧的页面边界仍落在同一组卡片之间，
        # 不会因删除隐藏卡而整页错位。
        hidden = set(getattr(self.config_manager.config, "hidden_cards", []) or [])
        vis_idx: dict[int, int] = {}   # full 顺序位置 -> visible 顺序位置
        visible_order: list[str] = []
        for i, t in enumerate(order):
            if t in hidden:
                continue
            vis_idx[i] = len(visible_order)
            visible_order.append(t)
        cap = None  # 不封顶：不做自动拆分
        # Normalise breaks: dedupe, force start at 0, clamp to order length.
        raw = sorted({int(x) for x in self.config_manager.config.page_breaks
                      if 0 <= int(x) <= len(order)})
        if not raw or raw[0] != 0:
            raw = [0] + [b for b in raw if b != 0]
        # 把 full 顺序断点映射到 visible 顺序断点；落在隐藏卡上的断点顺延到其前
        # 最近的一个可见断点，保证不出现空页 / 整页错位。
        breaks: list[int] = [0]
        for b in raw[1:]:
            fb = vis_idx.get(b)
            if fb is None:
                fb = max((v for k, v in vis_idx.items() if k < b), default=0)
            if fb not in breaks:
                breaks.append(fb)
        breaks = sorted({b for b in breaks if b < len(visible_order)})
        if not breaks or breaks[0] != 0:
            breaks = [0] + breaks
        pages: list[list[str]] = []
        for i, b in enumerate(breaks):
            e = breaks[i + 1] if i + 1 < len(breaks) else len(visible_order)
            if b < e:   # 跳过零宽段（不该出现，但防御性兜底）
                pages.append(visible_order[b:e])
        return pages
