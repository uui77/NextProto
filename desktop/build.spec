# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — CB08 录音笔控制台（Windows）

用法：
    (venv) pip install pyinstaller
    (venv) pyinstaller build.spec --noconfirm

产物： dist/录音笔控制台/录音笔控制台.exe
资源： dist/录音笔控制台/_internal/（web/、ffmpeg/、models/ 等）
"""
block_cipher = None

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", Path("."))).resolve()
RECORD_DIR = PROJECT_ROOT / "record"
FFMPEG_DIR = PROJECT_ROOT / "ffmpeg-8.1.2-essentials_build"

# --- datas：随包分发的资源（(源, 目标目录)） ---
datas = [
    # 前端静态资源
    (str(RECORD_DIR / "web"),                  "web"),
    # README / 协议文档
    (str(RECORD_DIR / "README.md"),             "."),
    (str(RECORD_DIR / "docs"),                  "docs"),
]

# --- 收集 ffmpeg/ffprobe 二进制 ---
if (FFMPEG_DIR / "bin" / "ffmpeg.exe").is_file():
    datas.append((str(FFMPEG_DIR / "bin" / "ffmpeg.exe"),  "ffmpeg"))
    datas.append((str(FFMPEG_DIR / "bin" / "ffprobe.exe"), "ffmpeg"))
    print(f"[spec] 已加入 ffmpeg/ffprobe：{FFMPEG_DIR}")
else:
    print(f"[spec] 警告：找不到 ffmpeg（期望：{FFMPEG_DIR}/bin/ffmpeg.exe），跳过 ffmpeg 捆绑")

# --- hiddenimports：动态导入模块 PyInstaller 扫描不到 ---
hiddenimports = [
    # FastAPI + uvicorn
    "fastapi.staticfiles",
    "fastapi.templating",
    "starlette",
    "starlette.*",
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    # 蓝牙
    "bleak",
    "bleak.backends.winrt.client",
    "bleak.backends.winrt.scanner",
    # ASR 推理引擎
    "funasr_onnx",
    "onnxruntime",
    "numpy",
    # 音频
    "pydub",
    "soundfile",
    # 其他
    "charset_normalizer",
    "tarfile",
    "zipfile",
    "ssl",
    "urllib.request",
]

# --- PyInstaller 官方 hook：批量收集框架子模块 ---
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports += collect_submodules("fastapi")
hiddenimports += collect_submodules("starlette")
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("bleak")
hiddenimports += collect_submodules("onnxruntime")
hiddenimports += collect_submodules("funasr_onnx")

a = Analysis(
    [str(RECORD_DIR / "main.py")],
    pathex=[str(RECORD_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 不打包的东西：Qt、matplotlib、pytest、IPython 等（减小体积）
    excludes=[
        "matplotlib", "pandas", "scipy", "sklearn",
        "PyQt5", "PyQt6", "PySide2", "PySide6",
        "IPython", "notebook", "pytest",
        "tkinter", "pandas.io.formats.style",
        "torch", "tensorflow", "keras",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="录音笔控制台",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="录音笔控制台",
)
