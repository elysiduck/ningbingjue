"""ls block — Lossless Scaling preset manager.

Faithful logic port of ``blocks/ls.ps1``.  The block only *manages*
presets: it scans the LS presets directory, shows running state via
process detection, parses the active ``settings.xml`` for scale / frame-gen /
perf-mode, and loads a selected preset (copy into the active config).  It
does NOT start/stop the LS process itself.  All I/O goes through the
``LosslessScaling`` adapter (real file/process ops on host, Mock in tests).
"""

from __future__ import annotations


import time
from typing import Any

from ..core.color.api import get_button_state
from ..core.color.palette import COLOR_GREEN, COLOR_YELLOW
from ..core.color.rgb import Color
from ..core.color.semantic import get_theme_accent
from ..core.navigation import (
    FocusHolder,
    clear_block_focus,
    set_control_focus_visual,
)

from ..platform.lossless_scaling import LosslessScaling
from .base import BlockBlueprint


class _Btn:
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


class LSBlock:
    BTN_H = 28
    BTN_GAP = 2
    TITLE_H = 78
    BOTTOM_H = 14
    ROW_PAD = 12
    ACTION_H = 40
    MIN_ROWS = 3
    MAX_ROWS = 8

    def __init__(self, ls: LosslessScaling, ui: dict[str, Any] | None = None) -> None:
        self.ls = ls
        self.ui: dict[str, Any] = dict(ui or {})
        self.theme_color = get_theme_accent()
        self.presets: list[str] = []
        self.focus_type: str = "list"
        self.focus_idx = FocusHolder(0)
        self.in_item_mode = False
        self.preset_btns: list[_Btn] = []
        self.btn_load = _Btn()
        self.btn_save = _Btn()
        self.btn_delete = _Btn()
        self._status: dict[str, str] = {
            "ScalingType": "关闭", "FrameGeneration": "关闭", "PerfMode": "关闭",
        }
        self._profile = "未追踪"
        self.refresh_panel()

    # -- refresh -------------------------------------------------------
    def refresh_panel(self) -> None:
        if self.ls.available:
            self.presets = self.ls.list_presets()
            self._status = self.ls.read_status()
            self._profile = self.ls.current_profile()
        self._render()

    def layout_panel(self) -> None:
        self._render()

    # -- rendering ------------------------------------------------------
    def _rebuild_preset_btns(self) -> None:
        if len(self.preset_btns) != len(self.presets):
            self.preset_btns = [_Btn() for _ in self.presets]
            if self.focus_idx.value >= len(self.presets):
                self.focus_idx.value = 0

    def _render(self) -> None:
        self._rebuild_preset_btns()
        for i, name in enumerate(self.presets):
            b = self.preset_btns[i]
            active = (name == self._profile)
            b.text = f"F{i + 1}  {name}" + ("  ✓" if active else "")
            st = get_button_state(COLOR_GREEN, "solid") if active else get_button_state(self.theme_color, "frosted")
            b.set_colors(st["Back"], st["Hover"], st["Border"], st["Fore"])
        # action buttons
        for b in (self.btn_load, self.btn_save, self.btn_delete):
            b.set_colors(*[self.theme_color] * 4)
        # status label（只显示 LS 预设配置，不显示运行态——启动就是朴素地打开 exe）
        lbl = self.ui.get("status_label")
        if lbl is not None:
            scale = self._status.get("ScalingType", "关闭")
            fg = self._status.get("FrameGeneration", "关闭")
            perf = self._status.get("PerfMode", "关闭")
            lbl.text = f"LS 预设 · 缩放: {scale} · 帧生成: {fg} · 性能: {perf}"
        self._apply_focus_visual()

    def _active_control(self) -> _Btn | None:
        if self.focus_type == "list":
            if 0 <= self.focus_idx.value < len(self.preset_btns):
                return self.preset_btns[self.focus_idx.value]
            return None
        return {"btnLoad": self.btn_load, "btnSave": self.btn_save,
                "btnDelete": self.btn_delete}.get(self.focus_type)

    def _apply_focus_visual(self) -> None:
        clear_block_focus([b for b in self.preset_btns + [self.btn_load, self.btn_save, self.btn_delete] if b is not None])
        c = self._active_control()
        if c is not None:
            set_control_focus_visual(c, True)

    # -- LS 主程序启动 / 路径 --------------------------------------------
    @property
    def exe_path(self) -> str:
        return self.ls.exe_path

    def set_exe_path(self, path: str) -> None:
        self.ls.set_exe_path(path)

    def auto_detect_exe_path(self) -> bool:
        """扫描当前 LS 进程并写入路径；扫不到返回 False 且保持空。"""
        found = self.ls.detect_exe_path()
        if found:
            self.ls.set_exe_path(found)
            return True
        return False

    def launch_ls(self) -> str:
        """拉起 Lossless Scaling；返回 key（launched/nopath/fail）供 UI 提示。

        空路径不尝试启动，由调用方带进设置面板。"""
        ok, key = self.ls.launch()
        if key == "nopath":
            self.show_toast("未设置 LS 路径，请到设置中自动设置或浏览", success=False)
        elif key == "fail":
            self.show_toast("启动 LS 失败，请检查路径或手动启动", success=False)
        elif key == "launched":
            self.show_toast("已启动 Lossless Scaling")
        elif key == "nostart":
            self.show_toast("已发送启动指令，但 LS 未出现（确认 Steam 是否在运行 / 是否缺运行库）", success=False)
        self.refresh_panel()
        return key

    def startup_detect(self) -> None:
        """启动钩子：仅当路径「从未设置」（AppData 里完全是空）时，才扫描一次 LS
        进程写入路径做一次性引导；一旦用户手动设过路径（无论当前是否有效），绝不再
        自动覆盖——避免掌机上「手动设的路径被自动探测覆盖 / 一拉源码就变」。"""
        try:
            if self.ls.exe_path:
                return  # 用户已设过路径，保留，绝不覆盖
            self.auto_detect_exe_path()  # 仅从未设置时才引导
        except Exception:
            pass

    # -- actions --------------------------------------------------------
    def _load_by_name(self, name: str) -> None:
        try:
            idx = self.presets.index(name)
        except ValueError:
            return
        self._load(idx)

    def _load(self, index: int) -> None:
        if 0 <= index < len(self.presets):
            name = self.presets[index]
            ok = self.ls.load_preset(name)
            if not ok:
                self.show_toast(f"加载失败: {name}", success=False)
                return
            # LS 只在启动时读一次 settings.xml；预设改了必须「先关后开」才生效。
            note = ""
            if self.ls.is_running():
                self.ls.close()            # ① 先关闭运行中的 LS
                time.sleep(0.4)            #   给进程退出留时间，避免单实例窗口残留致重启失败
                l_ok, key = self.ls.launch()
                if key == "launched":
                    note = " · 已重启 LS"
                elif key == "nopath":
                    self.show_toast(f"已加载预设 {name}，但重启 LS 失败：未设置路径", success=False)
                    self._profile = name
                    self.refresh_panel()
                    return
                else:
                    self.show_toast(f"已加载预设 {name}，但重启 LS 失败", success=False)
                    self._profile = name
                    self.refresh_panel()
                    return
            self._profile = name
            self.show_toast(f"已加载预设 {name}{note}")
            self.refresh_panel()

    def _save(self) -> None:
        name = self._profile if self._profile not in ("未追踪", "读取失败") else (
            self.presets[self.focus_idx.value] if 0 <= self.focus_idx.value < len(self.presets) else "")
        ok = self.ls.save_preset(name) if name else False
        self.show_toast(f"已保存预设 {name}" if ok else "保存失败", success=ok)
        self.refresh_panel()

    def _delete(self, index: int) -> None:
        if 0 <= index < len(self.presets):
            name = self.presets[index]
            ok = self.ls.delete_preset(name)
            self.show_toast(f"已删除预设 {name}" if ok else "删除失败", success=ok)
            self.refresh_panel()

    # -- height --------------------------------------------------------
    def _card_height(self) -> int:
        rows = max(self.MIN_ROWS, min(self.MAX_ROWS, len(self.presets)))
        return self.TITLE_H + rows * (self.BTN_H + self.BTN_GAP) + self.ROW_PAD + self.ACTION_H + self.BOTTOM_H

    # -- theme ---------------------------------------------------------
    def _set_title_color(self, color: Color) -> None:
        title = self.ui.get("title")
        if title is not None:
            title.fore_color = color

    def on_theme_change(self, accent: dict[str, Any]) -> None:
        self.theme_color = get_theme_accent()
        self._set_title_color(self.theme_color)
        self._render()

    # -- item-mode navigation -------------------------------------------
    def on_activate_item(self) -> None:
        self.in_item_mode = True
        self.focus_type = "list"
        self.focus_idx.value = 0
        self._apply_focus_visual()

    def on_deactivate_item(self) -> None:
        self.in_item_mode = False
        clear_block_focus([b for b in self.preset_btns + [self.btn_load, self.btn_save, self.btn_delete] if b is not None])

    # -- toast ---------------------------------------------------------
    def show_toast(self, text: str, success: bool = True) -> None:
        panel = self.ui.get("toast_panel")
        label = self.ui.get("toast_label")
        if panel is not None and hasattr(panel, "set_tone"):
            panel.set_tone(success)
            panel.visible = True
        if label is not None:
            label.text = text
            label.fore_color = Color.from_rgb(0, 230, 120) if success else Color.from_rgb(255, 80, 80)

    # -- registration ----------------------------------------------------
    def register(self, registry: Any) -> None:
        bp = BlockBlueprint(
            block_type="ls",
            name="► LS预设",
            color=COLOR_YELLOW,
            card=self.ui.get("card"),
            hooks={
                "GetCardHeight": lambda: max(160, self._card_height()),
                "OnRefresh": self.refresh_panel,
                "OnLayout": self.layout_panel,
                "OnSyncTitleColor": self._set_title_color,
                "OnThemeChange": self.on_theme_change,
                "OnActivateItem": self.on_activate_item,
                "OnDeactivateItem": self.on_deactivate_item,
            },
        )
        registry.register(bp.to_block())

    def status(self) -> str:
        """Short live readout for the smoke report / GUI panel."""
        return f"LS 预设 · {self._profile} · 缩放 {self._status.get('ScalingType', '关闭')}"