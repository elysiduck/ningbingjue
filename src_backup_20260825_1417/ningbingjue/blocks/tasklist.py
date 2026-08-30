"""blocks/tasklist.py — 任务列区块。

主页可直接手动添加 / 删除任务；agent 完成任务后（如「查看 logs」）也可调用
``add_task(source="agent")`` 自动追加一行。所有任务持久化到 ``layout.json`` 的
``TaskListConfig``，由 app.save_all 统一落盘。

卡片 UI 见 ``ui/panels.py::TaskListPanel``（主页内联输入框 + 增删列表）。

agent 自动填写的实际入口：本模块导出的 :func:`add_task` 可在进程外通过
``python -m ningbingjue.blocks.tasklist --text "..." --source agent`` 直接写入
layout.json，无需启动 GUI。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from typing import Any

from ..core.config import TaskListConfig, TaskListItem
from ..core.registry import Block

# 主页卡片默认身份色：雾紫偏青，与「任务/待办」语义相符。
DEFAULT_COLOR = "#7fd6c2"

# 单卡最多渲染的任务行（超出滚动）。纯展示上限，不限制实际存储数量。
MAX_VISIBLE = 50


def _now() -> str:
    try:
        return datetime.now().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


class TaskListBlock:
    """任务列表区块：内存态 + 配置读写。"""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.load_config()

    # -- 配置读写 -------------------------------------------------------
    def save_config(self) -> TaskListConfig:
        tlc = TaskListConfig(
            items=list(self.items),
            show_agent_tag=self.show_agent_tag,
        )
        self.config.tasklist_config = tlc
        return tlc

    def load_config(self) -> None:
        tlc = getattr(self.config, "tasklist_config", None) or TaskListConfig()
        # 拷贝一份可变列表，避免直接持有 dataclass 的共享引用。
        self.items: list[TaskListItem] = [TaskListItem.from_dict(x.to_dict())
                                           for x in tlc.items]
        self.show_agent_tag = bool(tlc.show_agent_tag)

    # -- 任务操作 -------------------------------------------------------
    @staticmethod
    def _gen_id(existing: set[str]) -> str:
        """生成不重复的 12 位短 id（碰撞时重抽，极端兜底用全长 hex）。"""
        for _ in range(10):
            i = uuid.uuid4().hex[:12]
            if i not in existing:
                return i
        return uuid.uuid4().hex

    def add_task(self, text: str, *, source: str = "manual",
                 note: str = "", done: bool = False) -> TaskListItem:
        """新增一行任务。``source`` 为 ``"agent"`` 时带机器来源标记。

        Returns the created item (so callers / the CLI can surface its id).
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("任务内容不能为空")
        item = TaskListItem(
            id=self._gen_id({x.id for x in self.items}),
            text=text,
            done=bool(done),
            source=source if source in ("manual", "agent") else "manual",
            note=(note or "").strip(),
            created_at=_now(),
        )
        self.items.append(item)
        return item

    def remove_task(self, item_id: str) -> bool:
        before = len(self.items)
        self.items = [x for x in self.items if x.id != item_id]
        return len(self.items) != before

    def toggle_done(self, item_id: str) -> bool:
        """翻转完成态，返回该任务当前是否完成。"""
        for x in self.items:
            if x.id == item_id:
                x.done = not x.done
                return x.done
        return False

    def clear_done(self) -> int:
        """移除所有已完成任务，返回移除数量。"""
        before = len(self.items)
        self.items = [x for x in self.items if not x.done]
        return before - len(self.items)

    def clear_all(self) -> int:
        n = len(self.items)
        self.items = []
        return n

    def set_show_agent_tag(self, on: bool) -> None:
        self.show_agent_tag = bool(on)

    @property
    def pending(self) -> list[TaskListItem]:
        return [x for x in self.items if not x.done]

    # -- 区块注册 -------------------------------------------------------
    def register(self, registry: Any) -> None:
        registry.register(Block(
            type="tasklist",
            name="任务列表",
            color=DEFAULT_COLOR,
            card=None,
            hooks={
                # 高度由内容（标题 + 输入框 + 列表）决定；提供与真实块一致的
                # GetCardHeight 钩子以满足区块契约（数值仅用于占位，运行时不消费）。
                "GetCardHeight": lambda: 220,
                "OnSaveConfig": self.save_config,
                "OnLoadConfig": self.load_config,
            },
        ))


# ---------------------------------------------------------------------------
# 进程外入口：让 agent / 脚本直接往 layout.json 写入一条任务，无需启动 GUI。
# 用法：
#   python -m ningbingjue.blocks.tasklist --text "查看 logs" --source agent
#   python -m ningbingjue.blocks.tasklist --text "复查崩溃" --note "logs/crash.txt"
# ---------------------------------------------------------------------------
def _resolve_layout_path() -> str:
    # 优先环境变量，其次常见路径：开发态 ningbingjue/layout.json 与打包态
    # _internal/layout.json（PyInstaller 冻结后文件在 <bundle>/_internal/）。
    env = os.environ.get("NBJ_LAYOUT")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    # 冻结态 exe：sys._MEIPASS 即 bundle 根，layout.json 在 _internal 下。
    meipass = getattr(sys, "_MEIPASS", None)
    candidates = []
    if meipass:
        candidates.append(os.path.join(meipass, "_internal", "layout.json"))
    # 开发态：src/ningbingjue/blocks -> 回退三层到 ningbingjue/layout.json。
    candidates.append(os.path.join(here, "..", "..", "..", "layout.json"))
    # 打包态（源码目录视角）：ningbingjue/_internal/layout.json。
    candidates.append(os.path.join(here, "..", "..", "..", "_internal", "layout.json"))
    candidates.append(os.path.join(os.path.dirname(here), "layout.json"))
    for c in candidates:
        if os.path.exists(c):
            return os.path.abspath(c)
    # 默认回退到开发态路径（即便尚不存在，写时创建）。
    return os.path.abspath(candidates[0])


def _cli_add(text: str, source: str, note: str, done: bool) -> int:
    path = _resolve_layout_path()
    data: dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except Exception as exc:  # 文件损坏时不破坏既有内容
            print(f"[tasklist] 读取 {path} 失败: {exc}", file=sys.stderr)
            return 2
    tlc = data.get("TaskListConfig", {})
    items = tlc.get("Items", []) or []
    existing = {x.get("Id") for x in items if isinstance(x, dict)}
    item = TaskListItem(
        id=TaskListBlock._gen_id(existing) if existing else uuid.uuid4().hex[:12],
        text=text.strip(),
        done=bool(done),
        source=source if source in ("manual", "agent") else "manual",
        note=(note or "").strip(),
        created_at=_now(),
    )
    items.append(item.to_dict())
    tlc["Items"] = items
    if "ShowAgentTag" not in tlc:
        tlc["ShowAgentTag"] = True
    data["TaskListConfig"] = tlc
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[tasklist] 写入 {path} 失败: {exc}", file=sys.stderr)
        return 3
    print(f"[tasklist] 已添加任务（{item.source}）: {item.text}  -> {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="凝冰结魄 · 任务列表：追加一条任务")
    ap.add_argument("--text", "-t", required=True, help="任务内容")
    ap.add_argument("--source", "-s", default="agent",
                    choices=["manual", "agent"], help="来源：agent 自动填写 / manual 手动")
    ap.add_argument("--note", "-n", default="", help="可选备注（如日志路径）")
    ap.add_argument("--done", action="store_true", help="标记为已完成")
    args = ap.parse_args(argv)
    if not args.text.strip():
        print("[tasklist] 任务内容不能为空", file=sys.stderr)
        return 1
    return _cli_add(args.text, args.source, args.note, args.done)


if __name__ == "__main__":
    raise SystemExit(main())
