#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_scripts_integrity.py  -  Python 脚本完整性/损坏检查器
零三方依赖，仅用标准库。

用法:
  python check_scripts_integrity.py            # 只扫项目自有源码(递归, 排除 bundled 库)
  python check_scripts_integrity.py DIR1 DIR2  # 扫指定目录(同样排除 bundled 库)
  python check_scripts_integrity.py --all      # 连 bundled runtime/.venv 一起扫
  python check_scripts_integrity.py --self     # 仅自检本文件
  python check_scripts_integrity.py --list     # 列出每个被检查的文件(干净也列)

它检查什么(即"文件损坏"的常见信号):
  硬损坏(必须处理):
    1. 嵌入 NUL 字节   -> 文本文件里出现 \\x00 通常是二进制垃圾被写进源码或半截写入
    2. 编码损坏        -> 无法按 UTF-8(含 BOM) 解码，说明混入了非法字节
    3. 语法/截断       -> 用 py_compile 编译，抓语法错误、半截文件、括号不闭合
    4. 读/ stat 错误    -> 文件打不开或取不到大小(权限/路径损坏)
  待确认(通常正常, 需人工看一眼):
    5. 零字节          -> 空 __init__.py 在很多第三方包里是故意的, 但项目里本该有内容的空文件需留意

默认排除目录(避免把 bundled 的 runtime/.venv/site-packages 误报成损坏):
  runtime  .venv  venv  env  __pycache__  node_modules
  build  dist  .git  .pytest_cache  .tox

退出码: 0 = 未发现硬损坏；1 = 发现硬损坏；2 = 用法/运行错误。
"""
import os
import sys
import py_compile

# 默认排除的目录名(项目自带的 bundled 解释器与虚拟环境, 不是用户的代码)
DEFAULT_EXCLUDES = {
    "runtime", ".venv", "venv", "env", "__pycache__", "node_modules",
    "build", "dist", ".git", ".pytest_cache", ".tox",
}

# 硬损坏标记: 这些出现即判定为"损坏"
HARD_FLAGS = {
    "null_bytes", "encoding", "syntax", "compile_other",
    "read_error", "stat_error",
}
# 软标记: 提示/待确认, 不计入硬损坏
SOFT_FLAGS = {"zero_byte"}


def scan_file(fp):
    """返回 (flags, detail)。flags 是字符串列表。"""
    flags = []
    detail = []
    try:
        sz = os.path.getsize(fp)
    except OSError as e:
        return ["stat_error"], [str(e)]

    if sz == 0:
        flags.append("zero_byte")
        detail.append("size=0 (空文件, 可能为正常的空 __init__.py, 需人工确认)")

    try:
        with open(fp, "rb") as fh:
            data = fh.read()
    except OSError as e:
        return ["read_error"], [str(e)]

    if b"\x00" in data:
        n = data.count(b"\x00")
        flags.append("null_bytes")
        detail.append(f"{n} 个 NUL 字节(疑似二进制污染/半截写入)")

    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        flags.append("encoding")
        detail.append(f"非 UTF-8 编码: {e}")

    try:
        # 注意: 不能用 quiet=2 —— 该 bundled Python 在 quiet>=2 时遇到语法错误
        # 不会抛 PyCompileError, 会静默放过(导致漏报语法损坏)。quiet=1 既能
        # 抑制 stderr 噪声, 又会在 doraise=True 时照常抛错。
        py_compile.compile(fp, doraise=True, quiet=1)
    except py_compile.PyCompileError as e:
        msg = str(e).strip()
        last = msg.splitlines()[-1] if msg else str(e)
        flags.append("syntax")
        detail.append(f"编译失败: {last}")
    except Exception as e:  # noqa: BLE001
        flags.append("compile_other")
        detail.append(f"编译异常: {e}")

    return flags, detail


def collect_py(roots, excludes):
    out = []
    for r in roots:
        if os.path.isfile(r) and r.endswith(".py"):
            out.append(r)
            continue
        for dp, dns, fns in os.walk(r):
            # 排除 bundled / 缓存目录, 避免把第三方库误报成损坏
            if excludes:
                dns[:] = [d for d in dns if d not in excludes]
            for fn in fns:
                if fn.endswith(".py"):
                    out.append(os.path.join(dp, fn))
    return sorted(set(out))


def main(argv):
    do_self = "--self" in argv
    do_all = "--all" in argv
    do_list = "--list" in argv
    scan_args = [a for a in argv if not a.startswith("--")]

    excludes = set() if do_all else DEFAULT_EXCLUDES

    if do_self:
        roots = [os.path.abspath(__file__)]
    elif scan_args:
        roots = scan_args
    else:
        roots = [os.path.dirname(os.path.abspath(__file__)) or "."]

    files = collect_py(roots, excludes)
    print(f"扫描范围: {roots}")
    if excludes:
        print(f"排除目录: {', '.join(sorted(excludes))}  (--all 可包含)")
    else:
        print("排除目录: 无 (--all 已启用, 扫描全部)")
    print(f"待检查 .py 文件: {len(files)}\n")

    clean = 0
    hard_bad = []
    soft_bad = []
    for fp in files:
        flags, detail = scan_file(fp)
        # 拆分硬/软
        hard = [f for f in flags if f in HARD_FLAGS]
        soft = [f for f in flags if f in SOFT_FLAGS]
        if hard:
            hard_bad.append((fp, flags, detail))
        elif soft:
            soft_bad.append((fp, flags, detail))
        else:
            clean += 1
        if hard:
            print(f"[损坏] {fp}")
            for d in detail:
                print(f"        - {d}")
        elif soft:
            print(f"[提示] {fp}")
            for d in detail:
                print(f"        - {d}")
        elif do_list:
            # --list: 即便干净也把每个文件列出来(检测清单)
            print(f"[OK]   {fp}")

    print("\n=== 摘要 ===")
    print(f"总计扫描 : {len(files)}")
    print(f"正常      : {clean}")
    print(f"疑似损坏 : {len(hard_bad)}   (硬损坏, 必须处理)")
    print(f"待确认    : {len(soft_bad)}   (空文件, 通常正常, 需人工看一眼)")
    if hard_bad:
        print("\n硬损坏文件清单:")
        for fp, flags, _ in hard_bad:
            print(f"  {fp}  ->  {','.join(f for f in flags if f in HARD_FLAGS)}")
    if soft_bad:
        print("\n待确认文件清单(通常为第三方包的正常空文件):")
        for fp, flags, _ in soft_bad:
            print(f"  {fp}  ->  {','.join(f for f in flags if f in SOFT_FLAGS)}")
    if not hard_bad:
        print("\n未发现硬损坏。项目自有 Python 脚本均可正常编译、编码合法、无二进制污染。")
        return 0
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("已中断")
        sys.exit(2)
