"""blocks/changelog.py — 更新内容区块。

展示「上一次打包 -> 本次打包之间」的更新说明。更新数据来自随包分发的
``changelog.json``（由 ``make_installer.py`` 在打包时生成，写入 ``_internal``）：
    {
      "version": "1.2.3",
      "build_time": "2026-08-24 16:30",
      "prev_version": "1.2.2",
      "entries": ["修复风扇控制失效", "新增任务列表卡片", ...]
    }

卡片 UI 见 ``ui/panels.py::ChangelogPanel``。用户首次看到新版本时卡片高亮，
关闭/查看后调用 ``mark_seen()`` 把 last_seen_version 记为当前版本。
"""
from __future__ import annotations

import json
import os
from typing import Any

from ..core.registry import Block

DEFAULT_COLOR = "#f5d74b"  # 纯暖黄（与 LS 编辑标题同色系，表「说明」语义）

CHANGELOG_FILENAME = "changelog.json"


def _resolve_changelog_path() -> str:
    env = os.environ.get("NBJ_CHANGELOG")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    # 候选顺序：
    #  1) 打包态 exe：<bundle>/_internal/changelog.json（here 在 _internal/ningbingjue/blocks）
    #  2) 开发态根：ningbingjue/changelog.json（项目随手放一份做本地预览）
    #  3) 兜底：src/changelog.json
    # 默认回退用开发态根（最贴近真实数据位置）。
    candidates = [
        os.path.join(here, "..", "..", "_internal", CHANGELOG_FILENAME),
        os.path.join(here, "..", "..", "..", CHANGELOG_FILENAME),
        os.path.join(here, "..", "..", CHANGELOG_FILENAME),
    ]
    for c in candidates:
        if os.path.exists(c):
            return os.path.abspath(c)
    return os.path.abspath(candidates[1])


class ChangelogBlock:
    """更新内容区块：只读外部 changelog.json + 记录已读版本。"""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.version: str = ""
        self.build_time: str = ""
        self.prev_version: str = ""
        self.entries: list[str] = []
        self.load_changelog()

    # -- 读取随包 changelog.json ---------------------------------------
    def load_changelog(self) -> None:
        path = _resolve_changelog_path()
        self._path = path
        self.version = ""
        self.build_time = ""
        self.prev_version = ""
        self.entries = []
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            self.version = str(data.get("version", "") or "")
            self.build_time = str(data.get("build_time", "") or "")
            self.prev_version = str(data.get("prev_version", "") or "")
            self.entries = [str(x) for x in (data.get("entries", []) or []) if str(x).strip()]
        except Exception:
            # 文件损坏不应让卡片崩溃，仅显示空。
            self.entries = []

    # -- 已读版本追踪 ---------------------------------------------------
    @property
    def is_new(self) -> bool:
        """当前版本是否比用户上一次见过的版本更新。"""
        last = getattr(self.config, "last_seen_version", "") or ""
        if not self.version:
            return False
        return last != self.version

    def mark_seen(self) -> None:
        """把当前版本记为「已读」，下次不再高亮。"""
        if self.version:
            self.config.last_seen_version = self.version

    def save_config(self) -> None:
        # 无自身持久化数据需要写；仅确保 last_seen_version 已随 config 落盘。
        # （config.last_seen_version 由 mark_seen 设置，app.save_all 统一保存。）
        pass

    def load_config(self) -> None:
        # 重新读取 changelog + 已读状态（版本可能随新包变化）。
        self.load_changelog()

    # -- 区块注册 -------------------------------------------------------
    def register(self, registry: Any) -> None:
        registry.register(Block(
            type="changelog",
            name="更新内容",
            color=DEFAULT_COLOR,
            card=None,
            hooks={
                "GetCardHeight": lambda: 200,
                "OnSaveConfig": self.save_config,
                "OnLoadConfig": self.load_config,
            },
        ))
