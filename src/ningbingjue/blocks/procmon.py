"""procmon block — monitor & toggle external scripts/processes.

Faithful logic port of ``blocks/procmon.ps1``.  Running-state is detected
by matching each config path against enumerated process command lines
(the original mapped by extension → host process → CIM CommandLine;
here we match the config path / executable basename, which is equivalent
for the observable behavior).  ``ui`` carries the (optional) widget objects;
``self.buttons`` is the rendered button list the GUI mirrors.

AutoHotkey (``.ahk``) support: a script never runs as its own process — it is
hosted by ``AutoHotkey*.exe``, with the script path passed as a command-line
argument.  Detection therefore matches the script path/name inside the host's
command line (see :func:`ningbingjue.platform.process_manager.match_running`),
and launching goes through the AutoHotkey interpreter so the path is recorded.
"""

from __future__ import annotations
from ..ui.panels import _show_block_toast

import time
from typing import Any

try:  # PyQt6 提供 sip.isdeleted 用于判断 C++ 对象是否已被销毁
    from PyQt6 import sip as _sip
except Exception:  # pragma: no cover - headless 环境无 PyQt6
    _sip = None


def _widget_alive(obj: Any) -> bool:
    """返回 Qt 对象是否仍可安全访问（未被 C++ 侧销毁）。

    已销毁的 QWidget / QObject 一旦被 Python 侧访问（如读取属性、发信号）
    会触发 PyQt6 的未定义行为并直接让进程崩溃（闪退）。``sip.isdeleted`` 是
    唯一能在访问前安全探活的手段。无 PyQt6 的环境（headless 测试）一律视为
    存活，由调用方自行保证对象有效。
    """
    if obj is None:
        return False
    if _sip is None:
        return True
    try:
        return not _sip.isdeleted(obj)
    except Exception:
        return False

from ..core.color.api import get_button_state
from ..core.color.palette import COLOR_CYAN
from ..core.color.rgb import Color
from ..core.color.semantic import get_theme_accent
from ..core.navigation import (
    FocusHolder,
    clear_block_focus,
    set_control_focus_visual,
)

from ..platform.process_manager import ProcessManager, match_running
from .base import BlockBlueprint


class _Btn:
    """Minimal headless button stand-in (mirrored by the real GUI)."""

    def __init__(self) -> None:
        self.text = ""
        self._colors: tuple | None = None
        self.focused = False
        self.fore_color: Color | None = None

    def set_colors(self, *args: Any) -> None:
        self._colors = tuple(args)

    def set_focus_visual(self, focused: bool) -> None:
        self.focused = focused

    def invalidate(self) -> None:
        pass


class ProcMonBlock:
    THROTTLE_S = 0.5
    CACHE_TTL_S = 10
    BTN_H = 42
    GAP = 4
    MIN_CARD_H = 160
    TITLE_H = 60
    BOTTOM_H = 44
    MIN_GRID = 60

    def __init__(self, config: Any, process_manager: ProcessManager, ui: dict[str, Any] | None = None) -> None:
        self.config = config
        self.pm = process_manager
        self.ui: dict[str, Any] = dict(ui or {})
        self.theme_color = get_theme_accent()
        self.configs: list[dict[str, Any]] = self._load_from_config()
        self.buttons: list[_Btn] = []
        self.focus_idx = FocusHolder(-1)
        self.in_item_mode = False
        self.edit_mode = False
        self.edit_controls: Any = None
        self._cache_ts = 0.0
        self._cache: list[Any] = []
        self._last_count = -1

    # -- config persistence ----------------------------------------------
    def _load_from_config(self) -> list[dict[str, Any]]:
        raw = getattr(self.config, "procmon_configs", None)
        if isinstance(raw, dict):
            return [{"Name": k, "Path": (v or {}).get("Path", ""), "Running": False}
                    for k, v in raw.items()]
        if isinstance(raw, list):
            return [{"Name": c.get("Name", ""), "Path": c.get("Path", ""), "Running": False}
                    for c in raw]
        return []

    def save_config(self) -> list[dict[str, Any]]:
        # Original OnSaveConfig returns [{Name, Path}] (Running omitted).
        data = [{"Name": c["Name"], "Path": c["Path"]} for c in self.configs]
        self.config.procmon_configs = data
        return data

    def load_config(self) -> None:
        self.configs = self._load_from_config()

    # -- running detection ----------------------------------------------
    @staticmethod
    def detect_running(cfg: dict[str, Any], processes: list[Any]) -> bool:
        """True if the configured target is currently running.

        Delegates to :func:`match_running`, which is AHK-aware: an ``.ahk``
        script runs under an ``AutoHotkey*.exe`` host, so its image name is
        never the script — the match is done against the script path/name in
        the host's command line (with separator normalization).
        """
        return match_running(cfg.get("Path") or "", processes)

    # -- rendering -------------------------------------------------------
    def rebuild_buttons(self) -> None:
        provided = self.ui.get("buttons")
        if isinstance(provided, list) and len(provided) == len(self.configs):
            self.buttons = list(provided)
        else:
            self.buttons = [_Btn() for _ in self.configs]
        self._last_count = len(self.configs)

    def _button_text(self, cfg: dict[str, Any]) -> str:
        icon = "●" if cfg.get("Running") else "○"
        # 空名称（新建尚未命名的监控项）回退到占位名，避免按钮显示裸图标
        # 「○  」（原 #P2-10）。
        name = (cfg.get("Name") or "").strip() or "监控项"
        return f"{icon} {name}"

    def _button_state(self, cfg: dict[str, Any]) -> dict[str, Any]:
        if cfg.get("Running"):
            return get_button_state(self.theme_color, "solid")
        return get_button_state(style="muted")

    def _render(self) -> None:
        for i, cfg in enumerate(self.configs):
            if i >= len(self.buttons):
                break
            b = self.buttons[i]
            b.text = self._button_text(cfg)
            st = self._button_state(cfg)
            if hasattr(b, "set_colors"):
                b.set_colors(st["Back"], st["Hover"], st["Border"], st["Fore"])

    def refresh_panel(self, now: float | None = None) -> None:
        """``OnRefresh`` — throttled full refresh."""
        now = time.time() if now is None else now
        if now - self._cache_ts < self.THROTTLE_S and self._cache:
            return
        procs = self.pm.enumerate() if self.pm.available else []
        self._cache = procs
        self._cache_ts = now
        if len(self.configs) != self._last_count:
            self.rebuild_buttons()
        for cfg in self.configs:
            cfg["Running"] = self.detect_running(cfg, procs)
        self._render()

    def layout_panel(self) -> None:
        """``OnLayout`` — reposition controls for the current card width."""
        # Headless: the GUI mirrors self.buttons; nothing to recompute here.
        self._render()

    def _card_height(self) -> int:
        rows = max(1, len(self.configs))
        return self.TITLE_H + rows * (self.BTN_H + self.GAP) + self.BOTTOM_H + self.MIN_GRID

    # -- theme ----------------------------------------------------------
    def _set_title_color(self, color: Color) -> None:
        title = self.ui.get("title")
        if title is not None:
            title.fore_color = color

    def on_theme_change(self, accent: dict[str, Any]) -> None:
        self.theme_color = get_theme_accent()
        self._set_title_color(self.theme_color)
        self._render()

    # -- toggle ----------------------------------------------------------
    def toggle_config(self, index: int) -> None:
        if index < 0 or index >= len(self.configs):
            return
        cfg = self.configs[index]
        if cfg.get("Running"):
            # Kill by matching the target path in the host's command line
            # (faithful to the original Toggle-ProcMonScript stop branch).
            if self.pm.available and cfg.get("Path"):
                self.pm.stop_matching(cfg["Path"])
            cfg["Running"] = False
            self.show_toast(f"正在关闭 {cfg['Name']} …", success=True)
        else:
            ok = False
            if self.pm.available and cfg.get("Path"):
                ok = self.pm.start(cfg["Path"])
            if ok:
                # optimistic: the background refresh re-detects and corrects shortly.
                cfg["Running"] = True
                self.show_toast(f"正在打开 {cfg['Name']} …", success=True)
            else:
                # 进程打开失败：保持未激活状态，按钮依旧标为未激活，并给出失败提示。
                cfg["Running"] = False
                self.show_toast(f"打开失败：{cfg['Name']}", success=False)
        self._render()

    # -- edit: add / remove monitored scripts --------------------------
    def add_config(self, name: str, path: str) -> None:
        # 注意：不要把空名称回退成「新脚本」。新建监控进程要求「进程属性啥都
        # 不要有」，空名称必须原样保留（""），显示层的占位符（按钮「监控项」、
        # 列表「(新监控项)」）负责兜底呈现，数据层面保持诚实。
        self.configs.append({"Name": name, "Path": path, "Running": False})
        self.save_config()
        self.refresh_panel()

    def remove_config(self, index: int) -> None:
        if 0 <= index < len(self.configs):
            self.configs.pop(index)
            if self.focus_idx.value >= len(self.configs):
                self.focus_idx.value = max(-1, len(self.configs) - 1)
            self.save_config()
            self.refresh_panel()

    # -- item-mode navigation -------------------------------------------
    def on_activate_item(self) -> None:
        self.in_item_mode = True
        self.focus_idx.value = 0
        if self.buttons:
            set_control_focus_visual(self.buttons[0], True)

    def on_deactivate_item(self) -> None:
        self.in_item_mode = False
        clear_block_focus([b for b in self.buttons if b is not None])
        self.focus_idx.value = -1

    # -- edit mode ------------------------------------------------------
    def on_edit_layout(self) -> None:
        self.edit_mode = True
        self._build_edit_controls()

    def on_enter_edit_mode(self) -> None:
        self.edit_mode = True

    def on_exit_edit_mode(self) -> None:
        self.edit_mode = False
        self.edit_controls = None

    def on_check_edit_mode(self) -> bool:
        return self.edit_mode

    def _build_edit_controls(self) -> None:
        # Headless: record the edit-state flag; the GUI builds the picker.
        self.edit_controls = [{"Name": c["Name"], "Path": c["Path"]} for c in self.configs]

    # -- toast ----------------------------------------------------------
    def show_toast(self, text: str, success: bool = True) -> None:
        _show_block_toast(self.ui, text, success)

    # -- registration ----------------------------------------------------
    def register(self, registry: Any) -> None:
        bp = BlockBlueprint(
            block_type="procmon",
            name="► 进程监控",
            color=COLOR_CYAN,
            card=self.ui.get("card"),
            hooks={
                "GetCardHeight": lambda: max(self.MIN_CARD_H, self._card_height()),
                "OnRefresh": self.refresh_panel,
                "OnLayout": self.layout_panel,
                "OnSyncTitleColor": self._set_title_color,
                "OnThemeChange": self.on_theme_change,
                "OnDeactivateItem": self.on_deactivate_item,
                "OnSaveConfig": self.save_config,
                "OnLoadConfig": self.load_config,
                "OnEditLayout": self.on_edit_layout,
                "OnEnterEditMode": self.on_enter_edit_mode,
                "OnExitEditMode": self.on_exit_edit_mode,
                "OnCheckEditMode": self.on_check_edit_mode,
                "OnActivateItem": self.on_activate_item,
            },
        )
        registry.register(bp.to_block())

    def status(self) -> str:
        """Short live readout for the smoke report / GUI panel."""
        if not self.configs:
            return "无配置"
        running = sum(1 for c in self.configs if c.get("Running"))
        return f"{running}/{len(self.configs)} 运行中"