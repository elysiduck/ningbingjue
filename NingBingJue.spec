# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['frozen_launcher.py'],
    pathex=['.', 'src'],
    binaries=[],
    datas=[('src/ningbingjue/blocks/ryzenadj-win64', 'blocks/ryzenadj-win64'), ('src/ningbingjue/blocks/RTSS', 'ningbingjue/blocks/RTSS'), ('src/ningbingjue/blocks/powerplans', 'ningbingjue/blocks/powerplans'), ('src/ningbingjue/vendor/VirtualDesktop', 'ningbingjue/vendor/VirtualDesktop')],
    hiddenimports=['PyQt6.QtCore', 'PyQt6.QtWidgets', 'PyQt6.QtGui', 'PyQt6.QtSvg', 'PyQt6.QtSvgWidgets', 'PyQt6.sip', 'pygame', 'psutil'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NingBingJue',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
    manifest='nbj_manifest.xml',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='NingBingJue',
)
