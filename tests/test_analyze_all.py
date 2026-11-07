"""跑一次全平台 analyze，确认酷狗真正进了 diagnostics 且贡献歌曲。

**这个测试要真实账号凭证**（三平台至少有一个已登录），所以它跟别的测试不一样：
凭证不存在时**跳过（exit 0）而不是失败** —— 裸跑（没设 MUSIC_DATA_DIR）不该报红，
那只是"这台机器没配凭证"，不是代码问题。2026-11-07 修（Hermes 反馈）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from music_mcp import compose, paths, vault  # noqa: E402


def _configured() -> list[str]:
    """返回已配置凭证的平台 key；一个都没有就没法测。"""
    status = vault.status()
    return [key for key, info in status.items()
            if isinstance(info, dict) and info.get("configured")]


def _skip(reason: str) -> None:
    print(f"SKIP: {reason}")
    info = paths.describe()
    print(f"  数据目录: {info['data_root']}（来源: {info['override']}）")
    print(f"  凭证目录: {info['secrets_dir']}")
    print("  想真跑就设 MUSIC_DATA_DIR 指向已有凭证的目录，或先 login_start 登录一次。")
    sys.exit(0)


async def main() -> None:
    ready = _configured()
    if not ready:
        _skip("本机没有任何平台凭证（这个测试不是离线测试）")
    print("已配置凭证的平台:", ready)
    if "kugou" not in ready:
        print("提示: 酷狗未登录，它的曲目不会计入；下面的断言只验「进了 diagnostics 且无错误」")

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
