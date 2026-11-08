#!/usr/bin/env python3
"""同步桌面版版本号。

用法：
    python scripts/set_version.py 0.1.1
    python scripts/set_version.py v0.1.1   # tag 形式也接受

会更新：pyproject.toml、paperead/__init__.py、web/package.json、
根 package.json、src-tauri/tauri.conf.json、src-tauri/Cargo.toml。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"无法在 {path} 中匹配版本号")
    path.write_text(new, encoding="utf-8")


def update_json(path: Path, version: str) -> None:
    # 只替换第一个 version 字段，避免 json.dumps 重排整个文件。
    replace_once(path, r'("version"\s*:\s*")[^"]*(")', rf'\g<1>{version}\g<2>')


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("用法：python scripts/set_version.py <version>")

    version = sys.argv[1].strip().removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
        raise SystemExit(f"不是合法 semver：{version}")

    replace_once(ROOT / "pyproject.toml", r'^version = ".*"$', f'version = "{version}"')
    replace_once(
        ROOT / "paperead" / "__init__.py",
        r'^__version__ = ".*"$',
        f'__version__ = "{version}"',
    )
    replace_once(
        ROOT / "src-tauri" / "Cargo.toml",
        r'^version = ".*"$',
        f'version = "{version}"',
    )
    update_json(ROOT / "web" / "package.json", version)
    update_json(ROOT / "package.json", version)
    update_json(ROOT / "src-tauri" / "tauri.conf.json", version)

    print(f"version -> {version}")


if __name__ == "__main__":
    main()
