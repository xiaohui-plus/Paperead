"""启动外部命令时统一隐藏 Windows 控制台窗口。

研究/综述会在后台调用 pandoc、LaTeX、winget、tasklist 以及项目终端等
控制台程序。父进程（sidecar）本身没有控制台时，子进程默认可能新建一个
黑色 cmd 窗口；这里统一补上 STARTF_USESHOWWINDOW + CREATE_NO_WINDOW，
避免窗口在用户眼前一闪而过。
"""

from __future__ import annotations

import os
import subprocess
from typing import Any


def hidden_kwargs() -> dict[str, Any]:
    """返回隐藏控制台窗口所需的 Popen/run 参数；非 Windows 返回空。"""

    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
    }


def run_hidden(command: Any, **kwargs: Any) -> Any:
    """``subprocess.run`` 的隐藏窗口版本。"""

    return subprocess.run(command, **hidden_kwargs(), **kwargs)


def popen_hidden(command: Any, **kwargs: Any) -> Any:
    """``subprocess.Popen`` 的隐藏窗口版本。"""

    return subprocess.Popen(command, **hidden_kwargs(), **kwargs)