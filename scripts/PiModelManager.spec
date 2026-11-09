# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件 + 无控制台窗口。

用法（在项目根目录或任意目录均可）：
    powershell -ExecutionPolicy Bypass -File scripts\\build.ps1
"""

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821
SRC = os.path.join(ROOT, "src")

# pywebview 的 JS 运行时（webview/lib/**）必须随包携带
datas = collect_data_files("webview")

hiddenimports = collect_submodules("webview") + [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr",
    "clr_loader",
    "pythonnet",
    "dsh_config",
]

a = Analysis(
    [os.path.join(SRC, "desktop_app.py")],
    pathex=[SRC],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "pytest",
        "numpy",
        "pandas",
        "matplotlib",
        "PIL",
        "IPython",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PiModelManager",
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
)
