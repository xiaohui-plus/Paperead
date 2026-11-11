#!/usr/bin/env python3
"""生成 Android 自动更新清单 android.json。

用法：
    python scripts/make_android_json.py \
        --version 0.2.1 \
        --apk release/Paperead_0.2.1_android-arm64.apk \
        --url https://gitee.com/deng_xiaohui/paperead/releases/download/v0.2.1/Paperead_0.2.1_android-arm64.apk \
        --fallback-url https://github.com/xiaohui-plus/Paperead/releases/download/v0.2.1/Paperead_0.2.1_android-arm64.apk \
        --notes "本次更新" \
        --out release/android.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--apk", required=True, type=Path)
    parser.add_argument("--url", required=True)
    parser.add_argument("--fallback-url", action="append", default=[])
    parser.add_argument("--notes", default="")
    parser.add_argument(
        "--notes-file",
        default="",
        help="从 UTF-8 文件读取更新说明（优先于 --notes，避免命令行转义问题）",
    )
    parser.add_argument("--out", default="android.json", type=Path)
    args = parser.parse_args()

    apk = args.apk
    if not apk.is_file():
        raise SystemExit(f"缺少 APK：{apk}")

    notes = args.notes
    if args.notes_file:
        notes = Path(args.notes_file).read_text(encoding="utf-8-sig")

    payload = {
        "version": args.version.strip().removeprefix("v"),
        "notes": notes,
        "pub_date": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "url": args.url,
        "fallback_urls": [u for u in args.fallback_url if u],
        "sha256": sha256_of(apk),
        "size": apk.stat().st_size,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out} (v{payload['version']}, {payload['size']} bytes)")


if __name__ == "__main__":
    main()
