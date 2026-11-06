"""验证画像权重参与选曲：rank 字段、_profile_ranked 打分、select_songs 排序。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from music_mcp import compose, paths  # noqa: E402
from music_mcp.platforms.base import Song  # noqa: E402

profile = {
    "top_artists": ["G.E.M.邓紫棋", "梁博", "赵雷"],
    "mood_tendencies": ["伤感", "怀旧"],
}
songs = [
    Song("普通歌A", "某歌手"),                       # rank 0
    Song("梁博的某首歌", "梁博"),                      # rank 2
    Song("赵雷的歌曲", "赵雷"),                        # rank 2
    Song("邓紫棋的歌", "G.E.M.邓紫棋"),               # rank 2
    Song("一首怀旧的歌", "某某"),                      # rank 1
    Song("普通歌B", "另一歌手"),                       # rank 0
]
ranked = compose._profile_ranked(songs, profile)
ranks = [(s.name, s.rank) for s in ranked]
print("ranked:", ranks)
assert ranked[1].rank == 2 and ranked[2].rank == 2 and ranked[3].rank == 2
assert ranked[4].rank == 1
assert ranked[0].rank == 0 and ranked[5].rank == 0

# select_songs 应按 rank 降序（forced 之外）
selected, reused = compose.select_songs(ranked, count=5)
print("selected:", [(s.name, s.rank) for s in selected])
assert selected[0].rank == 2 or selected[0].rank == 0  # 至少没有把 rank 低的排前面
assert all(selected[i].rank >= selected[i + 1].rank for i in range(len(selected) - 1)) or \
       all(s.rank == 0 for s in selected)

# 无画像时行为不变
unranked = compose._profile_ranked(songs, {})
assert all(s.rank == 0 for s in unranked)
print("PASS: 画像权重生效，无画像时 rank 全 0")
