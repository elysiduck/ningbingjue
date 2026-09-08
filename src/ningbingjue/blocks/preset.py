"""配置预设区块（凝冰结魄新增的「配置预设」卡片大脑）。

这是 YeManCC「档位」概念在凝冰结魄里的终极形态：不再是一组写死的六档，而是用户
自己「调好各卡片 → 一键存成预设」的自由预设系统。

数据来源：预设**不持有任何硬件状态**，它只是把四个源区块（power / rtss / fan /
powerplan）在「新建预设」那一刻的快照值打包存起来。四个源区块各自通过
``OnExportPreset`` 钩子吐出自己的当前值，本区块负责拼装、命名、持久化；套用时再
通过各自的 ``OnApplyPreset`` 钩子把值写回去，最后 ``app.sync_all()`` 让所有卡片
立即反映新值。

预设命名（用户钦定格式，按组合值自动生成，无需手填）：
    TDP 限制量（带单位） - 电源计划名称 - AT限制帧数（fps） - GPU浮动上限 - 风扇档位
例如：``10W-YMPerformance-60fps-700MHz-手动60%``
未开启的维度（AT 未开 / GPU 浮动未开）对应段留空，但仍保留「-」分隔位。
"""

from __future__ import annotations
from ..ui.panels import _show_block_toast

from typing import Any

from ..core.config import AppConfig, PresetSpec, PresetsConfig
from .base import BlockBlueprint

PRESET_COLOR = "#7ad0ff"  # 冷青色，表「配置/预设」语义


class PresetBlock:
    """配置预设容器区块（真实区块：持有预设列表 + 跨卡片编排）。"""

    def __init__(self, config: "AppConfig", app: Any = None) -> None:
        self.config: "AppConfig" = config
        self.app = app
        self.ui: dict[str, Any] = {}
        # AppConfig 已保证 presets_config 存在（默认工厂），这里做个防御。
        if getattr(self.config, "presets_config", None) is None:
            self.config.presets_config = PresetsConfig()

    # -- 快照源区块 ----------------------------------------------------
    def _snap(self, key: str) -> dict[str, Any]:
        """调用某源区块的 OnExportPreset 钩子取当前值；区块/钩子缺失则返回空。"""
        blk = self.app.blocks.get(key) if self.app else None
        fn = getattr(blk, "on_export_preset", None) if blk else None
        try:
            return fn() if callable(fn) else {}
        except Exception:
            return {}

    def _apply_to(self, key: str, data: dict[str, Any]) -> None:
        """调用某源区块的 OnApplyPreset 钩子回填值；异常不影响其它区块。"""
        blk = self.app.blocks.get(key) if self.app else None
        fn = getattr(blk, "on_apply_preset", None) if blk else None
        if callable(fn):
            try:
                fn(data)
            except Exception:
                pass

    # -- 命名 ----------------------------------------------------------
    @staticmethod
    def _build_name(power: dict, rtss: dict, fan: dict, pp: dict) -> str:
        """按用户钦定格式拼装预设名：TDP - 电源计划 - AT帧数 - GPU浮动 - 风扇。

        未开启的维度留空段（仍保留 '-' 分隔位）。同名自动加 ' #n' 后缀去重。"""
        parts: list[str] = []
        parts.append(f"{int(power.get('tdp', 0))}W")
        parts.append(str(pp.get("name", "") or ""))
        if rtss.get("at_enabled"):
            parts.append(f"{int(rtss.get('at_fps', 0))}fps")
        else:
            parts.append("")
        if power.get("gpu_float"):
            parts.append(f"{int(power.get('gpu', 0))}MHz")
        else:
            parts.append("")
        parts.append(str(fan.get("label", "") or ""))
        name = "-".join(parts)
        return name

    def _dedup_name(self, name: str) -> str:
        existing = {p.name for p in self.config.presets_config.presets}
        if name not in existing:
            return name
        n = 2
        while f"{name} #{n}" in existing:
            n += 1
        return f"{name} #{n}"

    # -- 增 / 套用 / 删 ----------------------------------------------
    def add_preset_from_current(self) -> PresetSpec | None:
        """把当前各卡片的值存成一个新预设，自动命名并落盘。"""
        power = self._snap("power")
        rtss = self._snap("rtss")
        fan = self._snap("fan")
        pp = self._snap("powerplan")
        raw = self._build_name(power, rtss, fan, pp)
        name = self._dedup_name(raw)
        spec = PresetSpec(name=name, power=power, rtss=rtss, fan=fan, powerplan=pp)
        self.config.presets_config.presets.append(spec)
        if self.app is not None and hasattr(self.app, "save_all"):
            try:
                self.app.save_all()
            except Exception:
                pass
        self._sync_ui()
        self.show_toast(f"✓ 已保存预设：{name}", success=True)
        return spec

    def apply_preset(self, index: int) -> bool:
        """套用第 index 个预设：把各维度值写回源区块，刷新所有卡片。"""
        presets = self.config.presets_config.presets
        if not (0 <= index < len(presets)):
            return False
        spec = presets[index]
        self._apply_to("power", spec.power)
        self._apply_to("rtss", spec.rtss)
        self._apply_to("fan", spec.fan)
        self._apply_to("powerplan", spec.powerplan)
        if self.app is not None and hasattr(self.app, "save_all"):
            try:
                self.app.save_all()
            except Exception:
                pass
        self._sync_ui()
        self.show_toast(f"✓ 已套用预设：{spec.name}", success=True)
        return True

    def remove_preset(self, index: int) -> bool:
        """删除第 index 个预设。"""
        presets = self.config.presets_config.presets
        if not (0 <= index < len(presets)):
            return False
        name = presets[index].name
        del presets[index]
        if self.app is not None and hasattr(self.app, "save_all"):
            try:
                self.app.save_all()
            except Exception:
                pass
        self._sync_ui()
        self.show_toast(f"✓ 已删除预设：{name}", success=True)
        return True

    # -- UI 联动 -------------------------------------------------------
    def _sync_ui(self) -> None:
        if self.app is not None and hasattr(self.app, "sync_all"):
            try:
                self.app.sync_all()
            except Exception:
                pass

    def save_config(self) -> None:
        """预设已就地写入 config.presets_config.presets，落盘由统一的 app.save_all()
        负责。与电源计划同理：本方法**不再**回调 app.save_all（避免重入死循环），作为
        OnSaveConfig 钩子为空操作；需要立即落盘时由调用方（add_preset_from_current /
        apply_preset / remove_preset）直接调 self.app.save_all()。"""
        # 内存态已在调用方就地修改；app.save_all 统一持久化。故意不回调，避免重入。
        return

    def load_config(self) -> None:
        """预设数据来自 config，无需重载；预留钩子保持一致。"""
        pass

    # -- toast（PyQt6 版） -------------------------------------------
    def show_toast(self, text: str, success: bool = True) -> None:
        _show_block_toast(self.ui, text, success)

    # -- 注册 ----------------------------------------------------------
    def register(self, registry: Any) -> None:
        registry.register(BlockBlueprint(
            block_type="preset",
            name="配置预设",
            color=PRESET_COLOR,
            card=None,
            hooks={
                "GetCardHeight": lambda: 240,
                "OnSaveConfig": self.save_config,
                "OnLoadConfig": self.load_config,
            },
        ).to_block())
