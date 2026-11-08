#!/usr/bin/env python3
"""生成 Tauri updater 的 latest.json。

用法：
    python scripts/make_latest_json.py \
        --version 0.1.1 \
        --installer release/Paperead_0.1.1_x64-setup.exe \
        --url https://dl.example.com/Paperead_0.1.1_x64-setup.exe \
        --out release/latest.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--url", required=True)
    parser.add_argument("--notes", default="")
    parser.add_argument("--out", default="latest.json", type=Path)
    args = parser.parse_args()

    signature_path = Path(str(args.installer) + ".sig")
    if not signature_path.is_file():
        raise SystemExit(f"缺少签名文件：{signature_path}")
    if not args.installer.is_file():
        raise SystemExit(f"缺少安装包：{args.installer}")

    payload = {
        "version": args.version.removeprefix("v"),
        "notes": args.notes,
        "pub_date": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "platforms": {
            "windows-x86_64": {
                "signature": signature_path.read_text(encoding="utf-8").strip(),
                "url": args.url,
            }
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
