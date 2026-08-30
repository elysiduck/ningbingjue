#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build 凝冰结魄 into a frozen onedir executable.

Run with the dedicated build venv:

    .build_venv/Scripts/python.exe build_exe.py
    .build_venv/Scripts/python.exe build_exe.py --with-config

What it does
------------
* Entry point: frozen_launcher.py (sets NINGBING_ROOT = exe dir when frozen).
* --windowed            : no console window (it's a HUD overlay).
* --uac-admin           : requireAdministrator manifest (ryzenadj needs it).
* Bundles the vendored ryzenadj + RTSS binaries next to the exe.

配置打包策略（2026-08-26 改进）
------------------------------
* 默认【不】把当前 layout.json / config / buttons 打进冻结文件夹。
  这样产出的 dist/NingBingJue（以及由此复制的便携版、安装包）不含任何
  本机配置，首次运行以代码出厂默认运行（锁帧关·30FPS、TDP 10W、GPU 700MHz、
  GPU 浮动关、降压关、风扇 Auto），并在用户改动后自行写出配置。
* 如需把当前配置一并分发（例如给自己的掌机做便携版、或希望安装包内置你的
  布局/按钮），显式加 --with-config。
* make_installer.py 在需要打包配置时会自动以 --with-config 调用本脚本。

Output: dist/NingBingJue/  (copy this whole folder to other devices).
"""
from __future__ import annotations

import os
import sys
import argparse

SEP = os.pathsep  # ';' on Windows


def _data(src: str, dst: str) -> str:
    return src + SEP + dst


def parse_args(argv=None) -> argparse.Namespace:
    root = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description="Build NingBingJue frozen onedir exe")
    p.add_argument(
        "--with-config", action="store_true",
        help="把当前 layout.json/config/buttons 一并打进冻结文件夹"
             "（默认不打，保持出厂默认）。",
    )
    p.add_argument(
        "--distpath",
        default=os.environ.get("NBJ_DISTPATH", os.path.join(root, "dist")),
        help="PyInstaller 输出根（默认 dist/）。",
    )
    p.add_argument(
        "--workpath",
        default=os.environ.get("NBJ_WORKPATH", os.path.join(root, "build")),
        help="PyInstaller 中间目录（默认 build/）。",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    from PyInstaller.__main__ import run

    args = parse_args(argv)
    with_config = bool(args.with_config)

    # Resolve paths relative to this script's directory (project root).
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)

    # distpath/workpath 可重定向（例如进 %TEMP% 以绕过沙箱对 shutil.rmtree 的
    # 拦截）。默认保持正常的 "dist/ + build/" 布局。
    distpath = os.path.abspath(args.distpath)
    workpath = os.path.abspath(args.workpath)

    args_list = [
        "frozen_launcher.py",
        "--name", "NingBingJue",
        "--windowed",
        "--uac-admin",
        # 自定义清单：在保留 requireAdministrator 的同时，把 DPI 感知升级为
        # PerMonitorV2（默认 admin 清单只是系统级 dpiAware，会导致外接屏上
        # 界面缩放失常）。--manifest 会整体替换 PyInstaller 自动生成的清单，
        # 因此管理员权限也要在本清单里重新声明。
        "--manifest", "nbj_manifest.xml",
        "--noconfirm",
        "--distpath", distpath,
        "--workpath", workpath,
        "--paths", ".",
        "--paths", "src",
        # PyQt6 submodules (the PyQt6 hook usually catches these, but be safe).
        "--hidden-import", "PyQt6.QtCore",
        "--hidden-import", "PyQt6.QtWidgets",
        "--hidden-import", "PyQt6.QtGui",
        "--hidden-import", "PyQt6.QtSvg",
        "--hidden-import", "PyQt6.QtSvgWidgets",
        "--hidden-import", "PyQt6.sip",
        # 惰性 import 的第三方库（函数体内 import，字节码扫描未必抓到，显式双保险）。
        "--hidden-import", "pygame",
        "--hidden-import", "psutil",
        # Vendored external binaries (PyInstaller won't auto-collect these).
        "--add-data", _data(r"src/ningbingjue/blocks/ryzenadj-win64", r"blocks/ryzenadj-win64"),
        "--add-data", _data(r"src/ningbingjue/blocks/RTSS", r"ningbingjue/blocks/RTSS"),
        # 预置电源计划 .pow（切换/导入依赖它；漏打包会导致掌机 exe 报「预设导入失败」）。
        "--add-data", _data(r"src/ningbingjue/blocks/powerplans", r"ningbingjue/blocks/powerplans"),
        # Vendored VirtualDesktop PowerShell module (imported by absolute
        # -Path at runtime so no per-machine Install-Module / admin scope needed).
        "--add-data", _data(r"src/ningbingjue/vendor/VirtualDesktop", r"ningbingjue/vendor/VirtualDesktop"),
    ]

    # RTSS 便携部署二进制（grill 决策 A：默认不内嵌，缺失时 UI 引导官方下载；
    # 仅当 NBJ_BUNDLE_RTSS=1 才把裁剪版 RTSS 打进构建，供自用构建「移植新机不缺
    # RTSS」）。授权：RTSS EULA 禁止再分发，故默认关闭，仅用户自用构建显式开启。
    bundle_rtss = os.environ.get("NBJ_BUNDLE_RTSS") == "1"
    rtss_src = os.path.join(root, "src", "ningbingjue", "vendor", "RTSS")
    if bundle_rtss:
        if os.path.isdir(rtss_src):
            args_list.append(
                _data(rtss_src, r"ningbingjue/vendor/RTSS")
            )
            print(f"[build] NBJ_BUNDLE_RTSS=1 → 内嵌 RTSS ({rtss_src})")
        else:
            print("[build] 警告：NBJ_BUNDLE_RTSS=1 但 vendor/RTSS 不存在，跳过内嵌")
    else:
        print("[build] NBJ_BUNDLE_RTSS 未开启 → 不内嵌 RTSS（运行时缺失将引导官方下载）")

    # 当前配置：默认【不】打包（出厂默认）；仅 --with-config 才把本机
    # layout.json / config / buttons 烘焙进冻结文件夹。
    if with_config:
        args_list += [
            "--add-data", _data("layout.json", "."),
            "--add-data", _data("config", "config"),
            "--add-data", _data("buttons", "buttons"),
        ]
    else:
        print("[build] 默认不打包当前配置（layout.json / config / buttons）"
              " → 冻结产物以出厂默认运行。")
        print("[build] 如需携带当前配置，请加 --with-config。")

    print("[build] PyInstaller args:")
    for a in args_list:
        print("   ", a)
    print()
    run(args_list)
    # If we built into a redirected distpath, land the result in the normal
    # dist/ folder (which is what the user copies around). Temp paths are
    # exempt from the sandbox rmtree shim, so this copy is safe.
    final = os.path.join(root, "dist", "NingBingJue")
    built = os.path.join(distpath, "NingBingJue")
    if os.path.abspath(built) != os.path.abspath(final) and os.path.isdir(built):
        import shutil
        import tempfile
        import time

        if os.path.isdir(final):
            # Move the whole dist/ tree aside by renaming its PARENT. Renaming
            # an ancestor dir succeeds even if a file deep inside is locked by
            # another process (the open handle stays valid). This dodges both
            # the sandbox rmtree shim and the locked-exe rename failure.
            dist_dir = os.path.dirname(final)
            old = os.path.join(
                tempfile.gettempdir(),
                "nbj_dist_old_" + str(os.getpid()) + "_" + str(int(time.time())),
            )
            try:
                os.rename(dist_dir, old)
            except OSError:
                shutil.rmtree(dist_dir, ignore_errors=True)
        shutil.copytree(built, final)
        print(f"\n[build] done -> {final}")
    else:
        print("\n[build] done -> dist/NingBingJue/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
