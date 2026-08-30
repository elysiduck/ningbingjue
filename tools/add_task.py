"""凝冰结魄 · 任务列表：从命令行追加一条任务（agent / 脚本友好入口）。

用法：
    python tools/add_task.py "查看 logs 诊断崩溃" --source agent --note "logs/crash.txt"
    python tools/add_task.py "复查风扇失效" --source manual

source=agent 的任务在「任务列表」卡片上以 🤖 标记，便于区分机器自动填写。
任务直接写入凝冰结魄的 layout.json（与程序运行时共用同一份数据），下次打开
程序即可在主页看到。无需启动 GUI。
"""
from __future__ import annotations

import argparse
import os
import sys

# 让脚本既能独立运行（开发态），也能在打包后从 _internal 找到模块。
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, os.path.join(_ROOT, "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="凝冰结魄 · 任务列表：追加一条任务")
    ap.add_argument("text", help="任务内容")
    ap.add_argument("--source", "-s", default="agent",
                    choices=["manual", "agent"],
                    help="来源：agent 自动填写 / manual 手动（默认 agent）")
    ap.add_argument("--note", "-n", default="", help="可选备注（如日志路径）")
    ap.add_argument("--done", action="store_true", help="标记为已完成")
    args = ap.parse_args(argv)
    if not args.text.strip():
        print("[add_task] 任务内容不能为空", file=sys.stderr)
        return 1

    # 复用 block 内的 CLI 写入逻辑（进程外直接改 layout.json）。
    from ningbingjue.blocks.tasklist import _cli_add
    return _cli_add(args.text, args.source, args.note, args.done)


if __name__ == "__main__":
    raise SystemExit(main())
