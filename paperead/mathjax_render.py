"""LaTeX → 矢量 PDF 片段（MathJax 完整 TeX 引擎）。

与 ``mathrender``（matplotlib mathtext，TeX 子集）并列：mathtext 不支持
``aligned`` / ``cases`` / ``bmatrix`` 等环境，数学论文里这些形态很常见。
本模块通过常驻的 node 进程调用 MathJax 的 TeX→SVG，再用 PyMuPDF 转成
单页矢量 PDF；调用方用 ``show_pdf_page`` 嵌入，产物不含位图。

node 或 MathJax 资源缺失时 ``mathjax_pdf`` 返回 ``None``，由调用方回退
mathtext，再回退原公式裁图。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pymupdf

_TIMEOUT = 6.0
_PROBE_TEX = "x"
_LOCK = threading.Lock()
_CACHE: dict[str, bytes | None] = {}
_PROC: subprocess.Popen | None = None
_QUEUE: queue.Queue | None = None
_READER: threading.Thread | None = None
_NEXT_ID = 0
_UNSET = object()
_NODE_EXE: object = _UNSET
_AVAILABLE: bool | None = None
_AVAILABLE_AT = 0.0
# node 运行时是首启后台下载的：装好前探测必然失败。失败结果只缓存一小段，
# 避免每块公式都重新起进程，同时下载完成后最多一分钟内自动转为可用。
_RETRY_SECONDS = 60.0


def _frozen_base() -> Path | None:
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def _vendor_dir() -> Path:
    base = _frozen_base()
    if base is not None:
        for rel in ("paperead/vendor/mathjax", "vendor/mathjax"):
            candidate = base / rel
            if (candidate / "tex2svg_server.js").exists():
                return candidate
    return Path(__file__).resolve().parent / "vendor" / "mathjax"


def _node_exe() -> str | None:
    global _NODE_EXE
    if _NODE_EXE is not _UNSET:
        return _NODE_EXE
    env = os.environ.get("PAPEREAD_NODE")
    if env:
        path = Path(env)
        if path.is_file():
            _NODE_EXE = str(path)
            return _NODE_EXE
    names = ("node.exe", "node") if os.name == "nt" else ("node",)
    roots = [p for p in (_frozen_base(), Path(__file__).resolve().parents[1]) if p]
    for root in roots:
        for rel in (
            Path("vendor") / "node",
            Path("paperead") / "vendor" / "node",
        ):
            for name in names:
                candidate = root / rel / name
                if candidate.is_file():
                    _NODE_EXE = str(candidate)
                    return _NODE_EXE
    _NODE_EXE = shutil.which("node")
    return _NODE_EXE


def _renderable() -> bool:
    """真实渲染一次，确认 node 与 MathJax 资源在本机可用。

    只判断文件存在不足以证明可用：node 版本不匹配时 tex2svg_server.js 能
    起来但渲染返回空，依赖方会直接硬失败而不是回退。探测失败即视为不可用。
    """
    try:
        svg = _request(_PROBE_TEX, display=False)
        return bool(svg and svg_to_pdf(svg))
    except Exception:
        return False
    finally:
        _kill()


def available() -> bool:
    global _AVAILABLE, _AVAILABLE_AT
    if _AVAILABLE:
        return True
    with _LOCK:
        if _AVAILABLE:
            return True
        now = time.time()
        if _AVAILABLE is False and now - _AVAILABLE_AT < _RETRY_SECONDS:
            return False
        ok = (
            _vendor_dir().joinpath("tex2svg_server.js").is_file()
            and _node_exe() is not None
            and _renderable()
        )
        _AVAILABLE = ok
        _AVAILABLE_AT = time.time()
        return ok


def reset_probe() -> None:
    """让下一次 ``available()`` 重新探测（node 刚装好 / 资源发生变化时调用）。"""
    global _AVAILABLE, _AVAILABLE_AT
    with _LOCK:
        _AVAILABLE = None
        _AVAILABLE_AT = 0.0


def set_node_path(path: str) -> None:
    """运行时注册一个可用的 node（例如首启后台下载完成后），立即生效。"""
    global _NODE_EXE
    if not path:
        return
    os.environ["PAPEREAD_NODE"] = path
    with _LOCK:
        _NODE_EXE = path
    reset_probe()


def _kill() -> None:
    global _PROC, _QUEUE, _READER
    proc = _PROC
    _PROC = None
    _QUEUE = None
    _READER = None
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def _start() -> None:
    global _PROC, _QUEUE, _READER
    server = _vendor_dir() / "tex2svg_server.js"
    node = _node_exe()
    if not server.is_file() or not node:
        raise RuntimeError("mathjax resource missing")
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [node, str(server)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(server.parent),
        creationflags=creationflags,
    )
    box: queue.Queue = queue.Queue()

    def pump() -> None:
        stdout = proc.stdout
        if stdout is None:
            box.put(None)
            return
        for line in stdout:
            box.put(line)
        box.put(None)

    thread = threading.Thread(target=pump, name="mathjax-reader", daemon=True)
    thread.start()
    _PROC = proc
    _QUEUE = box
    _READER = thread


def _request(tex: str, display: bool) -> str | None:
    global _NEXT_ID
    for attempt in range(2):
        try:
            if _PROC is None or _PROC.poll() is not None or _QUEUE is None:
                _kill()
                _start()
            _NEXT_ID += 1
            request_id = _NEXT_ID
            assert _PROC is not None and _PROC.stdin is not None and _QUEUE is not None
            _PROC.stdin.write(
                json.dumps({"id": request_id, "tex": tex, "display": display}) + "\n"
            )
            _PROC.stdin.flush()
            deadline = time.time() + _TIMEOUT
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("mathjax timeout")
                line = _QUEUE.get(timeout=remaining)
                if line is None:
                    raise RuntimeError("mathjax exited")
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if payload.get("id") != request_id:
                    continue
                svg = payload.get("svg")
                return str(svg) if svg else None
        except Exception:
            _kill()
            if attempt == 1:
                return None
    return None


def _normalize(latex: str) -> str:
    value = (latex or "").strip()
    if value.startswith("$$") and value.endswith("$$") and len(value) > 4:
        value = value[2:-2]
    elif value.startswith("$") and value.endswith("$") and len(value) > 2:
        value = value[1:-1]
    return value.strip()


def svg_to_pdf(svg: str) -> bytes | None:
    try:
        doc = pymupdf.open(stream=svg.encode("utf-8"), filetype="svg")
        if doc.page_count <= 0:
            return None
        rect = doc[0].rect
        if rect.width <= 1 or rect.height <= 1:
            return None
        data = doc.convert_to_pdf()
        probe = pymupdf.open(stream=data, filetype="pdf")
        ok = probe.page_count > 0
        probe.close()
        doc.close()
        return data if ok else None
    except Exception:
        return None


def _balanced(tex: str) -> bool:
    """MathJax 对残缺输入很宽容（会照画错误标记），门禁仍需拒绝不平衡括号。"""
    value = tex.replace(r"\{", "").replace(r"\}", "")
    depth = 0
    for ch in value:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def mathjax_pdf(latex: str, size: float = 9.5) -> bytes | None:
    """渲染为单页矢量 PDF；结果按公式文本缓存（字号只影响嵌入缩放）。"""
    body = _normalize(latex)
    if not body or not _balanced(body) or not available():
        return None
    with _LOCK:
        if body in _CACHE:
            return _CACHE[body]
        svg = _request(body, display=False)
        data = svg_to_pdf(svg) if svg else None
        _CACHE[body] = data
        return data