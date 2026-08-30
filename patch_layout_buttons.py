# -*- coding: utf-8 -*-
"""
凝冰结魄 · 布局编辑界面按钮 字号/加粗/放宽 一键补丁 (v2 · 变量锚定版)
=================================================================
作用（全部在 src/ningenjue/ui/edit_overlay.py）：
  · 页脚「保存 / 取消」→ 字号 11px→13px，且水平内边距 7px 14px → 7px 22px（放宽，高度不变）
  · 页脚「呼吸灯 / 快捷键 / 手柄隔离」→ 11px→13px（保持原加粗）
  · 页眉「按钮风格编辑 / 贴右 / 磁贴 / 分页 / 重启」→ 11px→13px 并补加粗
  · 页眉「重启」文字 「↻ 重启+」→「↻ 重启」（去掉 + 号）

为什么用变量名锚定：上一版用「精确字符串计数」校验，因保存按钮当时仍是 11px 导致
校验失败、整体 abort、什么都没改。本版按 self._xxx 变量名定位各自 setStyleSheet 块，
互不干扰；某按钮若用默认样式（无 setStyleSheet）则跳过并提示，绝不会整体失败。

安全：改前自动备份为 edit_overlay.py.bak_<时间戳>；UTF-8 读写。
用法（在项目根 ningbingjue/ 下双击 run_patch.bat，或 python patch_layout_buttons.py）：
"""
import io
import os
import re
import sys
import time

TARGET = "src/ningenjue/ui/edit_overlay.py"

# (变量名, 是否补加粗, 是否放宽, 目标字号)
JOBS = [
    ("self._save_btn", False, True, 13),
    ("self._cancel_btn", False, True, 13),
    ("self._breath_btn", False, False, 13),
    ("self._shortcut_edit_btn", False, False, 13),
    ("self._xinput_gate_btn", False, False, 13),
    ("self._btn_style_open", True, False, 13),
    ("self._align_btn", True, False, 13),
    ("self._new_tile_btn", True, False, 13),
    ("self._add_page_btn", True, False, 13),
    ("self._restart_btn", True, False, 13),
]


def main():
    path = TARGET
    if not os.path.isfile(path):
        alt = os.path.join("ningbingjue", TARGET)
        if os.path.isfile(alt):
            path = alt
        else:
            sys.exit("找不到目标文件：%s（请在项目根 ningbingjue/ 下运行）" % TARGET)

    txt = io.open(path, encoding="utf-8").read()
    original = txt

    for var, bold, widen, fs in JOBS:
        # 锚定该变量自己的 setStyleSheet(...) 调用块（支持跨行多段字符串拼接）
        pat = re.compile(re.escape(var) + r'\.setStyleSheet\(\s*(.*?)"\)', re.DOTALL)
        m = pat.search(txt)
        if not m:
            print("[跳过] 未找到 %s.setStyleSheet（可能用默认样式，无需改）" % var)
            continue
        block = m.group(1)
        new_block = block
        # 字号 11/12 -> 目标（覆盖主样式与 hover 样式里的所有声明）
        new_block = new_block.replace("font-size: 11px;", "font-size: %dpx;" % fs)
        new_block = new_block.replace("font-size: 12px;", "font-size: %dpx;" % fs)
        if bold and "font-weight: bold" not in new_block:
            # 在第一个 font-size 后补 font-weight: bold;
            new_block = re.sub(r'(font-size: \d+px;)', r'\1 font-weight: bold;',
                               new_block, count=1)
        if widen:
            new_block = new_block.replace("padding: 7px 14px;", "padding: 7px 22px;")
        txt = txt[:m.start(1)] + new_block + txt[m.end(1):]
        desc = "font-size:%dpx" % fs
        if bold:
            desc += " +bold"
        if widen:
            desc += " +放宽"
        print("[应用] %s → %s" % (var, desc))

    # 重启去「+」
    if 'HintButton("↻ 重启+")' in txt:
        txt = txt.replace('HintButton("↻ 重启+")', 'HintButton("↻ 重启")')
        print("[应用] 重启 去掉「+」")
    else:
        print("[提示] 未找到「↻ 重启+」（可能已改，或文本不同）")

    if txt == original:
        print("\n⚠️ 没有做任何修改（可能所有按钮已是目标状态，或变量名未匹配）。")
        return

    bak = path + ".bak_" + time.strftime("%Y%m%d_%H%M%S")
    io.open(bak, "w", encoding="utf-8").write(original)
    io.open(path, "w", encoding="utf-8").write(txt)
    print("\n✅ 已应用并写入：%s" % path)
    print("[备份] %s" % bak)
    print("请重新双击 launch.bat 启动查看效果。")


if __name__ == "__main__":
    main()
