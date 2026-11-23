"""拉取 paper-notes 原文。

部分网络环境下 Python 的 TLS 握手会被直接重置（WinError 10054），
但系统 curl 可以正常访问。这里统一封装：httpx 优先，失败回退 curl。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

from server.winproc import run_hidden

USER_AGENT = "Paperead/notes (+https://github.com/xiaohui-plus/Paperead)"
MAX_PDF_BYTES = 100 * 1024 * 1024


def _curl(url: str, timeout: int) -> bytes:
    try:
        completed = run_hidden(
            ["curl", "-sL", "--max-time", str(timeout), "-A", USER_AGENT, url],
            capture_output=True,
            timeout=timeout + 10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"curl 不可用：{error}") from error
    if completed.returncode != 0 or not completed.stdout:
        detail = completed.stderr.decode("utf-8", "ignore").strip()[:120]
        raise RuntimeError(detail or f"curl 退出码 {completed.returncode}")
    return completed.stdout


def http_get_bytes(
    url: str,
    timeout: int = 45,
    max_bytes: int | None = None,
    prefer_curl: bool = True,
) -> bytes:
    """本机 httpx 直连 GitHub 系会被 RST，默认先走 curl，失败再回退 httpx。"""
    if prefer_curl:
        try:
            data = _curl(url, min(timeout, 12))
            if max_bytes is not None and len(data) > max_bytes:
                raise ValueError("文件超过大小上限")
            return data
        except (RuntimeError, ValueError):
            pass

    data: bytes | None = None
    try:
        with httpx.Client(
            follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT}
        ) as client:
            with client.stream("GET", url) as response:
                if response.status_code == 200:
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if max_bytes is not None and total > max_bytes:
                            raise ValueError("文件超过大小上限")
                        chunks.append(chunk)
                    data = b"".join(chunks)
    except (httpx.HTTPError, ValueError):
        data = None

    if data is None:
        data = _curl(url, timeout)
        if max_bytes is not None and len(data) > max_bytes:
            raise ValueError("文件超过大小上限")
    return data


def http_get_text(url: str, timeout: int = 12) -> str:
    return http_get_bytes(url, timeout=timeout).decode("utf-8", "replace")

def http_get_prefix(url: str, limit: int = 8192, timeout: int = 6) -> str:
    """只取文件开头若干字节：笔记的 front-matter 都在头部，不必下全文。

    GitHub 系在本机会先被 httpx 重置、白等数秒，因此与正文一致走 curl 优先。
    """
    try:
        completed = run_hidden(
            ["curl", "-sL", "--max-time", str(timeout), "-A", USER_AGENT,
             "-r", f"0-{limit - 1}", url],
            capture_output=True,
            timeout=timeout + 10,
        )
        if completed.returncode == 0 and completed.stdout:
            return completed.stdout[:limit].decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        with httpx.Client(
            follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT}
        ) as client:
            headers = {"Range": f"bytes=0-{limit - 1}"}
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code in (200, 206):
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= limit:
                            break
                    return b"".join(chunks)[:limit].decode("utf-8", "replace")
    except httpx.HTTPError:
        pass
    raise RuntimeError("摘要拉取失败")


def http_get_prefix_many(
    urls: list[str], limit: int = 8192, timeout: int = 12
) -> list[str]:
    """一次 curl 进程并发取多条文件头。

    摘要按篇各起一个 curl 时，进入一个领域就是几十个控制台进程；这里用
    ``--parallel`` 把整批压到一个进程里，输出落到独立临时文件后再按顺序读回。
    """
    if not urls:
        return []

    try:
        folder = Path(tempfile.mkdtemp(prefix="paperead-prefix-"))
    except OSError:
        return [http_get_prefix(url, limit=limit, timeout=timeout) for url in urls]

    try:
        files = [folder / f"{index}.part" for index in range(len(urls))]
        command = [
            "curl", "-sL", "--parallel", "--parallel-max", "24",
            "--max-time", str(timeout), "-A", USER_AGENT,
        ]
        for url, target in zip(urls, files):
            command += ["-r", f"0-{limit - 1}", "-o", str(target), url]
        try:
            run_hidden(command, capture_output=True, timeout=timeout + 20)
        except (OSError, subprocess.SubprocessError):
            return ["" for _ in urls]

        out: list[str] = []
        for target in files:
            try:
                out.append(target.read_bytes()[:limit].decode("utf-8", "replace"))
            except OSError:
                out.append("")
        return out
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _race(urls: list[str], task) -> bytes | str:
    """多个源并发请求，取最先成功的结果，不再串行等慢源。"""
    if not urls:
        raise RuntimeError("没有可用来源")
    pool = ThreadPoolExecutor(max_workers=len(urls))
    last: Exception | None = None
    try:
        futures = [pool.submit(task, url) for url in urls]
        for future in as_completed(futures):
            try:
                return future.result()
            except Exception as error:  # 单源失败，等下一个
                last = error
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    raise RuntimeError(str(last) if last else "所有来源都失败")


def http_get_text_any(urls: list[str], timeout: int = 8) -> str:
    return str(_race(urls, lambda url: http_get_text(url, timeout=timeout)))


def http_get_prefix_any(urls: list[str], limit: int = 8192, timeout: int = 6) -> str:
    return str(_race(urls, lambda url: http_get_prefix(url, limit=limit, timeout=timeout)))


def http_download(url: str, dest: Path, timeout: int = 600) -> int:
    """流式下载到文件；大包不能先读进内存。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(
            follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT}
        ) as client:
            with client.stream("GET", url) as response:
                if response.status_code == 200:
                    total = 0
                    with dest.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            handle.write(chunk)
                            total += len(chunk)
                    if total:
                        return total
    except httpx.HTTPError:
        pass

    completed = run_hidden(
        ["curl", "-sL", "--max-time", str(timeout), "-A", USER_AGENT,
         "-o", str(dest), url],
        capture_output=True,
        timeout=timeout + 30,
    )
    if completed.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        detail = completed.stderr.decode("utf-8", "ignore").strip()[:120]
        raise RuntimeError(detail or "下载镜像包失败")
    return dest.stat().st_size
