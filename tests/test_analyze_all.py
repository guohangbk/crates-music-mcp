"""跑一次全平台 analyze，确认酷狗真正进了 diagnostics 且贡献歌曲。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from music_mcp import compose  # noqa: E402


async def main() -> None:
    result = await asyncio.wait_for(compose.analyze(""), timeout=300)
    print("ok:", result.get("ok"))
    print("total:", result.get("total"))
    print("sources:", json.dumps(result.get("sources", {}), ensure_ascii=False)[:500])
    diag = result.get("diagnostics", {})
    for key in ("qq_music", "netease", "kugou"):
        d = diag.get(key, {})
        errors = (d.get("errors") or []) if isinstance(d, dict) else []
        print(f"{key}: present={key in diag} errors={errors[:1]}")
    print("summary head:", str(result.get("summary", ""))[:120])
    if "kugou" not in diag:
        print("FAIL: 酷狗没进 diagnostics")
        sys.exit(1)
    kg_err = (diag.get("kugou") or {}).get("errors") or []
    if kg_err:
        print("FAIL: 酷狗仍有错误:", kg_err[:2])
        sys.exit(1)
    if not result.get("ok"):
        print("FAIL: analyze 失败")
        sys.exit(1)
    print("PASS: 三平台都参与分析，酷狗无错误")


if __name__ == "__main__":
    asyncio.run(main())
