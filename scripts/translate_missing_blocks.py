"""补齐论文里没有译文的正文块（只补空块，覆盖前备份）。"""

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paperead.translate import translate_text

sys.stdout.reconfigure(encoding="utf-8")

PID = "p_efcf4e4aacb6"
PAPERS = Path("data/papers")
BLOCKS = PAPERS / f"{PID}.blocks.json"
BACKUP = Path("data/backups") / f"translate-missing-{time.strftime('%Y%m%d-%H%M%S')}"


async def main() -> int:
    blocks = json.loads(BLOCKS.read_text(encoding="utf-8"))
    pending = [
        b
        for b in blocks
        if b.get("kind") in ("para", "heading", "caption")
        and (b.get("text") or "").strip()
        and not (b.get("translated") or "").strip()
    ]
    print(f"pending {len(pending)}")

    BACKUP.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BLOCKS, BACKUP / f"{PID}.blocks.json")

    ok = fail = 0
    for b in pending:
        try:
            text = await translate_text(b["text"], "中文")
            if not text.strip():
                raise RuntimeError("空译文")
            b["translated"] = text
            ok += 1
            print(f"OK  {b['id']} p{b.get('page')} {b.get('kind')} {len(text)}字")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"FAIL {b['id']}: {exc}")

    BLOCKS.write_text(
        json.dumps(blocks, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"DONE ok={ok} fail={fail} backup={BACKUP}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))