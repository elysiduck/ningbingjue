"""凝冰结魄 - 解冻失败现场诊断工具（CLI 入口）

核心实现在 ``ningbingjue/platform/diag_unfreeze.py``（编辑页「解冻诊断」按钮
调用的就是它）。本文件是在源码树/开发环境下运行的命令行薄壳。

用法（在运行凝冰结魄的机器上，复现解冻失败后）：
    python tools/diag_unfreeze.py --root <凝冰结魄目录> [--fix]
    --root  凝冰结魄目录（exe 或 layout.json 所在目录）。默认：当前目录
    --fix   尝试真实执行 resume + 窗口移回（会改变游戏状态！默认只读）

输出：控制台 + diag_unfreeze_<时间戳>.txt（--root 目录）
"""
import argparse
import os
import sys


def _main() -> int:
    ap = argparse.ArgumentParser(description="凝冰结魄 · 解冻失败现场诊断")
    ap.add_argument("--root", default=None, help="凝冰结魄安装目录")
    ap.add_argument("--fix", action="store_true",
                    help="尝试真实 resume + 移回窗口（默认只读）")
    args = ap.parse_args()

    # 源码树内运行：把项目 src/ 加入 sys.path 以导入核心模块。
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.normpath(os.path.join(here, "..", "src"))
    if src not in sys.path:
        sys.path.insert(0, src)

    try:
        from ningbingjue.platform.diag_unfreeze import run_diag_to_file
    except ImportError as e:  # pragma: no cover
        print(f"[!] 无法导入诊断核心模块: {e!r}")
        print("    请在源码树内运行，或在凝冰结魄的「进程冻结」编辑页"
              "点击「解冻诊断」按钮（exe 环境内置）。")
        return 2

    root = args.root or os.getcwd()
    path = run_diag_to_file(root, fix=args.fix)
    if path:
        print(f"\n报告已写入: {path}")
        return 0
    print("\n[!] 报告写入失败，请检查 --root 目录可写性；"
          "控制台已打印现场内容。")
    return 1


if __name__ == "__main__":
    sys.exit(_main())
