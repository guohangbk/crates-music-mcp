"""歌单命名与台账的不变量（2026-11-08 定的口径，别再改回去）。

跑法：``python tests/test_theme_naming.py``（离线，不需要凭证）

为什么专门写这个：2026-11-07 用户连着两天拿到同一个名字「复古回潮」。根因是命名按日期轮换 +
天气命中时把候选池 insert(0) 插队，导致池长在 10 和 11 之间跳、取模结果错位；而且名字跟
内容（歌手/年代）完全无关，同一天天气一变名字还会变。这里把新口径一条条钉死。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 必须用临时数据目录：台账/去重池都在 state 里，绝不能碰真实数据
_TMP = tempfile.TemporaryDirectory(prefix="crates-naming-")
os.environ["MUSIC_DATA_DIR"] = _TMP.name

from music_mcp import compose  # noqa: E402
from music_mcp.platforms.core.base import Song  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(label)
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


class FakeRng:
    """可控随机源：按脚本依次返回 choice 结果，random() 固定。"""

    def __init__(self, picks: list[str], roll: float = 0.0) -> None:
        self.picks = list(picks)
        self.roll = roll

    def random(self) -> float:
        return self.roll

    def choice(self, options):  # noqa: ANN001 - 模拟 random.choice
        if self.picks:
            return self.picks.pop(0)
        return list(options)[0]


def main() -> None:
    print("=== 名字长度与日期前缀 ===")
    songs = [Song("泡沫", "G.E.M.邓紫棋"), Song("光年之外", "G.E.M.邓紫棋"),
             Song("喜欢你", "G.E.M.邓紫棋")]
    daily = compose.resolve_name("邓紫棋2020年以前的歌", "", "20261108", None, songs)
    check("今日歌单带 8 位日期前缀", daily.startswith("20261108-"), repr(daily))
    check("今日歌单总长 ≤20", len(daily) <= compose.NAME_LIMIT, f"{len(daily)} 字：{daily!r}")
    manual = compose.resolve_name("邓紫棋2020年以前的歌", "", "20261108", None, songs,
                                  with_date=False)
    check("普通建单不带日期", not manual.startswith("20261108"), repr(manual))
    check("普通建单 ≤20", len(manual) <= compose.NAME_LIMIT, f"{len(manual)} 字：{manual!r}")
    long_name = "深夜书房里的邓紫棋和她二十年前的自己"
    trimmed = compose.limit_name(long_name, 11)
    check("超长按段裁剪且不超预算", len(trimmed) <= 11, f"{trimmed!r}（{len(trimmed)} 字）")
    check("裁剪不会把日期切掉",
          compose.resolve_name(long_name, "", "20261108", None, songs).startswith("20261108-"))

    print("\n=== 指定优先：歌手 / 年代 / 心情 ===")
    check("prompt 里的歌手进名字（曲库写法是 G.E.M.邓紫棋）",
          "邓紫棋" in daily, repr(daily))
    check("prompt 里的年代原样进名字", "2019以前" in daily, repr(daily))
    mood = compose.resolve_name("今天心情不好", "", "20261108", None, songs, with_date=False)
    check("心情关键词优先", mood == "心情不好", repr(mood))
    named = compose.resolve_name("建个歌单", "我的私藏", "20261108", None, songs, with_date=False)
    check("显式名字直接用", named == "我的私藏", repr(named))
    generic = compose.resolve_name("随便来点", "今日歌单", "20261108", None, songs,
                                   with_date=False)
    check("通用词不当名字（走自动命名）", generic != "今日歌单", repr(generic))

    print("\n=== 素材缺失能退化 + 单次调用内一致 ===")
    from_pool = {*compose.DEFAULT_THEMES, *(word for _, _, word in compose.HOUR_THEMES)}
    bare = compose.resolve_theme("", "", "20261108", None, [], FakeRng(["随机漫步"]))
    check("没素材也能出有效主题", bool(bare[0]) and len(bare[0]) <= compose.NAME_LIMIT,
          f"{bare!r}")
    check("随机来源 = random 兜底", bare[1] in {"random", "random+换一个"}, bare[1])
    fixed_a = compose.resolve_theme("", "", "20261108", None, [],
                                    FakeRng(["随机漫步"]))[0]
    fixed_b = compose.resolve_theme("", "", "20261108", None, [],
                                    FakeRng(["随机漫步"]))[0]
    check("同样输入 → 同样结果（单次调用内一致）", fixed_a == fixed_b,
          f"{fixed_a!r} vs {fixed_b!r}")

    print("\n=== 天气是可选素材，不做硬绑定 ===")
    weather_hit = compose.resolve_theme("", "", "20261108", {"weather": "小雨"}, [],
                                        FakeRng(["随机漫步"], roll=0.1))
    check("天气命中时可以拼进名字", "雨天窗边" in weather_hit[0], repr(weather_hit))
    weather_skip = compose.resolve_theme("", "", "20261108", {"weather": "小雨"}, [],
                                         FakeRng(["随机漫步"], roll=0.9))
    check("天气命中也可以不用（不是硬映射）", "雨天窗边" not in weather_skip[0],
          repr(weather_skip))
    uncovered = set(compose.WMO_TEXT.values()) - set(compose.WEATHER_THEMES)
    check("WEATHER_THEMES 覆盖全部 WMO 词（原缺 11 个）", not uncovered, f"缺：{sorted(uncovered)}")
    uncovered_mood = set(compose.WMO_TEXT.values()) - set(compose.WEATHER_MOOD)
    check("WEATHER_MOOD 覆盖全部 WMO 词", not uncovered_mood, f"缺：{sorted(uncovered_mood)}")

    print("\n=== 台账：写入 / 上限 / 派生去重池 ===")
    compose.record_playlist(name="20261108-邓紫棋2019以前", date="20261108", theme="邓紫棋2019以前",
                            name_source="artist+year", platforms=["qq_music"],
                            songs=[{"name": "泡沫", "artist": "G.E.M.邓紫棋"}], kind="daily")
    rows = compose.recent_playlists()
    check("台账写入成功", len(rows) == 1 and rows[0]["theme"] == "邓紫棋2019以前", str(rows[:1]))
    check("台账带曲目明细", rows[0]["songs"][0]["name"] == "泡沫", str(rows[0]["songs"]))
    like_key = compose.song_key("泡沫", "G.E.M.邓紫棋")
    check("去重池从台账派生", like_key in compose.recent_keys(7),
          f"期望 {like_key!r} ∈ {sorted(compose.recent_keys(7))}")
    check("recent_songs 也派生自台账",
          any(item["name"] == "泡沫" for item in compose.recent_songs(7)))
    check("recent_tracks 工具数据源可用", compose.recent_tracks().get("ok") is True)
    for index in range(compose.PLAYLIST_HISTORY_LIMIT + 5):
        compose.record_playlist(name=f"名字{index}", date="20261108", theme=f"名字{index}")
    check(f"台账上限 {compose.PLAYLIST_HISTORY_LIMIT} 条",
          len(compose.recent_playlists()) == compose.PLAYLIST_HISTORY_LIMIT,
          f"{len(compose.recent_playlists())} 条")

    print("\n=== 软规则：随机来源不与上一张同名 ===")
    compose.record_playlist(name="20261108-X", date="20261108", theme="随机漫步")
    check("最近主题已就位", "随机漫步" in compose._recent_themes(),
          str(sorted(compose._recent_themes())))
    re_rolled = compose.resolve_theme("", "", "20261109", None, [],
                                      FakeRng(["随机漫步", "深夜书房"]))[0]
    check("撞上一张就换一个（仅随机来源）", re_rolled != "随机漫步", repr(re_rolled))
    kept = compose.resolve_theme("今天心情不好", "", "20261109", None, [],
                                 FakeRng(["随机漫步"]))[0]
    check("指定来源不避重（用户要的就是那个名字）", kept == "心情不好", repr(kept))

    print("\n=== avoid_days：默认 7，可覆盖 ===")
    check("默认 7 天", compose.RECENT_DAYS == 7, str(compose.RECENT_DAYS))
    pool = [Song("泡沫", "G.E.M.邓紫棋"), Song("晴天", "周杰伦"), Song("天后", "陈势安")]
    compose.record_playlist(name="20261109-Y", date="20261109", theme="Y",
                            songs=[{"name": "泡沫", "artist": "G.E.M.邓紫棋"}])
    check("待测歌曲确实在去重池里",
          compose.song_key("泡沫", "G.E.M.邓紫棋") in compose.recent_keys(7),
          str(sorted(compose.recent_keys(7))[:5]))
    # select_songs 的契约是"**优先**用没推过的，数量不够才复用"，并回报 reused 计数
    deduped, reused = compose.select_songs(pool, 2, None, 7)
    check("7 天窗口内够挑时把推过的挤出去",
          "泡沫" not in {song.name for song in deduped} and reused == 0,
          f"{[song.name for song in deduped]} reused={reused}")
    filled, reused_3 = compose.select_songs(pool, 3, None, 7)
    check("不够时允许复用，但排在最后并计入 reused_recent",
          [song.name for song in filled][-1] == "泡沫" and reused_3 == 1,
          f"{[song.name for song in filled]} reused={reused_3}")
    no_dedup, reused_0 = compose.select_songs(pool, 3, None, 0)
    check("avoid_days=0 时不去重（reused 恒为 0）", reused_0 == 0, f"reused={reused_0}")

    print("\n=== 只记日常歌单：手动建单不写台账 ===")
    source = (ROOT / "music_mcp" / "compose.py").read_text(encoding="utf-8")
    servers = (ROOT / "music_mcp" / "servers.py").read_text(encoding="utf-8")
    check("record_playlist 只在日常建单里调用", source.count("record_playlist(") == 2,
          f"compose 里 {source.count('record_playlist(')} 次（定义 + 日常建单）")
    check("工具层的普通建单不写台账", "record_playlist" not in servers)
    check("旧文件已停止写入",
          "record_songs" not in source and "RECENT_FILENAME)" not in source.split("def _read_raw")[1][:400])

    print(f"\n通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败项：", FAILED)
        sys.exit(1)


if __name__ == "__main__":
    main()
