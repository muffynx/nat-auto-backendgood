# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['F:\\project-net\\nat-auto-backend-main\\nat-auto-backend-main\\agent_gui.py'],
    pathex=[],
    binaries=[],
    datas=[('F:\\project-net\\nat-auto-backend-main\\nat-auto-backend-main\\icons', 'icons')],
    hiddenimports=[],
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
    a.binaries,
    a.datas,
    [],
    name='agent_gui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['F:\\project-net\\nat-auto-backend-main\\nat-auto-backend-main\\icons\\icons.ico'],
)
