"""项目终端：在项目 files/ 目录内执行命令。

这不是系统级沙箱。P0 的边界是：
- 工作目录固定在 `data/research/projects/<id>/files`，`cwd` 不允许越界；
- 命令先过拒绝名单，再交给独立进程执行；
- 120 秒超时、输出截断、环境变量白名单；
- 每次执行写 JSONL 审计日志。

更高强度的隔离（Windows ACL / AppContainer / 系统沙箱）放在 P1。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

MAX_OUTPUT = 20_000
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600

DENY_RULES: list[tuple[str, str]] = [
    (r"(?i)\b(format|diskpart|bcdedit)\b", "磁盘/引导级命令"),
    (r"(?i)\b(shutdown|Restart-Computer|Stop-Computer)\b", "关机/重启命令"),
    (r"(?i)Remove-Item(?=[^\n]*-Recurse)(?=[^\n]*(?:C:|D:|~|\$HOME))", "递归删除系统目录"),
    (r"(?i)\brm\s+-rf\s+([/\\]+|~|\$HOME)", "递归删除系统目录"),
    (r"(?i)(Invoke-Expression|iex)\s*\(.*(http|iwr|Invoke-WebRequest)", "远程脚本执行"),
    (r"(?i)(curl|wget|Invoke-WebRequest|iwr)[^\n|]*\|\s*(sh|bash|iex|Invoke-Expression)", "管道执行远程脚本"),
    (r"(?i)Set-ExecutionPolicy", "修改执行策略"),
    (r"(?i)\bnet\s+user\b|takeown|icacls[^\n]*/grant", "修改账户/权限"),
    (r"(?i)reg\s+delete", "删除注册表"),
]

ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA",
    "LOCALAPPDATA", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "LANG", "LC_ALL",
}


def terminal_mode() -> str:
    mode = (os.environ.get("PAPEREAD_TERMINAL") or "off").strip().lower()
    return mode if mode in {"off", "ask", "auto"} else "off"


def _clean_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in ENV_KEYS
    }


def _shell(command: str) -> list[str]:
    if os.name == "nt":
        executable = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        # 强制 UTF-8，避免中文输出在重定向时变成乱码。
        prefix = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", prefix + command]
    return ["bash", "-lc", command]


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT:
        return text, False
    return text[:MAX_OUTPUT] + "\n…（输出已截断）", True


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            process.kill()
    except OSError:
        process.kill()


def _audit(root: Path, payload: dict[str, Any]) -> None:
    try:
        with (root.parent / "terminal.log").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def run_command(
    research: Any,
    project_id: str,
    command: str,
    cwd: str | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    mode = terminal_mode()
    if mode == "off":
        raise PermissionError("终端未开启：请先在设置或 PAPEREAD_TERMINAL 中启用。")
    if mode == "ask":
        # P0 没有审批 UI，ask 先按拒绝处理，避免出现“静默等待”。
        raise PermissionError("终端处于逐次确认模式，但当前版本还没有确认界面；请使用 auto 或关闭终端。")

    text = command.strip()
    if not text:
        raise ValueError("命令不能为空。")

    for pattern, reason in DENY_RULES:
        if re.search(pattern, text):
            _audit(
                Path(research.workspace(project_id)),
                {"at": time.time(), "command": text, "blocked": reason},
            )
            raise PermissionError(f"命令被拒绝：{reason}。")

    root = Path(research.workspace_root(project_id)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = (root / (cwd or ".")).resolve()
    if target != root and root not in target.parents:
        raise PermissionError("工作目录必须位于项目工作文件夹内。")
    if not target.is_dir():
        raise ValueError(f"工作目录不存在：{cwd}")

    timeout = DEFAULT_TIMEOUT if timeout_ms is None else max(1, min(int(timeout_ms) // 1000, MAX_TIMEOUT))
    started = time.monotonic()
    process = subprocess.Popen(
        _shell(text),
        cwd=str(target),
        env=_clean_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(process)
        stdout, stderr = process.communicate()

    out_text, out_cut = _truncate(stdout.decode("utf-8", errors="replace"))
    err_text, err_cut = _truncate(stderr.decode("utf-8", errors="replace"))
    duration = int((time.monotonic() - started) * 1000)
    result = {
        "command": text,
        "cwd": str(target.relative_to(root)) or ".",
        "exitCode": process.returncode,
        "stdout": out_text,
        "stderr": err_text,
        "truncated": out_cut or err_cut,
        "timedOut": timed_out,
        "durationMs": duration,
    }
    _audit(
        Path(research.workspace(project_id)),
        {
            "at": time.time(),
            "command": text,
            "cwd": result["cwd"],
            "exitCode": result["exitCode"],
            "durationMs": duration,
            "timedOut": timed_out,
        },
    )
    return result
