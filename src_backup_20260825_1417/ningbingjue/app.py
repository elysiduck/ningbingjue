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

import os
import sys
from typing import Any, Callable

from .blocks import (
    ChangelogBlock,
    FanBlock,
    FreezeBlock,
    LSBlock,
    PerfMonBlock,
    PowerBlock,
    ProcMonBlock,
    RtssBlock,
    SmtBlock,
    TaskListBlock,
    TileBlock,
    UndervoltBlock,
)
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
    def discover_builtin_blocks(self) -> None:
        cfg = self.config_manager.config
        pm = build_process_manager()

        # One ryzenadj instance shared by power + rtss (rtss reads the shared
        # MetricSampler for load, and the power block for the GPU auto-lock freq).
        ryzenadj = build_ryzenadj(self.script_root)

        # power 块先建：rtss 自动锁帧需要读取 power.gpu_current_freq
        # (GPU 自动锁频 MHz) 作为判定信号之一。
        power = PowerBlock(cfg, ryzenadj)
        power.register(self.registry)
        self.blocks["power"] = power

        # GPU 自动锁频是否落到地板：直接注入布尔，消除 rtss 内硬编码的 400
        # 魔法数（地板 = power.GPU_MIN，改 GPU_MIN 时自动跟随）。CPU 占用 & GPU
        # 负载仍走全应用唯一的共享指标器 MetricSampler，不在此另起采样。
        gpu_freq_provider: Callable[[], bool] | None = None
        if sys.platform == "win32":
            gpu_freq_provider = lambda: power.gpu_current_freq <= PowerBlock.GPU_MIN

        # 游戏白名单：复用冻结卡片「标为游戏」维护的 undervolt 白名单，使自动
        # 锁帧能识别 ZZZ 等漏网游戏（game_ok 判定）。运行时 self 为 MainWindow
        # 实例时才有该方法，故用 hasattr 保护，避免基类/测试环境 AttributeError。
        game_whitelist_provider: Callable[[], list[str]] | None = None
        if hasattr(self, "undervolt_get_whitelist"):
            game_whitelist_provider = lambda: self.undervolt_get_whitelist()

        rtss = RtssBlock(
            build_rtss_cli(self.script_root), self.script_root,
            gpu_freq_provider=gpu_freq_provider,
            game_whitelist_provider=game_whitelist_provider,
        )
        rtss.register(self.registry)
        self.blocks["rtss"] = rtss

        procmon = ProcMonBlock(cfg, pm)
        procmon.register(self.registry)
        self.blocks["procmon"] = procmon

        freeze = FreezeBlock(self.script_root, pm, vdesktop=build_virtual_desktop(self.script_root), config=cfg)
        freeze.register(self.registry)
        self.blocks["freeze"] = freeze

        ls = LSBlock(build_lossless(self.script_root))
        ls.register(self.registry)
        self.blocks["ls"] = ls

        tile = TileBlock(cfg, pm)
        tile.register(self.registry)
        self.blocks["tile"] = tile

        # 降压控制：轻量虚拟区块（真正的 UndervoltController 由主窗口装配）。
        # 放在 tile 之后注册，卡片顺序上落在最后，用户可在布局编辑器里自由拖动。
        undervolt = UndervoltBlock()
        undervolt.register(self.registry)
        self.blocks["undervolt"] = undervolt

        # 性能监控：轻量虚拟区块（只读实时聚合，由主窗口 perfmon_snapshot 提供）。
        # 紧跟 undervolt 注册，卡片顺序上落在末尾。
        perfmon = PerfMonBlock()
        perfmon.register(self.registry)
        self.blocks["perfmon"] = perfmon

        # SMT 热核隔离：真实区块（拓扑识别 + CPU 集合施加）。
        # 放在 perfmon 之后注册，卡片顺序上落在末尾，用户可在布局编辑器里自由拖动。
        smt = SmtBlock(cfg, pm)
        smt.register(self.registry)
        self.blocks["smt"] = smt

        # 风扇控制：移植 MotionAssistant 的 EC 路径，走 WinRing0 + 跨进程互斥。
        # 紧跟 power 注册：与电源设置语义相关；用户可在布局编辑器里自由拖动。
        # EC 写入失败（Core Isolation / 非管理员）→ 卡片仍显示，写入静默跳过。
        fan = FanBlock(cfg)
        fan.register(self.registry)
        self.blocks["fan"] = fan

        # 任务列表：主页直接增删 + agent 自动填写。轻量真实区块（持有任务列表
        # 内存态 + 配置持久化）。
        tasklist = TaskListBlock(cfg)
        tasklist.register(self.registry)
        self.blocks["tasklist"] = tasklist

        # 更新内容：展示上一次打包 -> 本次打包之间的更新。只读外部 changelog.json。
        changelog = ChangelogBlock(cfg)
        changelog.register(self.registry)
        self.blocks["changelog"] = changelog

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
        self.load_config(config_path)
        # Load persisted button styles / L1 presets / binding map (if present).
        # Stored under <script_root>/buttons/*.json (mirrors button-editor.ps1).
        try:
            load_all_button_data(self.script_root)
        except Exception:
            pass
        self.nav = NavigationController(self.registry, self.config_manager, self)
        self.discover_builtin_blocks()
        for block_type in self.registry:
            block = self.registry.get(block_type)
            hook = block.hook("OnLoadConfig") if block else None
            if hook:
                try:
                    hook()
                except Exception:
                    pass
        self._wire_shims()
        if not self.headless:
            self._build_gui()
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
        if self.nav:
            self.nav.switch_page(direction)
            if self._page_changed_cb:
                self._page_changed_cb()

    def goto_page(self, index: int) -> None:
        if self.nav:
            self.nav.goto_page(index)
            if self._page_changed_cb:
                self._page_changed_cb()

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
        cap = None  # 不封顶：不做自动拆分
        # Normalise breaks: dedupe, force start at 0, clamp to order length.
        raw = sorted({int(x) for x in self.config_manager.config.page_breaks
                      if 0 <= int(x) <= len(order)})
        if not raw or raw[0] != 0:
            raw = [0] + [b for b in raw if b != 0]
        breaks: list[int] = []
        for i, b in enumerate(raw):
            s = b
            e = raw[i + 1] if i + 1 < len(raw) else len(order)
            breaks.append(s)
            if cap:
                p = s + cap
                while p < e:
                    breaks.append(p)
                    p += cap
        breaks = sorted({b for b in breaks if b < len(order)})
        if not breaks or breaks[0] != 0:
            breaks = [0] + breaks
        pages: list[list[str]] = []
        for i, b in enumerate(breaks):
            e = breaks[i + 1] if i + 1 < len(breaks) else len(order)
            if b < e:   # 跳过零宽段（不该出现，但防御性兜底）
                pages.append(order[b:e])
        return pages
