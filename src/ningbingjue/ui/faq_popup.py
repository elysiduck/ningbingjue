#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
faq_popup.py — 凝冰结魄「疑惑解答弹窗」

完全解耦的附属模块，只依赖 PyQt6 + 标准库，不侵入任何 block / panel 的业务逻辑。
挂载方式（见 app_window.py）：
    self._faq = FaqController(self)        # 在 _OverlayWindow.__init__ 末尾
    self._controller.on_help = self._faq.toggle_for_current   # 手柄 L3

特性：
  • 紧贴主窗口「朝向屏幕中心」的一侧：主窗口在右半屏 → dock 在左、从主窗口
    左边缘向外伸出；在左半屏 → dock 在右、从主窗口右边缘向外伸出。
    （出现方向：从 Python 主程序里「探」出来、移到外面，而不是从外面飞进来。）
  • 主窗口被拖动时实时跟随（moveEvent 调 controller.reposition()）。
  • 内容随上下文：点卡片标题打开该卡片的疑惑解答；F1/H 打开当前选中卡片；
    切换页面自动关闭（_OverlayWindow._on_page_switched 调 controller.close()）。
  • 触发：鼠标点卡片标题、键盘 F1/H、手柄 L3（左摇杆按下）。

headless 自检：python -m ningbingjue.ui.faq_popup --selftest
"""

from __future__ import annotations

import re
import sys

from PyQt6.QtCore import Qt, QObject, QRect, QPropertyAnimation, QEasingCurve, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QLabel, QScrollArea, QPushButton, QHBoxLayout, QVBoxLayout, QWidget,
    QTextBrowser, QLayout,
)

from ..core.color.semantic import get_theme_accent


# ---------------------------------------------------------------- 主题色
def _theme_accent() -> tuple[int, int, int]:
    """返回当前主题色（呼吸灯颜色）的 (r, g, b)。"""
    c = get_theme_accent()
    return (c.r, c.g, c.b)


def _accent_hex(accent: tuple[int, int, int] | None = None) -> str:
    r, g, b = accent if accent else _theme_accent()
    return f"#{r:02x}{g:02x}{b:02x}"


# ---------------------------------------------------------------- 可点击标题
class ClickableTitle(QLabel):
    """编辑器页面顶部的标题标签：鼠标点击即打开疑惑解答（手型光标 + 悬停提示）。

    用法：
        t = ClickableTitle("呼吸灯自定义", color="#ffb3d1", size=20)
        t.clicked.connect(lambda: app_window._faq.open_for("breathing_edit", "鼠标 标题"))

    ``color`` 留空则跟随全局主题色；``size`` 控制字号（页面主标题用 20，
    子卡片标题用较小值）。``set_color`` / ``set_size`` 可在运行时重新着色。
    """

    clicked = pyqtSignal()

    def __init__(self, text: str, color: str | None = None, size: int = 14,
                 parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._col = color
        self._size = size
        self._apply()
        self.setToolTip("点击查看本页疑惑解答")

    def _apply(self) -> None:
        col = self._col or _accent_hex()
        # 必须用 QLabel 类型选择器：父面板普遍设置了
        # ``QLabel { background: transparent; border: none; }``，
        # 若子控件用无选择器的声明，font-size/font-weight 会被父选择器覆盖。
        self.setStyleSheet(
            f"QLabel{{color:{col};font-size:{self._size}px;font-weight:bold;"
            f"padding:1px 4px;border-radius:6px;}}"
            f"QLabel:hover{{border-bottom:1px solid {col};}}"
        )

    def set_color(self, color: str) -> None:
        self._col = color
        self._apply()

    def set_size(self, size: int) -> None:
        self._size = size
        self._apply()

    def mousePressEvent(self, ev: object) -> None:  # noqa: N802
        self.clicked.emit()
        ev.accept()  # 阻止事件继续冒泡到主窗口（避免误触发窗口拖动 / 主 HUD 卡片 FAQ）


# ---------------------------------------------------------------- Markdown
def _md_to_html(md: str, accent: tuple[int, int, int] | None = None) -> str:
    """极简 Markdown -> HTML（标题 / 列表 / 加粗 / 段落）。

    不依赖 setMarkdown（Qt 版本差异大），自建转换保证跨版本一致渲染。
    ``accent`` 为主题色 (r,g,b)，用于标题与行内 code 着色，跟随全局主题。
    """
    import html as _html

    def esc(s: str) -> str:
        return _html.escape(s)

    def inline(s: str) -> str:
        s = esc(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
        return s

    out: list[str] = []
    in_ul = False
    for raw in md.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            if in_ul:
                out.append("</ul>")
                in_ul = False
            continue
        if line.startswith("### "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h3>{inline(line[4:])}</h3>")
        elif line.startswith("## "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("- ") or line.startswith("• ") or line.startswith("* "):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{inline(line[2:].lstrip())}</li>")
        else:
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<p>{inline(line)}</p>")
    if in_ul:
        out.append("</ul>")
    ar, ag, ab = accent if accent else (127, 209, 255)
    css = (
        f"h1{{font-size:15px;color:#{ar:02x}{ag:02x}{ab:02x};margin:2px 0 8px;font-weight:bold;}}"
        f"h2{{font-size:13.5px;color:#{ar:02x}{ag:02x}{ab:02x};margin:10px 0 5px;font-weight:bold;}}"
        f"h3{{font-size:12.5px;color:#{ar:02x}{ag:02x}{ab:02x};margin:8px 0 4px;font-weight:bold;}}"
        "p{margin:4px 0;line-height:1.6;}"
        "ul{margin:4px 0 4px 0;padding-left:10px;}"
        "li{margin:2px 0;line-height:1.45;}"
        "b{color:#eaf2ff;}"
        f"code{{background:rgba({ar},{ag},{ab},0.14);color:#{ar:02x}{ag:02x}{ab:02x};"
        "padding:0 4px;border-radius:4px;font-size:12px;}"
    )
    return f"<style>{css}</style>\n" + "\n".join(out)


# ---------------------------------------------------------------- FAQ 内容
# key == BlockPanel.block_type（与 blocks 注册名一致），或编辑器页面 key
#（button_edit / breathing_edit / procmon_edit / tile_edit）。
FAQ: dict[str, dict[str, str]] = {
    "rtss": {
        "title": "锁帧控制 · 疑惑解答",
        "body": (
            "## 这是什么\n"
            "锁帧控制给游戏设定一个**上限帧率**，避免帧率过高带来的额外功耗与发热。\n\n"
            "## 手动锁帧\n"
            "开启后，限帧器始终以 **基线 FPS**（默认 30，或你设定的封顶值）运行。\n\n"
            "## 自动锁帧（卡片右侧按钮）\n"
            "低负载时自动把帧率顶到 60，高负载时回落基线。\n\n"
            "其运行逻辑为：\n"
            "- 开启后每约 **1 秒**轮询一次，读取 GPU 占用率，并确认前台是真实游戏"
            "（不是桌面或本程序自身）。\n"
            "- 当 **GPU 占用 < 25%** 且前台是游戏，连续 **3 次**轮询（约 3 秒）→ "
            "自动把帧率顶到 **60**（显示「已顶60」），让 2D 菜单 / 读盘画面更顺滑。\n"
            "- 当 GPU 占用回到 **≥25%** 或前台窗口丢失，连续 **2 次**轮询（约 2 秒）→ "
            "回落到基线 FPS。\n"
            "- 没有 GPU 监控来源时，按钮置灰显示「自动锁帧: 不可用」。\n"
            "- 关闭「锁帧」会一并关闭自动锁帧；重启后从 3D 封顶档开始（保存的是基线值）。"
        ),
    },
    "power": {
        "title": "电源 · 疑惑解答",
        "body": (
            "## 这是什么\n"
            "电源卡片用于实时设定 CPU 的 **TDP**（功耗上限）与显卡频率偏移。\n\n"
            "- **TDP** 越低越省电、温度更低，但性能受限；调高则释放更多性能，但更热、更耗电。\n"
            "- 显卡频率偏移（**GPU Auto**）会在游戏负载下自动拉高/回落频率，"
            "滑块是你允许的**上限**。\n"
            "- 改动会实时写入 RyzenAdj / 显卡驱动，适合在不同游戏间快速切换性能档位。"
        ),
    },
    "undervolt": {
        "title": "降压控制 · 疑惑解答",
        "body": (
            "## 这是什么\n"
            "降压控制按**游戏负载**实时调节 CPU 的 Curve Optimizer 负压偏移（CO），"
            "重载时自动加负压省电降温，空闲时回落。\n\n"
            "它走 ryzenadj 的 `--set-coall`（全 CPU 核统一偏移），**不碰核显（cogfx）**"
            "——核显 CO 在 HX370 等机型上被 BIOS/SMU 锁死，写不进去也没意义。\n\n"
            "## 档位怎么切\n"
            "- **空闲**：前台无真实游戏时 CO=0（不降压），避免轻载掉压。\n"
            "- **轻载**：前台是游戏、CPU/GPU 占用不高 → CO=-20mV（键盘安全的稳定档）。\n"
            "- **重载**：游戏明显吃资源 → CO=-50mV（省电降温更狠）。\n"
            "切换带**非对称去抖**：升压（朝 -50）需连续 3 次采样确认，降压（朝 0）"
            "1 次即可回，并有 30 秒粘滞，避免 ALT-TAB 切到 2D 菜单时反复跳变。\n\n"
            "## 自动降压（卡片按钮）\n"
            "- 开启后状态机每约 **1 秒**读一次前台游戏 + GPU/CPU 负载，自动在"
            " 空闲 / 轻载 / 重载 档间切换。\n"
            "- 关闭总开关会**立即把电压归零**（`reset`），避免 -50mV 残留挂着。\n"
            "- 状态行实时显示当前档位与下发值，例如「自动降压运行中 · 档位 重载 · CO=-50」。\n\n"
            "## 关闭降压（按钮）\n"
            "关闭自动控制器，并把电压**钉死在 -20mV** 固定值（不再随负载变化）。\n"
            "适合「只想稳稳降一点、绝不碰键盘风险」的场景：自动已关、电压固定、"
            "重启 / 拔插电后需重新点击（CO 断电会还原）。\n\n"
            "## 允许 -50mV 重载档 / -50 键盘受控验证\n"
            "-50mV 档曾导致部分掌机**键盘失灵**（轻载降压过深，EC 经 eSPI 通信中断）。\n"
            "因此 -50 有**两道门控**，必须都开才会真正下发：\n"
            "1. **允许 -50mV 重载档**（`heavy_enabled`）：放行重载档判定。\n"
            "2. **已完成 -50 键盘受控验证**（`heavy_validated`）：确认 -50 实机不冻键盘。\n"
            "只开第一个、没开第二个时，重载档会**自动降到 -20**，并在降压卡片状态行"
            "与性能监控卡片黄字提示「-50未验证→用-20」。\n"
            "验证方法：短期开第二个开关，进重负载游戏连续玩 30+ 分钟，确认键盘不冻结、"
            "事件查看器无 WHEA-Logger 欠压报错；有异常立即关掉第二个开关（自动回退 -20）。"
            "建议先用 `dry_run` 观察。\n\n"
            "## 录制标定\n"
            "点「开始录制」会记录一段前台游戏的真实负载曲线（CPU% / GPU%），"
            "停止后生成一份自带图表的 HTML 报表，帮你判断该机器的合适负压档位与阈值。\n\n"
            "## 常见问题\n"
            "- **为什么掌机键盘会冻住？** 轻载时 CO 降压最深易使 SoC 常开域掉压，"
            "波及键盘控制器。把档位压回 -20~-30 或改用本卡的「关闭降压」即可规避。\n"
            "- **能不能读出当前 CO 值？** ryzenadj 只能写、不能干净读回当前偏移；"
            "看行为即可（同负载同频下电压是否更低、事件查看器有无 WHEA 报错、"
            "重启后是否还原）。\n"
            "- **拔插电后降压没了？** CO 是临时写、断电 / 插电要重设，本卡会在需要时"
            "重新下发。\n"
        ),
    },
    "freeze": {
        "title": "进程冻结 · 疑惑解答",
        "body": (
            "## 这是什么\n"
            "进程冻结把后台吃资源的进程**挂起**（内核级 suspend），释放 CPU / 内存给前台游戏。\n\n"
            "- **候选列表** = 游戏进程优先显示（不限内存），非游戏需占用内存 > 300MB，"
            "自动排除自身与已冻结项。\n"
            "- **两按激活**：第一次按下高亮该进程（3 秒超时），同一进程的第二次按下才执行"
            "冻结 / 解冻。\n"
            "- 已冻结的 PID 会持久化保存，重启后仍为冻结态，可随时解冻恢复。"
        ),
    },
    "perfmon": {
        "title": "性能监控 · 疑惑解答",
        "body": (
            "## 这是什么\n"
            "性能监控是一张**只读实时卡片**，把当前系统的关键指标与「降压控制 / 锁帧"
            "控制」的判断结果汇总到一个面板，方便你一眼看清机器此刻的运行状态。\n\n"
            "卡片上的每一项都来自同一个共享数据源（全应用唯一的指标捕获器"
            " `MetricSampler`，后台常驻 1 个合并 PowerShell 流读 GPU / 每核 / "
            "温度 + 1 个线程跟踪前台进程 CPU），**纯内存只读、零额外采样**，"
            "所以不会拖慢游戏。\n\n"
            "## 各项含义与数据来源\n"
            "- **当前窗口**：前台进程名。命中游戏关键字（或你加的白名单）识别为游戏；"
            "否则显示任意前台程序名；无前台窗口显示「桌面 / 无前台窗口」。\n"
            "- **CPU 平均占用**：所有核心占用的**均值**，抓均衡型负载（如《原神》）。\n"
            "- **CPU 最大占用**：**最忙那一核**的占用，抓单核瓶颈型负载（如《绝区零》）。\n"
            "- **GPU 占用**：系统整体 GPU 利用率，作为判定的防抖守卫。\n"
            "- **负载判定**：见下方「判断逻辑」。\n"
            "- **是否降压**：当前 Curve Optimizer 偏移是否非零；是则显示 `CO=<mV>`。\n"
            "- **锁帧状态**：RTSS 是否限制了上限帧率——「已锁帧 X FPS」或「未锁帧(解锁)」。\n"
            "- **GPU 锁定频率**：power 卡片给出的 GPU 频率——静态锁定（滑块值）/ "
            "浮动锁定（自动锁频实时值）/ 未锁定(自动)。\n"
            "- **当前降压值**：即 `是否降压` 里的 `CO=<mV>`（如 `CO=-20`、`CO=-50`），"
            "0 表示当前未降压。若显示 `CO=-20 (-50未验证)`（黄字），说明状态机判定为"
            "重载但 -50 键盘受控验证未通过，已自动降一档用 -20——去降压控制卡片编辑项"
            "开「已完成 -50 验证」即可放行 -50。\n\n"
            "## 负载判定逻辑（和「降压控制」同一套状态机）\n"
            "1. **前台无真实游戏**（或失焦超过 30 秒粘滞）→ 判定 **空闲**，CO=0，不降压。\n"
            "2. 前台是游戏时，看 CPU：单核最大 ≥ 阈值 **或** 均值 ≥ 阈值（阈值在"
            " `config/undervolt.json`，默认 最大50 / 均值25），且**连续约 2 秒确认**"
            "才升 **重载(CO=-50)**——升档慢，过滤单核瞬时尖峰。\n"
            "3. 否则累计轻载时长，满约 3 秒落 **轻载(CO=-20)**——降档即时。\n"
            "4. **GPU 防抖**：已是重载且 GPU 仍高（≥55%）时，即使 CPU 暂时回落也"
            "**保持重载**不降级（应对开背包 / 切菜单瞬间 CPU 掉、GPU 还在跑）。\n"
            "5. **-50 双重门控**：默认不真正下发 -50，除非你在降压控制卡片的编辑项里"
            "同时开启「允许 -50mV 重载档」(`heavy_enabled`) 与「已完成 -50 键盘受控验证」"
            "(`heavy_validated`)；否则重载档会降一档用 -20（性能监控卡片会黄字标注"
            "「-50未验证」），避免轻载降压过深导致掌机键盘失灵。\n\n"
            "## 为什么这些值会实时变化\n"
            "- 进 / 出游戏、ALT-TAB、开 2D 菜单都会改变前台窗口与负载档位。\n"
            "- 失焦超过 30 秒粘滞后，负载判定回落空闲、降压归零（避免离开游戏还挂着 -50）。\n"
            "- 本卡片只**展示**判断结果，不修改任何设置；要改降压 / 锁帧请去对应的"
            "「降压控制」「锁帧控制」卡片。"
        ),
    },
    "ls": {
        "title": "Lossless Scaling · 疑惑解答",
        "body": (
            "## 这是什么\n"
            "LS 卡片是 Lossless Scaling 的**预设管理器**（不负责启动 / 关闭 LS 本体）。\n\n"
            "- 它扫描 LS 的预设目录，并通过进程检测显示当前运行状态。\n"
            "- 解析活动 `settings.xml`，展示缩放 / 帧生成 / 性能模式等参数。\n"
            "- 选中某个预设后加载它（写入活动配置），由 LS 自身在游戏中生效。"
        ),
    },
    "tile": {
        "title": "磁贴 · 疑惑解答",
        "body": (
            "## 这是什么\n"
            "磁贴是一个固定的**启动网格**（快捷方式面板）。\n\n"
            "- 每个磁贴可指向文件 / 程序 / 网址；**1–2 格**宽高，按列自动排布。\n"
            "- 激活磁贴即启动其目标；`IMG` 类型磁贴仅作展示。\n"
            "- 布局持久化保存，可添加多个独立磁贴块（**ExtraTileBlocks**）。"
        ),
    },
    "procmon": {
        "title": "进程监控 · 疑惑解答",
        "body": (
            "## 这是什么\n"
            "进程监控用于监测并一键开关外部脚本 / 进程（如 **AutoHotkey** 脚本）。\n\n"
            "- 运行状态通过把配置路径与枚举到的进程命令行做匹配来检测。\n"
            "- AutoHotkey 脚本由 AutoHotkey 解释器托管，因此按脚本路径 / 名称匹配。\n"
            "- 点击按钮即启动或关闭对应脚本 / 进程。"
        ),
    },
    "smt": {
        "title": "SMT 热核隔离 · 疑惑解答",
        "body": (
            "## 这是什么\n"
            "SMT 热核隔离把**最热的物理核**从非游戏进程里让出来，只给前台游戏独占，"
            "降低游戏线程被调度到共享 SMT 兄弟核时带来的延迟抖动（掉帧、卡顿）。\n\n"
            "它靠 `core/topology.py` 枚举物理核 / 逻辑兄弟核，再用 `platform/cpuset.py`"
            " 施加 CPU 集合（affinity）实现隔离，不需要关闭系统 SMT。\n\n"
            "## SMT自动\n"
            "- 开启后持续在后台检测热核（约 **3 秒**一次），**连续 2 次**检测到瓶颈才施加隔离、"
            "**连续 2 次**清除才复原（2 轮滞回，避免负载抖动时反复切换）。\n"
            "- 瓶颈消失后**自动复原全核**，无需手动关闭。\n"
            "- 适合希望「游戏一开就自动让核、一关就自动还核」的场景。\n\n"
            "## SMT锁定\n"
            "- 一次性动作：先复原全核、**3 秒**采样一次，算出最热核后**强制**施加一次隔离。\n"
            "- 锁定**不要求达到瓶颈阈值**（这是你主动要求，不是靠自动判定），"
            "适合固定游戏场景下手动钉死隔离。\n"
            "- 锁定后不做持续检测，需要解除请点「SMT复原」。\n\n"
            "## SMT复原\n"
            "把所有进程的 CPU 集合绑定回**全核**（解除任何隔离）。\n\n"
            "## 常见问题\n"
            "- **为什么用隔离而不是关 SMT？** 全局关 SMT 会损失近一半逻辑线程、整体性能下滑；"
            "隔离只把最热的核让给游戏，既压低延迟又保住多线程吞吐。\n"
            "- **隔离会不会影响其它程序？** 仅调整 CPU 亲和集合，不改系统 SMT 开关；"
            "复原后一切回到原样。\n"
            "- **点击卡片标题**即可随时打开本说明（也支持 F1 / 手柄 L3）。"
        ),
    },
    "fan": {
        "title": "风扇控制 · 疑惑解答",
        "body": (
            "## 这是什么\n"
            "风扇控制通过 **EC 寄存器**直接写风扇转速（0–255），绕过 BIOS 自动策略，"
            "让你按需要手动 / 自动控温。\n\n"
            "温度来源是 `core/thermal.ThermalProbe`（WMI `MSAcpi_ThermalZoneTemperature`，"
            "零第三方依赖）；写入走 `platform/ec_io`（自写 ctypes + WinRing0，"
            "与 ryzenadj **共用同一份** `WinRing0x64.sys`）。\n\n"
            "## 四种模式\n"
            "- **Auto**：把 EC 控制权**交还 BIOS** 自动调速（最安全，退出的默认状态）。\n"
            "- **Manual**：固定一个转速（`manual_pct`/255），拖动滑块即生效。\n"
            "- **Curve**：按温度曲线 **1Hz** 调度，带 **3℃ 滞回**避免风扇在阈值附近抽搐。\n"
            "- **Silent**：固定一个低转速（`silent_pct`/255，默认 0=停转，可在编辑项里改）；"
            "但 **CPU ≥ 85℃** 会拒绝开启 / 自动退出，防止烧机（过热保护）。\n\n"
            "## EC / WinRing0 与权限\n"
            "- 首次写 EC 前会先 `write_ec(ec_enable_addr, 1)` 使能，再写转速；"
            "互斥繁忙时本次跳过、下个 tick 重试，不阻塞主程序。\n"
            "- 若卡片提示 `WinRing0 不可用` / `EC 加载失败`：请**以管理员身份运行**本程序，"
            "或关闭 Windows 的「内存完整性 / Core Isolation（内核隔离）」——"
            "二者会阻止 WinRing0 驱动加载。\n"
            "- 退出程序时若处于 manual / curve / silent，会**自动恢复 Auto**"
            "（交还 BIOS 控制权），防止风扇被永久钉死。\n\n"
            "## 滞回（Curve 模式）\n"
            "温度在阈值附近抖动时保持当前档，等越过 **3℃ 滞回带**才升 / 降档，"
            "避免风扇反复换档的噪声与电机损耗。\n\n"
            "## 曲线 / 静音转速 / 设备识别（编辑项）\n"
            "- 在「风扇控制 · 编辑项」里可**增删曲线拐点**（温度 ℃ → 转速 PWM 0-255）、"
            "调**手动 / 静音固定转速**与**滞回**，改动即时落盘。\n"
            "- **自动检测设备**：读取 WMI 机型并匹配已知 GPD 映射（WM2 / Win Mini / "
            "Win4 / Duo），自动切换 SIO 协议（4E4F / 2E2F）与 EC 寄存器地址——"
            "这就是 MotionAssistant 按机型查表路由的等价实现。\n\n"
            "## 常见问题\n"
            "- **Silent 模式安全吗？** 有 **85℃ 过热保护**兜底——温度一到就拒绝或"
            "自动切回全速，不会真的烧机，但不建议长时间闷在高负载场景。\n"
            "- **温度读不到 / EC 写不进？** 多半是权限或 Core Isolation 拦了驱动，"
            "按上面权限说明处理即可。\n"
            "- **点击卡片标题**随时打开本说明（F1 / 手柄 L3 亦可）。"
        ),
    },
    # ---- 编辑器页面（点击页面标题打开）----
    "button_edit": {
        "title": "按钮风格编辑器 · 疑惑解答",
        "body": (
            "## 这是什么\n"
            "按钮风格编辑器统一设置 HUD 里**所有按钮**的视觉：圆角、边框、配色、"
            "悬停 / 按下双态等。\n\n"
            "## 两层预设\n"
            "- **L1 定制按钮预设**：作用于*单个*按钮（例如只改「锁帧」按钮的颜色）。\n"
            "- **L2 页面风格预设**：作用于*整页*按钮风格（全局统一一套外观）。\n\n"
            "## 操作提示\n"
            "- 左侧预览列表点选按钮 → 右侧调整颜色 / 形状，可「保存为定制预设」或"
            "「覆盖当前预设」。\n"
            "- `link_theme` 开启后，按钮颜色会跟随呼吸灯主题色，无需手动维护。"
        ),
    },
    "breathing_edit": {
        "title": "呼吸灯自定义 · 疑惑解答",
        "body": (
            "## 这是什么\n"
            "呼吸灯是 HUD 顶部的**呼吸扫描光效**，它的颜色同时是整套主题的**唯一主题色**"
            "（边框、各区块标题、滑块高亮都跟随它）。\n\n"
            "## 可调项\n"
            "- **呼吸时间间隔**：一次完整呼吸（最暗→最亮→最暗）的时长。\n"
            "- **辉光颜色**：可自定义，或留空「跟随主题色」。\n"
            "- **峰值亮度**：呼吸最亮时的强度。\n\n"
            "改完会**实时**应用到 HUD 边框与各区块，是调整整体配色的中枢。"
        ),
    },
    "procmon_edit": {
        "title": "进程监控编辑器 · 疑惑解答",
        "body": (
            "## 这是什么\n"
            "这里配置进程监控卡片里**要监控 / 一键开关**的脚本或进程清单。\n\n"
            "## 字段说明\n"
            "- **名称**：在卡片上显示的名字。\n"
            "- **路径**：脚本 / 程序的可执行路径（如 `.ahk` 脚本或其解释器路径）。\n\n"
            "## 提示\n"
            "- 按「新增 / 删除 / 保存」维护清单；「关闭」返回布局编辑。\n"
            "- 运行状态靠路径与进程命令行匹配，路径要写准确。"
        ),
    },
    "tile_edit": {
        "title": "磁贴编辑器 · 疑惑解答",
        "body": (
            "## 这是什么\n"
            "磁贴编辑器管理一个磁贴块里的**所有磁贴**与其排布。\n\n"
            "## 顶栏「磁贴区块设置」\n"
            "- **区块名称**：该磁贴块的标题（可隐藏）。\n"
            "- **新增磁贴**：往这个块里加一个启动项。\n\n"
            "## 操作提示\n"
            "- **区块预览**里可拖动磁贴调整位置（自动按列排布）。\n"
            "- 选中磁贴后，在「磁贴属性」里编辑名称 / 目标路径 / 宽高。\n"
            "- 可添加多个独立磁贴块（ExtraTileBlocks），各自一套磁贴。"
        ),
    },
}


# ---------------------------------------------------------------- 弹窗本体
class FaqPopup(QWidget):
    """紧贴主窗口一侧、从主窗口边缘向外伸出的磨砂圆角弹窗。"""

    def __init__(self, parent_window: QWidget) -> None:
        super().__init__(parent_window)
        self._mw = parent_window
        self._pw = 320
        self._side = "left"
        self._open = False
        self._anim: QPropertyAnimation | None = None
        self._block_type: str | None = None
        # 出现动画改用 windowOpacity 淡入淡出（见 _animate_reveal）——不再用 setMask，
        # 以规避 frameless + 复杂子控件树在 Windows DWM 下 setMask 导致的原生崩溃。
        self._final_rect: QRect | None = None   # 停靠位（全尺寸、主窗口外侧）
        self._main_rect: QRect | None = None    # 主窗口几何（用于跟随/遮罩）
        self._sliver = 16
        # 主题色 / 背景 / 边框透明度（固定深色系，与主界面一致）
        self._accent = QColor(127, 209, 255)
        # 主界面同款深色磨砂背景，不透出桌面；比纯黑稍亮以便看清边缘
        self._bg = QColor(22, 26, 34, 245)
        self._border_alpha = 210

        # 无框 Tool 窗口，且以主窗口为父（parent）。这样 Windows 把它视为
        # 主窗口的从属窗口：① 主窗口移动时自动跟随；② 切换虚拟桌面时
        # 与主窗口同进退；③ 不出现在任务栏。弹窗 dock 在主窗口外侧紧贴（不重叠），
        # 因此不会遮住主界面内容。不再用 WindowStaysOnBottomHint（那会全局置底）。
        # 关键修复（2026-08-18）：显式加 WindowStaysOnTopHint，保证弹窗稳定浮于
        # 主窗口之上（与已稳定的 PerfMonPopup 一致），不再依赖父窗口置顶链传导。
        # 同时**不再用 setMask 做「擦出」动画**：frameless + 复杂子控件树
        # （QScrollArea + QTextBrowser）+ setMask 在 Windows DWM 真实渲染时会原生段错误
        # （0xC0000005，离屏测不到），表现为弹窗打不开 / 程序崩溃。改用 windowOpacity
        # 淡入淡出，既保留圆角（靠 WA_TranslucentBackground 自绘），又彻底避开 setMask。
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 辅助浮窗不应触发 app 退出：Qt.Tool 即使有 parent，Qt 默认仍给它
        # WA_QuitOnClose=True；若主窗口已 hide 到托盘，关本弹窗会误触发
        # lastWindowClosed → app.quit()。显式关闭以根治此类"关浮窗=退程序"。
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setMinimumSize(0, 0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)

        top = QHBoxLayout()
        self._title = QLabel("")
        self._title.setStyleSheet("color:#9fb0c8;font-size:14px;font-weight:bold;")
        top.addWidget(self._title, 1)
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(26, 26)
        close_btn.setStyleSheet(
            "QPushButton{background:rgba(255,90,90,0.16);color:#ff9b9b;"
            "border:1px solid rgba(255,90,90,0.3);border-radius:6px;}"
            "QPushButton:hover{background:rgba(255,90,90,0.3);}"
        )
        close_btn.clicked.connect(self.close_popup)
        top.addWidget(close_btn)
        outer.addLayout(top)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self._body = QTextBrowser()
        self._body.setOpenExternalLinks(False)
        self._body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._body.setStyleSheet(
            "QTextBrowser{background:transparent;border:none;color:#dfe6f0;"
            "font-size:12px;padding:0px;}"
            "QScrollBar:vertical{width:6px;background:transparent;}"
            "QScrollBar::handle:vertical{background:rgba(150,165,190,0.30);"
            "border-radius:3px;}"
            "QScrollBar::handle:vertical:hover{background:rgba(150,165,190,0.50);}"
        )
        self._body.document().setDocumentMargin(0)
        self._scroll.setWidget(self._body)
        outer.addWidget(self._scroll, 1)

        self._foot = QLabel("")
        self._foot.setStyleSheet("color:#7b8794;font-size:12px;")
        outer.addWidget(self._foot)

    # ---- 几何：按主窗口在屏幕的位置决定 dock 侧与滑入方向 ----
    def _dock_rect(self) -> QRect:
        gm = self._mw.geometry()
        screen = self._mw.screen().availableGeometry()
        ph = min(gm.height(), screen.height() - 8)
        pw = self._pw
        mw_cx = gm.x() + gm.width() / 2
        sc_cx = screen.x() + screen.width() / 2
        if mw_cx >= sc_cx:          # 主窗口在右半屏 -> dock 左侧，从主窗口左边缘向外伸出
            self._side = "left"
            dock_x = gm.x() - pw
        else:                       # 主窗口在左半屏 -> dock 右侧，从主窗口右边缘向外伸出
            self._side = "right"
            dock_x = gm.x() + gm.width()
        dock_x = max(screen.x(), min(dock_x, screen.x() + screen.width() - pw))
        return QRect(dock_x, gm.y(), pw, ph)

    # ---- 开 / 关 / 跟随 ----
    def open(self, block_type: str, title: str, body: str, trigger: str) -> None:
        self._block_type = block_type
        # 主题色：跟随呼吸灯颜色；整体固定深色系，和主界面保持一致。
        ar, ag, ab = _theme_accent()
        self._accent = QColor(ar, ag, ab)
        self._text = "#d8dee9"
        self._foot_col = "#6c7888"
        title_col = _accent_hex((ar, ag, ab))
        self._title.setText(title)
        self._title.setStyleSheet(f"color:{title_col};font-size:14px;font-weight:bold;")
        self._body.setStyleSheet(
            "QTextBrowser{background:transparent;border:none;"
            f"color:{self._text};font-size:12px;padding:0px;}}"
            "QScrollBar:vertical{width:6px;background:transparent;}"
            "QScrollBar::handle:vertical{background:rgba(150,165,190,0.30);"
            "border-radius:3px;}"
            "QScrollBar::handle:vertical:hover{background:rgba(150,165,190,0.50);}"
        )
        self._body.setHtml(_md_to_html(body, (ar, ag, ab)))
        self._foot.setText(f"触发：{trigger}　·　Esc / ✕ 关闭")
        self._foot.setStyleSheet(f"color:{self._foot_col};font-size:12px;")
        # 几何一次性固定到停靠位（全尺寸、主窗口外侧），动画不再改尺寸。
        self._final_rect = self._dock_rect()      # 同时设定 self._side
        self._main_rect = self._mw.geometry()
        self.setFixedSize(self._final_rect.width(), self._final_rect.height())
        self.setGeometry(self._final_rect)
        self.show()
        self._open = True
        # 从完全透明开始，向外淡入（轻量、不卡顿、不触发 DWM 崩溃）。
        self.setWindowOpacity(0.0)
        self._animate_reveal(True)

    def close_popup(self) -> None:
        if not self._open:
            return
        self._open = False
        # 淡出后隐藏（同样轻量）。
        self._animate_reveal(False)

    def reposition(self, instant: bool = True) -> None:
        if self._final_rect is None:
            return
        # 主窗口移动时：停靠位同步更新，弹窗始终贴着主窗口外侧。
        self._final_rect = self._dock_rect()
        self._main_rect = self._mw.geometry()
        self.setGeometry(self._final_rect)

    def is_open(self) -> bool:
        return self._open

    def current_block(self) -> str | None:
        return self._block_type

    def _animate_reveal(self, show: bool) -> None:
        if self._anim is not None:
            self._anim.stop()
        # 直接动画 windowOpacity：frameless + 复杂子控件树下比 setMask 安全得多，
        # 彻底规避 Windows DWM 在「frameless + 复杂子控件树 + setMask」下的原生崩溃。
        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setDuration(150 if show else 120)
        self._anim.setEasingCurve(
            QEasingCurve.Type.OutCubic if show else QEasingCurve.Type.InCubic
        )
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(1.0 if show else 0.0)
        if not show:
            self._anim.finished.connect(self._on_hide_finished)
        self._anim.start()

    def _on_hide_finished(self) -> None:
        self.hide()
        self._block_type = None

    # ---- 磨砂圆角面板 ----
    def paintEvent(self, ev: object) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r = 12
        path = QPainterPath()
        path.addRoundedRect(1, 1, w - 2, h - 2, r, r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._bg)
        p.drawPath(path)
        a = self._accent
        p.setPen(QColor(a.red(), a.green(), a.blue(), self._border_alpha))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()


# ---------------------------------------------------------------- 控制器
class FaqController(QObject):
    """挂载在 _OverlayWindow 上，负责内容查询、触发与生命周期管理。"""

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._win = window
        self._popup = FaqPopup(window)

    # -- 内容查询 ----
    @staticmethod
    def _block_type_of_widget(widget: QWidget) -> str | None:
        p: QWidget | None = widget
        while p is not None:
            bt = getattr(p, "block_type", None)
            if bt:
                return bt
            p = p.parentWidget()
        return None

    def current_block_type(self) -> str | None:
        # 编辑器覆盖层打开时，优先按当前编辑器页面返回 FAQ key
        overlay = getattr(self._win, "_edit_overlay", None)
        if overlay is not None and getattr(overlay, "isVisible", lambda: False)():
            mode = overlay.mode()
        _mode_map = {
            "button": "button_edit",
            "breathing": "breathing_edit",
            "procmon": "procmon_edit",
            "tile": "tile_edit",
            "freeze": "freeze",
            "undervolt": "undervolt",
            "perfmon": "perfmon",
        }
        if mode in _mode_map:
            return _mode_map[mode]
        ctrl = getattr(self._win, "_controller", None)
        items = getattr(ctrl, "_items", []) if ctrl is not None else []
        focus = getattr(ctrl, "_focus", -1)
        if 0 <= focus < len(items):
            bt = self._block_type_of_widget(items[focus].widget)
            if bt:
                return bt
        panels = getattr(self._win, "panels", []) or []
        if panels:
            return getattr(panels[0], "block_type", None)
        return None

    def _content_for(self, block_type: str | None) -> tuple[str, str]:
        if block_type and block_type in FAQ:
            e = FAQ[block_type]
            return e["title"], e["body"]
        return "疑惑解答", "该卡片暂无疑惑解答内容。"

    def _open(self, block_type: str | None, trigger: str) -> None:
        title, body = self._content_for(block_type)
        self._popup.open(block_type, title, body, trigger)

    # -- 触发入口 ----
    def open_for(self, block_type: str | None, trigger: str) -> None:
        if self._popup.is_open() and self._popup.current_block() == block_type:
            self.close()
        else:
            self._open(block_type, trigger)

    def toggle_for_current(self) -> None:
        bt = self.current_block_type()
        if self._popup.is_open() and self._popup.current_block() == bt:
            self.close()
        else:
            self._open(bt, "键盘 F1")

    def open_current(self, trigger: str) -> None:
        self._open(self.current_block_type(), trigger)

    def close(self) -> None:
        self._popup.close_popup()

    def reposition(self) -> None:
        self._popup.reposition(instant=True)

    def is_open(self) -> bool:
        return self._popup.is_open()

    def current_block(self) -> str | None:
        """Return the block_type whose FAQ is currently on screen (or None)."""
        return self._popup.current_block()


# ---------------------------------------------------------------- 自检
def _selftest() -> int:
    from PyQt6.QtWidgets import QApplication
    os_platform = "offscreen"
    import os as _os
    _os.environ.setdefault("QT_QPA_PLATFORM", os_platform)
    QApplication.instance() or QApplication(sys.argv)

    # 用一个最简窗口当主窗口，验证几何与内容逻辑
    mw = QWidget()
    mw.setGeometry(100, 100, 400, 600)
    ctrl = FaqController(mw)
    errs: list[str] = []

    # 1) 右半屏 -> dock 左、从右往左
    screen = mw.screen().availableGeometry()
    mw.setGeometry(screen.x() + screen.width() - 400 - 6, screen.y() + 10, 400, 600)
    dock = ctrl._popup._dock_rect()
    if ctrl._popup._side != "left":
        errs.append("右半屏应 dock 在左")
    if dock.x() != mw.geometry().x() - dock.width():
        errs.append("左侧 dock x 计算错误")

    # 2) 左半屏 -> dock 右、从左往右
    mw.setGeometry(screen.x() + 6, screen.y() + 10, 400, 600)
    dock2 = ctrl._popup._dock_rect()
    if ctrl._popup._side != "right":
        errs.append("左半屏应 dock 在右")
    if dock2.x() != mw.geometry().x() + mw.geometry().width():
        errs.append("右侧 dock x 计算错误")

    # 2b) 停靠位置：左/右 dock 必须紧贴主窗口外侧、不压主窗口（几何固定，
    # 出现动画改用 windowOpacity，不再用 setMask，故此处直接校验停靠几何）。
    mw.setGeometry(screen.x() + screen.width() - 400 - 6, screen.y() + 10, 400, 600)
    ctrl._popup._final_rect = ctrl._popup._dock_rect()   # side == left
    ctrl._popup._main_rect = mw.geometry()
    fr = ctrl._popup._final_rect
    if fr.x() + fr.width() != mw.geometry().x():
        errs.append("左dock：右边缘应贴主窗口左边缘")
    if fr.x() + fr.width() > mw.geometry().x():
        errs.append("左dock：压住了主窗口")

    mw.setGeometry(screen.x() + 6, screen.y() + 10, 400, 600)
    ctrl._popup._final_rect = ctrl._popup._dock_rect()   # side == right
    ctrl._popup._main_rect = mw.geometry()
    fr = ctrl._popup._final_rect
    if fr.x() != mw.geometry().x() + mw.geometry().width():
        errs.append("右dock：左边缘应贴主窗口右边缘")
    if fr.x() < mw.geometry().x() + mw.geometry().width():
        errs.append("右dock：压住了主窗口")
    # 复位，避免影响后续 open/close 测试
    ctrl._popup._final_rect = None
    ctrl._popup._main_rect = None

    # 3) 内容查询
    t, b = ctrl._content_for("rtss")
    if "锁帧控制" not in t:
        errs.append("rtss 标题缺失")
    if "自动锁帧" not in b or "基线" not in b:
        errs.append("rtss 正文缺少自动锁帧/基线说明")

    # 4) 开关状态
    ctrl._open("rtss", "selftest")
    if not ctrl.is_open():
        errs.append("open 失败")
    ctrl.close()
    if ctrl.is_open():
        errs.append("close 失败")

    if errs:
        print("SELFTEST FAIL:")
        for e in errs:
            print("  -", e)
        return 1
    print("SELFTEST PASS: dock 左/右、内容查询、开/关 全部 OK")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())