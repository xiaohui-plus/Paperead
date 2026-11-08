# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

# SPECPATH 在不同 PyInstaller 版本里可能是 spec 所在目录，也可能是项目根；
# 用 server_entry.py 的位置做锚点，避免把 ROOT 算到上一级。
_here = Path(SPECPATH).resolve()
ROOT = _here.parent if (_here / "server_entry.py").exists() else _here

# 只带 PyMuPDF 核心 + MuPDF DLL；不带可选的 layout/ONNX 模型（约 90MB）。
pymupdf_datas = collect_data_files(
    "pymupdf", excludes=["**/layout/**", "**/mupdf-devel/**"]
)
pymupdf_bins = collect_dynamic_libs("pymupdf")
pymupdf_hidden = collect_submodules(
    "pymupdf",
    filter=lambda name: not name.startswith(("pymupdf.layout", "pymupdf4llm")),
)

a = Analysis(
    [str(ROOT / "packaging" / "server_entry.py")],
    pathex=[str(ROOT)],
    binaries=pymupdf_bins,
    datas=[(str(ROOT / "web" / "dist"), "web/dist")] + pymupdf_datas,
    hiddenimports=pymupdf_hidden
    + [
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "fastapi",
        "multipart",
        "certifi",
    ],
    excludes=[
        "tkinter",
        "matplotlib",
        "pandas",
        "pytest",
        "IPython",
        "pymupdf.layout",
        "pymupdf4llm",
        "onnxruntime",
        "numpy",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="paperead-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    version=str(ROOT / "packaging" / "version_info.txt"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="paperead-server",
)
