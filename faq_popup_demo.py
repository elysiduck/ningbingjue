#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
faq_popup_demo.py — 凝冰结魄「疑惑解答弹窗」解耦 Demo (PyQt6)

完全独立：只依赖 PyQt6 与标准库，不 import 任何 src/ 下的模块，
因此不会改动主程序。运行方式（在仓库根目录）：
    runtime/python/python.exe faq_popup_demo.py
headless 自检（无需显示器）：
    runtime/python/python.exe faq_popup_demo.py --selftest

触发方式（三者同时生效，按用户拍板）：
  • 鼠标：点击卡片（标题/正文）或标题栏 / 卡片上的 ? 按钮
  • 键盘：F1 或 H（打开当前选中卡片的疑惑解答）
  • 手柄：L3（左摇杆按下）—— 演示用模拟按钮，真实集成接 InteractionController 的 L3 事件
关闭：✕ / Esc / 再次触发；切换卡片或切换页面时自动关闭。
"""
import os
import sys

from PyQt6.QtCore import (
    Qt, QRect, QPropertyAnimation, QEasingCurve, QTimer, pyqtSignal, QEvent,
)
from PyQt6.QtGui import QColor, QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QPushButton, QScrollArea, QFrame, QCheckBox,
)

# ---------------------------------------------------------------- FAQ 内容
FAQ = {
    "rtss": {
        "title": "锁帧控制 · 自动锁帧",
        "body": (
            "自动锁帧会在你游玩时自动把帧率调到最合适的档位，逻辑如下：\n\n"
            "• 开启后，限帧器始终以「基线 FPS」（默认 30，或你当前设定且 <60 的封顶值）运行。\n"
            "• 每约 1 秒轮询一次：读取 GPU 占用率（经 gpu_util_provider，如 ryzenadj / typeperf），"
            "并确认前台窗口是真实游戏（不是桌面或本程序自身）。\n"
            "• 当 GPU 占用 < 25% 且前台是游戏，连续 3 次轮询（约 3 秒）→ 自动把帧率顶到 60"
            "（显示「已顶60」），让 2D 菜单 / 读盘画面更顺滑。\n"
            "• 当 GPU 占用回到 ≥25% 或前台窗口丢失，连续 2 次轮询（约 2 秒）→ 回落到基线 FPS。\n"
            "• 没有 GPU 监控来源时，按钮置灰显示「自动锁帧: 不可用」。\n"
            "• 关闭「锁帧」会一并关闭自动锁帧；配置保存的是基线值而非瞬时的 60，重启后从 3D 封顶档开始。"
        ),
    },
    "power": {
        "title": "电源 · TDP / 显卡频率",
        "body": (
            "拖动滑块设定 CPU 的 TDP（功耗上限）与显卡频率偏移。\n\n"
            "• 功耗越低越省电、温度更低，但性能受限；\n"
            "• 调高则释放更多性能，但更热、更耗电。\n"
            "改动会实时写入 RyzenAdj / 显卡驱动，适合在不同游戏间快速切换性能档位。"
        ),
    },
    "display": {
        "title": "显示 · 分辨率 / 刷新率",
        "body": (
            "调整主显示器的分辨率与全局刷新率。\n\n"
            "• 降低分辨率可换取更高帧率；\n"
            "• 提高刷新率让画面更顺滑，但需要显示器与线缆（如 DP 1.4+）支持。\n"
            "注意：部分全屏游戏会忽略全局设置，需在游戏内单独调整。"
        ),
    },
    "audio": {
        "title": "音频 · 设备 / 音量",
        "body": (
            "快捷切换音频输出设备与系统音量，并支持一键静音。\n\n"
            "常用于游戏中快速在耳机 / 扬声器之间切换，或临时静音避免打扰。"
        ),
    },
}

# 页面 -> 卡片顺序（模拟真实分页卡片）
PAGES = [
    ("性能", ["rtss", "power"]),
    ("画面", ["display", "audio"]),
]


# ---------------------------------------------------------------- 弹窗本体
class FaqPopup(QWidget):
    def __init__(self, parent_window):
        super().__init__()
        self._mw = parent_window
        self._pw = 320
        self._side = "left"
        self._open = False
        self._anim = None
        self._hover_card = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        top = QHBoxLayout()
        self._title = QLabel("")
        self._title.setStyleSheet("color:#7fd1ff;font-size:14px;font-weight:bold;")
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
        self._body = QLabel("")
        self._body.setWordWrap(True)
        self._body.setStyleSheet("color:#dfe6f0;font-size:12px;line-height:1.55;")
        self._scroll.setWidget(self._body)
        outer.addWidget(self._scroll, 1)

        self._foot = QLabel("")
        self._foot.setStyleSheet("color:#7b8794;font-size:11px;")
        outer.addWidget(self._foot)

    # ---- 几何：按主窗口在屏幕的位置决定 dock 侧与滑入方向 ----
    def _dock_rect(self):
        gm = self._mw.geometry()
        screen = self._mw.screen().availableGeometry()
        ph = min(gm.height(), screen.height() - 8)
        pw = self._pw
        mw_cx = gm.x() + gm.width() / 2
        sc_cx = screen.x() + screen.width() / 2
        if mw_cx >= sc_cx:          # 主窗口在右半屏 -> dock 左侧，从右往左滑
            self._side = "left"
            dock_x = gm.x() - pw
        else:                       # 主窗口在左半屏 -> dock 右侧，从左往右滑
            self._side = "right"
            dock_x = gm.x() + gm.width()
        dock_x = max(screen.x(), min(dock_x, screen.x() + screen.width() - pw))
        return QRect(dock_x, gm.y(), pw, ph)

    def _tucked_rect(self, dock):
        # 起点：藏在主窗口内侧边缘之后，再滑出到 dock 位
        if self._side == "left":
            return QRect(dock.x() + dock.width(), dock.y(), dock.width(), dock.height())
        return QRect(dock.x() - dock.width(), dock.y(), dock.width(), dock.height())

    # ---- 开 / 关 / 跟随 ----
    def open(self, title, body, trigger, from_card=None):
        self.update_content(title, body, trigger, from_card)
        dock = self._dock_rect()
        self.setFixedSize(dock.width(), dock.height())
        self.setGeometry(self._tucked_rect(dock))
        self.show()
        self.raise_()
        self._open = True
        self._animate_to(dock, 260, QEasingCurve.Type.OutCubic)

    def close_popup(self):
        if not self._open:
            return
        self._open = False
        self._hover_card = None
        dock = self.geometry()
        tucked = self._tucked_rect(dock)
        self._animate_to(tucked, 200, QEasingCurve.Type.InCubic, hide_on_finish=True)

    def update_content(self, title, body, trigger, from_card=None):
        self._title.setText(title)
        self._body.setText(body)
        self._foot.setText(f"触发：{trigger}　·　Esc / ✕ 关闭")
        self._hover_card = from_card

    def reposition(self, instant=True):
        if not self._open:
            return
        dock = self._dock_rect()
        if instant:
            self.setGeometry(dock)

    def is_open(self):
        return self._open

    def _animate_to(self, rect, dur, easing, hide_on_finish=False):
        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(dur)
        self._anim.setEasingCurve(easing)
        self._anim.setStartValue(self.geometry())
        self._anim.setEndValue(rect)
        if hide_on_finish:
            self._anim.finished.connect(self.hide)
        self._anim.start()

    # ---- 磨砂圆角面板 ----
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r = 12
        path = QPainterPath()
        path.addRoundedRect(1, 1, w - 2, h - 2, r, r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(16, 20, 28, 236))
        p.drawPath(path)
        accent = QColor(127, 209, 255)
        p.setPen(QColor(accent.red(), accent.green(), accent.blue(), 210))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()


# ---------------------------------------------------------------- 卡片
class Card(QFrame):
    def __init__(self, key, title, on_help, on_focus):
        super().__init__()
        self.key = key
        self._title = title
        self._on_help = on_help
        self._on_focus = on_focus
        self._focused = False
        self.setMinimumHeight(64)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        self._label = QLabel(title)
        self._label.setStyleSheet("color:#dfe6f0;font-size:13px;font-weight:bold;")
        lay.addWidget(self._label, 1)
        help_btn = QPushButton("?")
        help_btn.setFixedSize(26, 26)
        help_btn.setStyleSheet(
            "QPushButton{background:rgba(127,209,255,0.12);color:#a9d8ff;"
            "border:1px solid rgba(127,209,255,0.28);border-radius:6px;}"
            "QPushButton:hover{background:rgba(127,209,255,0.22);}"
        )
        help_btn.clicked.connect(lambda: self._on_help(self.key))
        lay.addWidget(help_btn)

    def set_focused(self, v):
        self._focused = v
        self.update()

    def mousePressEvent(self, ev):
        # 鼠标点击卡片（标题/正文）= 打开该卡片的疑惑解答
        self._on_focus(self.key, "mouse")

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r = 10
        path = QPainterPath()
        path.addRoundedRect(1, 1, w - 2, h - 2, r, r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(20, 24, 33, 200))
        p.drawPath(path)
        if self._focused:
            col = QColor(127, 209, 255)
            a = 230
        else:
            col = QColor(127, 209, 255)
            a = 110
        p.setPen(QColor(col.red(), col.green(), col.blue(), a))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()


# ---------------------------------------------------------------- 主窗口(Demo)
class DemoMainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.page_idx = 0
        self.focus_key = None
        self._follow = True
        self._drag = None
        self._cards = {}

        self._build_ui()
        self.faq = FaqPopup(self)
        self._position_default()
        QApplication.instance().installEventFilter(self)

    def _position_default(self):
        screen = self.screen().availableGeometry()
        w = 420
        h = min(680, screen.height() - 20)
        x = screen.x() + screen.width() - w - 6
        y = screen.y() + 10
        self.setGeometry(x, y, w, h)

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        hdr = QHBoxLayout()
        title = QLabel("凝冰结魄 · 覆盖任务栏 HUD (Demo)")
        title.setStyleSheet("color:#eaf2ff;font-size:14px;font-weight:bold;")
        hdr.addWidget(title, 1)
        self._help_btn = QPushButton("?")
        self._help_btn.setFixedSize(28, 28)
        self._help_btn.setStyleSheet(
            "QPushButton{background:rgba(127,209,255,0.12);color:#a9d8ff;"
            "border:1px solid rgba(127,209,255,0.28);border-radius:7px;}"
            "QPushButton:hover{background:rgba(127,209,255,0.22);}"
        )
        self._help_btn.clicked.connect(lambda: self._on_help_button())
        hdr.addWidget(self._help_btn)
        outer.addLayout(hdr)

        # 跟随开关
        fl = QHBoxLayout()
        self._follow_chk = QCheckBox("跟随主窗口拖动")
        self._follow_chk.setChecked(True)
        self._follow_chk.setStyleSheet("color:#dfe6f0;")
        self._follow_chk.stateChanged.connect(lambda v: setattr(self, '_follow', bool(v)))
        fl.addWidget(self._follow_chk)
        fl.addStretch(1)
        outer.addLayout(fl)

        mv = QHBoxLayout()
        for label, side in (("移到右半屏 ▶", "right"), ("◀ 移到左半屏", "left")):
            b = QPushButton(label)
            b.setStyleSheet(
                "QPushButton{background:rgba(255,255,255,0.08);color:#dfe6f0;"
                "border:1px solid rgba(255,255,255,0.15);border-radius:6px;padding:4px 8px;}"
                "QPushButton:hover{background:rgba(255,255,255,0.16);}"
            )
            b.clicked.connect(lambda _=False, s=side: self._move_to(s))
            mv.addWidget(b)
        self._pad_btn = QPushButton("🎮 模拟手柄 L3 (左摇杆按下)")
        self._pad_btn.setStyleSheet(
            "QPushButton{background:rgba(255,209,102,0.12);color:#ffd166;"
            "border:1px solid rgba(255,209,102,0.28);border-radius:6px;padding:4px 8px;}"
            "QPushButton:hover{background:rgba(255,209,102,0.22);}"
        )
        self._pad_btn.clicked.connect(self._on_gamepad_l3)
        mv.addWidget(self._pad_btn)
        outer.addLayout(mv)

        self._page_bar = QHBoxLayout()
        outer.addLayout(self._page_bar)
        self._cards_host = QVBoxLayout()
        self._cards_host.setSpacing(8)
        outer.addLayout(self._cards_host)

        legend = QLabel(
            "触发（三者同时生效）：①鼠标=点卡片 / 标题栏? / 卡片?　"
            "②键盘=F1 或 H（↑↓ 切换卡片会自动关）　③手柄=L3（左摇杆按下）。\n"
            "关闭：✕ / Esc / 再次触发；切换卡片或切换页面自动关闭。弹窗紧贴主窗口内侧、随其在屏幕的位置自适应滑入。"
        )
        legend.setWordWrap(True)
        legend.setStyleSheet("color:#7b8794;font-size:11px;line-height:1.4;")
        outer.addWidget(legend)

        self._rebuild_page()

    def _rebuild_page(self):
        while self._page_bar.count():
            w = self._page_bar.takeAt(0).widget()
            if w:
                w.deleteLater()
        while self._cards_host.count():
            w = self._cards_host.takeAt(0).widget()
            if w:
                w.deleteLater()
        self._cards.clear()

        for i, (pname, _) in enumerate(PAGES):
            b = QPushButton(pname)
            b.setFixedSize(40, 26)
            active = i == self.page_idx
            b.setStyleSheet(
                "QPushButton{background:%s;color:%s;border:1px solid rgba(255,209,102,0.5);"
                "border-radius:6px;}" % (
                    "rgba(255,209,102,0.22)" if active else "rgba(255,255,255,0.08)",
                    "#ffd166" if active else "#dfe6f0",
                )
            )
            b.clicked.connect(lambda _=False, idx=i: self._goto_page(idx))
            self._page_bar.addWidget(b)
        self._page_bar.addStretch(1)

        _, keys = PAGES[self.page_idx]
        for key in keys:
            card = Card(key, FAQ[key]["title"].split(" · ")[0],
                        self._on_card_help, self._on_card_focus)
            self._cards_host.addWidget(card)
            self._cards[key] = card
        self.focus_key = keys[0]
        self._refresh_focus()

    def _refresh_focus(self):
        for k, c in self._cards.items():
            c.set_focused(k == self.focus_key)

    def _goto_page(self, idx):
        if self.faq.is_open():
            self.faq.close_popup()          # 切换页面自动关闭
        self.page_idx = idx
        self._rebuild_page()

    def _on_help_button(self):
        self._toggle_faq("鼠标 标题栏?", self.focus_key)

    def _on_card_help(self, key):
        self._activate_card(key, "鼠标 卡片?")

    def _on_card_focus(self, key, via):
        self.focus_key = key
        self._refresh_focus()
        if via == "mouse":
            self._activate_card(key, "鼠标 点击卡片")
        elif via == "key":
            if self.faq.is_open():
                self.faq.close_popup()      # 键盘切换卡片自动关闭

    def _on_gamepad_l3(self):
        # 真实集成时：InteractionController 在 L3 事件里调用此方法即可。
        self._toggle_faq("手柄 L3", self.focus_key)

    def _current_content(self, key=None):
        k = key or self.focus_key
        return FAQ.get(k, FAQ[self.focus_key])

    def _open_faq(self, trigger, key=None):
        c = self._current_content(key)
        self.faq.open(c["title"], c["body"], trigger, from_card=key)

    def _activate_card(self, key, trigger):
        # 鼠标点同一张已打开的卡 -> 关；点别的卡 -> 切内容；未开 -> 开
        if self.faq.is_open() and self.faq._hover_card == key:
            self.faq.close_popup()
        elif self.faq.is_open():
            c = self._current_content(key)
            self.faq.update_content(c["title"], c["body"], trigger, from_card=key)
        else:
            self._open_faq(trigger, key)

    def _toggle_faq(self, trigger, key=None):
        if self.faq.is_open():
            self.faq.close_popup()
        else:
            self._open_faq(trigger, key)

    def _move_to(self, side):
        screen = self.screen().availableGeometry()
        w = self.width()
        h = self.height()
        x = (screen.x() + screen.width() - w - 6) if side == "right" else (screen.x() + 6)
        anim = QPropertyAnimation(self, b"geometry")
        anim.setDuration(400)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(self.geometry())
        anim.setEndValue(QRect(x, self.y(), w, h))
        anim.start()
        self._move_anim = anim

    # ---- 拖动 ----
    def mousePressEvent(self, ev):
        if ev.position().y() <= 44:
            self._drag = ev.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, ev):
        if self._drag is not None:
            self.move(ev.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, ev):
        self._drag = None

    def moveEvent(self, ev):
        super().moveEvent(ev)
        if getattr(self, 'faq', None) and self.faq.is_open() and self._follow:
            self.faq.reposition(instant=True)

    # ---- 全局按键 ----
    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.KeyPress:
            key = ev.key()
            if key in (Qt.Key.Key_F1, Qt.Key.Key_H):
                self._toggle_faq("键盘 F1", self.focus_key)
                return True
            if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                self._nav_focus(1 if key == Qt.Key.Key_Down else -1)
                return True
            if key == Qt.Key.Key_Escape and self.faq.is_open():
                self.faq.close_popup()
                return True
        return super().eventFilter(obj, ev)

    def _nav_focus(self, delta):
        keys = PAGES[self.page_idx][1]
        if self.focus_key not in keys:
            return
        i = keys.index(self.focus_key)
        ni = (i + delta) % len(keys)
        self._on_card_focus(keys[ni], "key")

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        path = QPainterPath()
        path.addRoundedRect(1, 1, w - 2, h - 2, 12, 12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(14, 17, 23, 240))
        p.drawPath(path)
        p.setPen(QColor(127, 209, 255, 120))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()


# ---------------------------------------------------------------- 自检
def run_selftest(app, win):
    errs = []
    screen = win.screen().availableGeometry()

    # 1) 右半屏 -> dock 左、从右往左
    win.setGeometry(screen.x() + screen.width() - 420 - 6, screen.y() + 10, 420, 600)
    dock = win.faq._dock_rect()
    if win.faq._side != "left":
        errs.append("右半屏应 dock 在左")
    if dock.x() != win.geometry().x() - dock.width():
        errs.append("左侧 dock x 计算错误")

    # 2) 左半屏 -> dock 右、从左往右
    win.setGeometry(screen.x() + 6, screen.y() + 10, 420, 600)
    dock2 = win.faq._dock_rect()
    if win.faq._side != "right":
        errs.append("左半屏应 dock 在右")
    if dock2.x() != win.geometry().x() + win.geometry().width():
        errs.append("右侧 dock x 计算错误")

    # 3) 内容查询
    if "自动锁帧" not in FAQ["rtss"]["title"]:
        errs.append("rtss 标题缺失")
    c = win._current_content("rtss")
    if "基线" not in c["body"]:
        errs.append("rtss 正文缺少基线说明")

    # 4) 开关状态
    win._open_faq("selftest", "rtss")
    if not win.faq.is_open():
        errs.append("open 失败")
    win.faq.close_popup()
    if win.faq.is_open():
        errs.append("close 失败")

    # 5) 切换页面应自动关闭
    win._open_faq("selftest", "rtss")
    win._goto_page(1)
    if win.faq.is_open():
        errs.append("切换页面未自动关闭")

    if errs:
        print("SELFTEST FAIL:")
        for e in errs:
            print("  -", e)
        sys.exit(1)
    print("SELFTEST PASS: dock 左/右、内容查询、开/关、切页自动关 全部 OK")


def main():
    selftest = "--selftest" in sys.argv
    if selftest:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication(sys.argv)
    win = DemoMainWindow()
    if selftest:
        run_selftest(app, win)
        return
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
