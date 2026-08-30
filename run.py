#!/usr/bin/env python3
"""Launcher for the 凝冰结魄 (NingBingJiePo) Python port.

Usage
-----
  python run.py                 # headless engine smoke (cross-platform, safe)
  python run.py --gui           # real PyQt6 overlay (Windows + PyQt6 only)
  python run.py --root D:/path  # point at the original script folder

The headless mode boots all 6 blocks, fires their OnRefresh hooks, prints a
live verification table, and exercises a couple of shims — proving the core
engine runs without a display.  The GUI mode builds a translucent, top-most
overlay window that hosts one live panel per block and refreshes every second.

The script auto-adds ``src/`` to sys.path so ``import ningbingjue`` works from
the project root without extra environment setup.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import threading
import time

# Make the package importable when run from the project root.
_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_ROOT, "src"))

_LOG_PATH = os.path.join(_ROOT, "launch.log")


def log(msg: str) -> None:
    """Echo to the console AND append to launch.log for post-mortem debugging."""
    text = str(msg)
    print(text)
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%H:%M:%S") + " " + text + "\n")
    except Exception:  # noqa: BLE001
        pass


# -- 阶段 1：外部源码覆盖（多设备同步测试解锁）---------------------------
# 掌机用 installer 冻的 exe 跑，但开发迭代只改 src/*.py。为免每次重建 exe，
# 这里允许 exe 启动时优先加载「外部 _ext/src」shadow 掉冻结模块。真正的同步
# 由掌机 app 内「开发同步」面板（edit_overlay.DevSyncEditPanel）拉取/重启完成；
# 本段只负责「启动时若外部源码可用则启用」，并做失败回退，保证 app 永远能开。
def _apply_external_src_override() -> str | None:
    """若外部源码目录存在且全部 .py 可编译，插到 sys.path 最前覆盖冻结模块。

    返回实际生效的外部 src 路径；None = 未启用（用冻结/内置源码）。
    安全性（决策 1：先校验再切换 + 失败回退）：仅当目录内所有 .py 通过
    py_compile 才启用；任一文件语法错则记录告警并回退，避免坏同步锁死掌机。
    仅冻结构建（或存在 _ext/src）时生效；开发模式无 _ext/src 则空过。
    """
    try:
        if getattr(sys, "frozen", False):
            base = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base = _ROOT
        cfg_path = os.path.join(base, "sync_config.json")
        ext_src = None
        if os.path.isfile(cfg_path):
            try:
                import json

                with open(cfg_path, "r", encoding="utf-8") as _fh:
                    _cfg = json.load(_fh)
                ext_src = _cfg.get("local_ext_src")
            except Exception:  # noqa: BLE001
                ext_src = None
        if not ext_src:
            ext_src = os.path.join(base, "_ext", "src")
        if not os.path.isdir(ext_src):
            return None
        # 编译校验：任一 .py 语法错则放弃覆盖，回退冻结模块。
        import py_compile

        _bad = []
        for _root, _dirs, _files in os.walk(ext_src):
            _dirs[:] = [d for d in _dirs if d != "__pycache__"]
            for _fn in _files:
                if _fn.endswith(".py"):
                    _fp = os.path.join(_root, _fn)
                    try:
                        py_compile.compile(_fp, doraise=True, quiet=2)
                    except Exception:  # noqa: BLE001
                        _bad.append(_fp)
        if _bad:
            log(f"[外部源码覆盖] 跳过：{len(_bad)} 个文件编译失败，回退冻结模块")
            for _b in _bad[:5]:
                log("   ✗ " + _b)
            return None
        sys.path.insert(0, ext_src)
        log(f"[外部源码覆盖] 已启用外部源码: {ext_src}")
        return ext_src
    except Exception as exc:  # noqa: BLE001
        log("[外部源码覆盖] 异常: " + str(exc))
        return None


_apply_external_src_override()


# -- crash forensics (凝冰结魄快捷键唤出闪退专项) -----------------------------
# 偶发"进程静默消失、无 Python 栈"= C 层原生崩溃(access violation)。
# faulthandler 在 Windows 上经 SetUnhandledExceptionFilter 捕获致命错误，
# 把当时活动的 Python 栈打印出来；sys.excepthook 兜底记录未处理异常。
# 两者都只落盘、不弹窗、不阻塞，正常路径零副作用。
import faulthandler as _faulthandler  # noqa: E402

try:
    # 把 faulthandler 的原生崩溃栈重定向到文件。冻结的 --windowed 构建里 stderr
    # 被丢弃，access violation / 段错误会变成「进程静默消失、无任何 Python 栈」
    # 的闪退，无法定位。写到磁盘后，下一次原生崩溃会留下真实的活动 Python 栈
    # （通常是触发 Qt 原生崩溃的那一帧：paintEvent / 手柄轮询槽等），便于事后定位。
    _crash_dir = os.path.join(_ROOT, "logs")
    try:
        os.makedirs(_crash_dir, exist_ok=True)
    except Exception:  # noqa: BLE001
        _crash_dir = _ROOT
    _crash_path = os.path.join(
        _crash_dir, "crash_" + time.strftime("%Y%m%d_%H%M%S") + ".log"
    )
    # 行缓冲（buffering=1）保证崩溃瞬间也能落盘。
    _crash_fh = open(_crash_path, "a", encoding="utf-8", buffering=1)
    _faulthandler.enable(file=_crash_fh)
    _faulthandler.dump_traceback_on_fault()  # 段错误等致命错误时自动 dump
except Exception:  # noqa: BLE001
    try:
        _faulthandler.enable()
        _faulthandler.dump_traceback_on_fault()
    except Exception:  # noqa: BLE001
        pass


def _crash_excepthook(etype, evalue, tb) -> None:  # pragma: no cover
    """Log unhandled exceptions to launch.log before the process dies."""
    try:
        import traceback as _tb

        log("‼ 未处理异常(进程即将退出):")
        for line in _tb.format_exception(etype, evalue, tb):
            log(line.rstrip("\n"))
    except Exception:  # noqa: BLE001
        pass


sys.excepthook = _crash_excepthook


def _thread_excepthook(args) -> None:  # pragma: no cover
    """后台线程未捕获异常的兜底：默认只打到被丢弃的 stderr（冻结构建里消失无踪）。
    这里改写到 launch.log，让线程里的崩溃也能被事后定位。"""
    try:
        import traceback as _tb

        exc = getattr(args, "exc_value", None)
        log("‼ 后台线程未捕获异常(线程=" + str(getattr(args, "thread_name", "?")) + "):")
        for line in _tb.format_exception(
            getattr(args, "exc_type", Exception),
            exc,
            getattr(args, "exc_traceback", None),
        ):
            log(line.rstrip("\n"))
    except Exception:  # noqa: BLE001
        pass


threading.excepthook = _thread_excepthook

# 解析优先级（见 resolve_root）：--root 参数 > 环境变量 NINGBING_ROOT > 本遗留默认路径 > 当前工作目录。
# 本默认路径仅作为旧备份位置的兜底，部署/迁移时应通过 --root 或设置 NINGBING_ROOT 显式指定，
# 切勿依赖此硬编码路径。
DEFAULT_ROOT = (
    r"D:\下载\localsend\凝冰结魄-覆盖任务栏-修复-完全解耦-备份-20260717172824"
    r"\凝冰结魄-覆盖任务栏-修复-完全解耦-备份"
)


def resolve_root(arg: str | None) -> str:
    if arg:
        return arg
    env = os.environ.get("NINGBING_ROOT")
    if env:
        return env
    if os.path.isdir(DEFAULT_ROOT):
        return DEFAULT_ROOT
    return os.getcwd()


def resolve_config_path(root: str) -> str:
    """配置文件的绝对路径（layout.json）。

    关键修复：冻结 exe 与源码共享「同一份」配置，避免两处 layout.json
    互相掩盖导致「改了又恢复默认值」的反复排查。

    优先级：
      1. 显式 --config 参数（main 透传）
      2. 环境变量 NINGBING_CONFIG
      3. 源码遗留的 D:备份 layout.json（若该文件存在，说明用户一直用它做
         为真相源，exe 必须复用它，而不是写自己 _internal 里的默认副本）
      4. 退化为 <script_root>/layout.json（便携部署到其他机器、D:备份不存在时
         仍能自包含运行）
    """
    env = os.environ.get("NINGBING_CONFIG")
    if env:
        return env
    candidate = os.path.join(DEFAULT_ROOT, "layout.json")
    if os.path.isfile(candidate):
        return candidate
    return os.path.join(root, "layout.json")


def run_headless(root: str, config_path: str | None = None) -> int:
    from ningbingjue.app import Application

    app = Application(root, headless=True, config_path=config_path).boot()

    print("=" * 62)
    print(f"凝冰结魄 引擎自检 · script_root = {root}")
    print("=" * 62)

    ok = True
    for block_type in app.registry:
        block = app.registry.get(block_type)
        instance = app.blocks.get(block_type)
        try:
            hook = block.hook("OnRefresh") if block else None
            if callable(hook):
                hook()
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  [!] {block_type:<8} refresh error: {exc}")
            continue
        status = ""
        try:
            status = instance.status() if instance and hasattr(instance, "status") else ""
        except Exception:  # noqa: BLE001
            status = ""
        name = block.name if block else block_type
        print(f"  ✓ {name:<14} {status}")

    # Exercise a couple of backward-compatible shims.
    try:
        if "Toggle-Lock" in app.shims:
            app.shims["Toggle-Lock"]()
            app.shims["Toggle-Lock"]()
        if "Toggle-GpuFloat" in app.shims:
            app.shims["Toggle-GpuFloat"]()
            app.shims["Toggle-GpuFloat"]()
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  [!] shim exercise error: {exc}")

    try:
        app.save_all()
    except Exception:  # noqa: BLE001
        pass

    print("-" * 62)
    print("自检结果:", "通过 ✅" if ok else "存在错误 ❌")
    return 0 if ok else 1


def run_gui(root: str, config_path: str | None = None) -> int:
    log("[启动] run_gui 开始 · root=" + root)
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: F401
        from PyQt6.QtCore import PYQT_VERSION_STR  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        log("PyQt6 不可用，无法启动 GUI。请先安装依赖：pip install -r requirements.txt")
        log("(" + str(exc) + ")")
        return 2

    # 补 Qt 致命消息钩子：faulthandler 只拦 SEH 原生故障，漏掉 Qt 自带的
    # qFatal/assert(abort)，而「QThread: Destroyed while thread still running」
    # 等典型闪退正是走 Qt 的 abort。装上后这些致命消息会落到 launch.log，且
    # FATAL 时先让 faulthandler 把各线程栈 dump 到 logs/crash_*.log，使下次
    # 「进程静默消失」也能留下真实证据（这正是此前崩溃日志全空的根因盲区）。
    try:
        from PyQt6.QtCore import qInstallMessageHandler, QtMsgType

        def _qt_message_handler(msg_type, context, message):  # pragma: no cover
            try:
                _level = {
                    QtMsgType.QtDebugMsg: "DEBUG",
                    QtMsgType.QtInfoMsg: "INFO",
                    QtMsgType.QtWarningMsg: "WARN",
                    QtMsgType.QtCriticalMsg: "CRIT",
                    QtMsgType.QtFatalMsg: "FATAL",
                }.get(msg_type, "???")
                _loc = getattr(context, "file", "") or ""
                _line = getattr(context, "line", 0) or 0
                log(f"[Qt:{_level}] {message} ({_loc}:{_line})")
                if msg_type == QtMsgType.QtFatalMsg:
                    # Qt 即将 abort 本进程；先让 faulthandler 把各线程栈落盘。
                    try:
                        _faulthandler.dump_traceback(all_threads=True)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass

        qInstallMessageHandler(_qt_message_handler)
    except Exception:  # noqa: BLE001
        pass

    from ningbingjue.app import Application

    try:
        # --- High-DPI setup (must happen before QApplication is created) ---
        # Without process DPI awareness Windows bitmap-stretches the window on
        # scaled displays, which makes the whole HUD look distorted ("比例失常").
        #
        # 关键：优先声明「每显示器 V2」(PerMonitorV2)。旧的 PROCESS_PER_MONITOR_DPI_AWARE
        # (level 2) 在多屏 / 外接屏场景下仍可能按主屏 DPR 渲染，导致元素在别的屏幕上
        # 缩放失常（过小或过大）。V2 让每个显示器都拿到正确的 devicePixelRatio，配合
        # app_window 里的 screenChanged -> _fit_to_screen 自动重适配。
        #
        # 冻结 exe 已由 nbj_manifest.xml 在进程启动时就设好 V2；这里再调用一次只是为
        # 覆盖「直接用 python run.py --gui」这类无清单的路径，且若清单已设定会被静默
        # 拒绝（E_ACCESSDENIED），属正常，无需处理。
        try:
            import ctypes

            # DPI 感知必须设在 QApplication 创建之前；否则整窗在缩放屏上被
            # Windows 按 DPR 位图拉伸（UNAWARE），导致 HUD 全屏发糊 + 窗口溢出
            # 裁掉卡片边缘高亮。冻结 exe 由 nbj_manifest.xml 在进程启动即设好
            # V2；这里覆盖「python run.py --gui」这类无清单路径。
            #
            # 关键 BUG 修复：SetProcessDpiAwarenessContext 的参数是「指针宽度」的
            # DPI_AWARENESS_CONTEXT 值（-4）。原代码直接传 -4 且未声明 argtypes，
            # ctypes 默认按 32 位 c_int 传入，64 位下被截断成垃圾指针 -> 函数
            # 静默返回 FALSE（不抛异常）-> 既未生效也未触发下方 V1 兜底，进程
            # 停留在 UNAWARE。现补正确 argtypes 并按返回值决定是否兜底。
            _dpi_set = False
            try:
                _user32 = ctypes.windll.user32
                _user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                _user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
                # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4（指针宽度）
                if _user32.SetProcessDpiAwarenessContext(
                        ctypes.c_void_p(-4 & 0xFFFFFFFFFFFFFFFF)):
                    _dpi_set = True
            except Exception:  # noqa: BLE001
                pass
            if not _dpi_set:
                # 兜底：V2 不可用（<Win10 1703 等）退回每显示器 V1，仍优于系统级。
                try:
                    _shcore = ctypes.windll.shcore
                    _shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
                    _shcore.SetProcessDpiAwareness.restype = ctypes.c_long
                    # PROCESS_PER_MONITOR_DPI_AWARE = 2
                    _shcore.SetProcessDpiAwareness(2)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        from PyQt6.QtCore import Qt

        # 必须在 QApplication 实例化之前设置（Qt 硬性要求）。否则会打印
        # "setHighDpiScaleFactorRoundingPolicy must be called before creating
        # the QGuiApplication instance" 且策略不生效（高 DPI 屏上窗口比例失常/糊）。
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        log("[启动] PyQt6 版本=" + PYQT_VERSION_STR)
        qapp = QApplication.instance() or QApplication(sys.argv)
        # 为自动锁帧的跨线程 UI 更新建立主线程马歇尔（必须在主线程创建，
        # 否则信号收不到主线程事件循环）。失败不阻断（降级为同步，仅失保护）。
        try:
            from ningbingjue.blocks.rtss import ensure_gui_bridge
            ensure_gui_bridge()
        except Exception:  # noqa: BLE001
            pass
        # 全局隐藏所有自建 QScrollArea 的纵向滚动条（滚轮/方向键仍可静默滚动）。
        # 见 ningbingjue/ui/_no_vscroll.py —— 覆盖当前与未来所有滚动区，原生
        # 控件（下拉框/列表框/文本框）内部滚动条不受影响。
        from ningbingjue.ui._no_vscroll import install_vertical_scrollbar_hider
        install_vertical_scrollbar_hider(qapp)
        # 锁定全局字体，防止 Windows 辅助功能「文本大小」放大 HUD 文字。
        _freeze_application_font()
        log("[启动] QApplication 已创建")

        # 启动诊断：确认进程实际的 DPI 感知等级与 effective devicePixelRatio。
        # 若 awareness=UNAWARE(0) 或 dpr=1.0，则整窗会被 Windows 按 DPR 位图拉伸
        # -> HUD 全屏糊 + 窗口溢出裁边。此日志用于掌机实测时核对根因是否消除。
        try:
            from PyQt6.QtGui import QGuiApplication
            from PyQt6.QtCore import Qt
            scr = QGuiApplication.primaryScreen()
            dpr = scr.devicePixelRatio() if scr is not None else None
            try:
                _sh = ctypes.windll.shcore
                _sh.GetProcessDpiAwareness.argtypes = [
                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
                _sh.GetProcessDpiAwareness.restype = ctypes.c_long
                _aw = ctypes.c_int(0)
                _sh.GetProcessDpiAwareness(
                    ctypes.windll.kernel32.GetCurrentProcess(),
                    ctypes.byref(_aw))
                _names = {0: "UNAWARE", 1: "SYSTEM_AWARE",
                          2: "PER_MONITOR", 3: "PER_MONITOR_V2"}
                # 系统真实 DPI（绕过 UNAWARE 的欺骗）：若 awareness=UNAWARE 但
                # 系统其实是 150%，则整窗一定被位图拉伸 -> 文字颗粒化。
                _sys_dpi = -1
                try:
                    _u = ctypes.windll.user32
                    _u.GetDpiForSystem.argtypes = []
                    _u.GetDpiForSystem.restype = ctypes.c_uint
                    _sys_dpi = int(_u.GetDpiForSystem())
                except Exception:  # noqa: BLE001
                    _sys_dpi = -1
                _verdict = "" if _aw.value in (2, 3) else \
                    "  ⚠️ UNAWARE：整窗将被 Windows 按缩放比位图拉伸，文字颗粒化！"
                log(f"[启动诊断] DPI awareness={_aw.value} "
                    f"({_names.get(_aw.value, '?')}) dpr={dpr} "
                    f"系统真实DPI={_sys_dpi}{_verdict}")
            except Exception as _e:  # noqa: BLE001
                log(f"[启动诊断] DPI 查询失败: {_e} dpr={dpr}")
        except Exception as _e:  # noqa: BLE001
            log(f"[启动诊断] 跳过: {_e}")

        log("[启动] 开始 boot() ...")
        app = Application(root, headless=False, config_path=config_path).boot()
        log("[启动] boot() 完成")

        window = getattr(app, "_window", None)
        if window is None:
            log("[错误] boot() 未创建窗口 (_window 为空)")
            return 4
        g = window.geometry()
        log(
            f"[启动] 窗口已创建 · 位置=({g.x()},{g.y()}) "
            f"大小={window.width()}x{window.height()} 可见={window.isVisible()}"
        )
        window.raise_()
        window.activateWindow()

        log("[启动] 进入事件循环 qapp.exec() ...")
        t0 = time.time()
        rc = qapp.exec()
        dt = time.time() - t0
        log(f"[退出] qapp.exec() 返回 rc={rc} · 运行 {dt:.1f}s")
        if dt < 1.0:
            log("[警告] 事件循环几乎立即退出，窗口可能未正常显示（检查显示器/多屏/DPI/被遮挡）")
        return rc
    except Exception as exc:  # noqa: BLE001
        import traceback

        tb = traceback.format_exc()
        log("启动失败：\n" + tb)
        try:
            q = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, "凝冰结魄 启动失败", tb[-3000:])
        except Exception:  # noqa: BLE001
            pass
        return 3


def _freeze_application_font() -> None:
    """锁定全局字体，使其不被 Windows「辅助功能 → 文本大小」放大。

    凝冰结魄从不为 QApplication 设置字体，于是所有「未显式指定 font-size」
    的控件（按钮、下拉框、列表项、消息框、标签页文字等）都会继承 Qt 的默认
    字体——而 Windows 上这个默认字体就是**系统 GUI 字体**。一旦用户在
    「设置 → 辅助功能 → 文本大小」里放大文字，系统 GUI 字号变大，所有继承
    字体的文字就跟着变大，撑爆 HUD 布局。

    修复办法：在 QApplication 创建后、任何控件创建前，显式锁定一个固定字号
    的全局字体（保留系统字体族以保证中文回退正常）。这样：

      * 继承字体的控件不再随辅助功能文本缩放变化（字号只受正常 DPI 缩放影响）；
      * 已用 ``font-size: Npx`` 显式声明的控件本就不受系统字号影响，保持不变；
      * 因此「所有文字」都不会再随 Windows 辅助功能字体放大而变大。

    注意：刻意只锁字号、不禁用 DPI 缩放——高 DPI 屏上文字仍按 devicePixelRatio
    清晰放大，不会变小变糊。
    """
    try:
        from PyQt6.QtGui import QFont
        from PyQt6.QtWidgets import QApplication
    except Exception:  # noqa: BLE001
        return
    base = QApplication.font()
    # 9pt ≈ 12px@96dpi，对应 Windows 标准 UI 字号；固定写死，彻底绕开系统字号。
    # 改用 setPixelSize 避免分数 DPR 下 point→pixel 再取整的精度损失。
    base.setPixelSize(16)
    # 全局开启 Full Hinting：Qt raster 默认 hinting 不足，在分数 DPR(1.5×)
    # 掌机上灰度 AA 笔画发糊；Full Hinting 让字形边缘尽量对齐像素网格。
    base.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    # 关键：强制抗锯齿策略。这是 LocalSend 等原生程序文字锐的本质。
    # Qt 在 HUD 半透明背景/分层窗口上默认会退化为灰度 AA；PreferAntialias 让 Qt
    # 尽量走标准抗锯齿（Windows 下仍受窗口属性与 DWM 控制，但不再主动降级）。
    # 注意：PyQt6 没有 PreferSubpixelAntialias；不设置 NoSubpixelAntialias 即可
    # 保留系统 ClearType 能力，具体是否生效取决于背景是否完全不透明。
    base.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    QApplication.setFont(base)


def _ensure_admin_for_gui() -> None:
    """GUI 模式下若当前进程非管理员，用 ShellExecuteW("runas") 自动提权后退出原进程。

    根因：之前只有 launch.bat（RunAs 重新拉起自身）与冻结 exe（requireAdministrator
    清单）会触发 UAC。若直接 `python run.py --gui`、双击 run.py、或任何绕过上述两
    入口的方式启动，run.py 自身不会提权，导致 ryzenadj / 虚拟桌面迁移等需要管理员
    的功能静默失效。这里把提权逻辑下沉到 run.py，使“无论哪种方式启动”都能自动提权。

    短路条件（不触发、无副作用）：
      * 非 Windows 平台（headless 自检需跨平台）。
      * 非 GUI 模式（--gui 不在 argv；headless 引擎自检不需要管理员）。
      * 已经是管理员（IsUserAnAdmin() 为真）——冻结 exe 因清单已是管理员，直接跳过，
        不会重复弹 UAC。
    异常分支：UAC 被用户取消或 ShellExecute 失败（返回值 <= 32）时，降级为非提权运行
    并打印警告，不阻断程序（保持“非提权也能跑基础功能”的设计）。
    """
    if not sys.platform.startswith("win"):
        return
    if "--gui" not in sys.argv:
        return
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return
    except Exception:  # noqa: BLE001
        return

    # 显式声明 Wide 签名，避免 ctypes 把 Python str 当 ANSI 传导致路径乱码。
    shell = ctypes.windll.shell32.ShellExecuteW
    shell.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
    ]
    shell.restype = ctypes.c_int  # HINSTANCE；>32 表示成功拉起

    def _quote(s: str) -> str:
        return '"' + s + '"' if (" " in s or "\t" in s) else s

    if getattr(sys, "frozen", False):
        # 冻结 exe：sys.executable 已经是本程序入口，不要再拼 run.py 脚本路径。
        # 否则会把「解包临时目录里的 run.py」当位置参数传给 exe，而 exe 不认
        # 脚本参数（它自己就是入口，frozen_launcher 直接 from run import main），
        # 提权后的新进程会因 argparse 收到多余位置参数（unrecognized arguments）
        # 直接退出，GUI 起不来——自动提权反而把程序搞没了。仅透传原 argv 其余部分。
        params = " ".join(_quote(a) for a in sys.argv[1:])
    else:
        # 开发模式（python run.py --gui）：sys.executable 是 python.exe，
        # 必须把 run.py 作为脚本参数拼上，才能 re-launch 自身。
        script = os.path.abspath(__file__)
        params = '"{}" {}'.format(script, " ".join(_quote(a) for a in sys.argv[1:]))
    rc = shell(None, "runas", sys.executable, params, os.getcwd(), 1)
    if rc > 32:
        # 已成功拉起提权副本，退出原（非管理员）进程。
        sys.exit(0)
    sys.stderr.write(
        "[warn] 未取得管理员权限（UAC 被取消或 ShellExecute 失败）。\n"
        "        ryzenadj、虚拟桌面迁移等需要管理员的功能可能不可用。\n"
    )


# -- 单实例守卫（2026-08-20）-----------------------------------------------
# 掌机 GPU 浮动「卡死在锁频上限」的隐蔽根因之一：同时运行两个凝冰结魄实例时，
# 两者互抢跨进程互斥 NingBingJue_RyzenAdj_WinRing0（WinRing0 是全局 ring0 驱动，
# 任意时刻全系统只允许一个调用方驱动）——被饿死的一方每次 GPU 浮动下发都在互斥上
# 1.5s 超时 SKIP → 回滚到天花板 → 频率永远降不下来（日志铁证：freq 恒 907 +
# applied 707↔907 振荡 + circuit 恒 False；busy 不算失败所以不熔断、也没人提示）。
# 这里用命名互斥做单实例守卫：第二个 GUI 实例启动即提示并退出，从根上杜绝双实例并存。
_SINGLE_INSTANCE_MUTEX = None  # 持有句柄防 GC（进程退出时系统自动清理）


def _ensure_single_instance() -> bool:
    """返回 False = 已有实例在运行（调用方应提示并退出）。非 Windows 恒 True（降级）。"""
    global _SINGLE_INSTANCE_MUTEX
    if not sys.platform.startswith("win"):
        return True
    # 重启子进程标记：点「↻ 重启」时，旧进程尚未退出仍持有互斥，新进程必须
    # 先起来才能接管。读到该标记即放行，并随即清除，避免污染后续（防止本进程
    # 的子进程再次被误放行，也确保该标记不会残留到下一次正常启动）。
    if os.environ.pop("NINGBING_RESTART", None) is not None:
        return True
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(
            None, False, "NingBingJue_SingleInstance")
        if not handle:
            return True  # 创建失败不阻断（降级为无守卫）
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _SINGLE_INSTANCE_MUTEX = handle  # 进程生命周期内持有
        return True
    except Exception:  # noqa: BLE001
        return True


def main() -> int:
    _ensure_admin_for_gui()  # 新增：GUI 非管理员时自动提权（runas 重新拉起自身）
    if "--gui" in sys.argv and not _ensure_single_instance():
        log("[启动] 检测到已有凝冰结魄实例在运行，本次启动退出"
            "（防双实例互抢 ryzenadj 驱动导致 GPU 浮动卡死）。")
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                "凝冰结魄已在运行。\n\n检测到重复实例：请先关闭已打开的窗口再启动。\n"
                "（两个实例同时运行会互抢 ryzenadj 驱动，导致 GPU 浮动频率卡死）",
                "凝冰结魄", 0x30)  # MB_OK | MB_ICONWARNING
        except Exception:  # noqa: BLE001
            pass
        return 1
    try:
        parser = argparse.ArgumentParser(description="凝冰结魄 launcher")
        parser.add_argument("--root", default=None, help="original script folder")
        parser.add_argument("--config", default=None, help="layout.json path (overrides auto resolution)")
        parser.add_argument("--gui", action="store_true", help="launch PyQt6 overlay")
        args = parser.parse_args()

        root = resolve_root(args.root)
        config_path = resolve_config_path(root)
        if args.config:
            config_path = args.config
        log("[启动] 解析 root=" + root + " · config=" + config_path + " · gui=" + str(args.gui))
        if args.gui:
            return run_gui(root, config_path)
        return run_headless(root, config_path)
    except Exception as exc:  # noqa: BLE001
        import traceback

        log("未预期的启动错误：\n" + traceback.format_exc())
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
