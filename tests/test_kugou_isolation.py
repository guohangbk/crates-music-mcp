"""回归：酷狗原生引擎在「同一进程第 2 次调用后随机崩溃」。

子进程隔离后，连续跑 N 次收藏拉取都应成功，且主进程不崩、不卡死。
运行前需显式设置数据目录（见 HANDOFF 第五节）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from music_mcp.platforms import get_platform  # noqa: E402
from music_mcp.platforms.base import Platform  # noqa: E402

ROUNDS = int(os.environ.get("KUGOU_ROUNDS", "6"))


async def main() -> None:
    platform = get_platform(Platform.KUGOU)
    assert platform is not None
    results = []
    for i in range(1, ROUNDS + 1):
        started = time.monotonic()
        try:
            songs, sources, diag = await asyncio.wait_for(platform.favorites(), timeout=90)
            elapsed = time.monotonic() - started
            errors = diag.get("errors") or []
            results.append((i, len(songs), bool(errors), elapsed))
            print(f"round {i}: songs={len(songs)} errors={errors[:1]} "
                  f"elapsed={elapsed:.1f}s", flush=True)
        except Exception as exc:
            results.append((i, 0, True, time.monotonic() - started))
            print(f"round {i}: EXCEPTION {exc.__class__.__name__}: {exc}", flush=True)

    ok_rounds = sum(1 for _, count, has_err, _ in results if count > 0 and not has_err)
    print(f"summary: {ok_rounds}/{ROUNDS} rounds returned songs cleanly", flush=True)
    if ok_rounds < ROUNDS:
        print("FAIL: 子进程隔离后仍有失败轮次")
        sys.exit(1)
    print("PASS: 主进程存活，连续调用无崩溃 / 无卡死")


if __name__ == "__main__":
    asyncio.run(main())
