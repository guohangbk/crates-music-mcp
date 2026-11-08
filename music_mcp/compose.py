"""组合能力：全平台候选池、多平台建同一份歌单，外加命名 / 天气 / 历史 / 收藏快照。

这一层**不认任何平台细节**，只跟 platforms 层暴露的 Platform / Song / MusicPlatform 打交道。

口径（用户定的）：
  * 候选池 = 所有已登录平台的「今日推荐」+「全部收藏」，去重合并；推荐排在前面，
    所以选歌时天然偏向「今天平台推给我的」，收藏负责把池子撑大；
  * 建单 = 同一份曲目清单（同名单），在每一个已登录的平台各建一份；每首歌在目标平台
    用 search 现查，**找不到就跳过**（记进该平台的 failed），不报错中断；
  * 最近 14 天推过的歌，优先让位给没推过的；
  * 歌单名 = <日期>-<主题>，主题优先用用户话里的心情/场景，其次按日期轮换 + 参考当天天气。

下面的函数是历史实现**原样搬过来**的，只把跨模块调用改成同文件调用。
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from . import paths, vault
from .platforms import DEFAULT_ORDER, get_platform, resolve_keys
from .platforms.core.base import MusicPlatform, Song, key_of, normalize, song_key


# 心情 / 场景关键词 -> 主题名。顺序有意义：先匹配到的先用（负面情绪放前面，
# 免得「心情不好但想听点开心的」这种被后一条抢走）。
MOOD_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("心情不好", "不开心", "难过", "难受", "emo", "丧", "郁闷", "低落", "失恋", "烦"), "心情不好"),
    (("失眠", "睡不着", "助眠", "入睡", "熬夜"), "深夜助眠"),
    (("加班", "写代码", "搬砖", "专注", "工作", "学习", "赶稿"), "专注工作流"),
    (("通勤", "地铁", "公交", "开车", "路上", "堵车"), "通勤路上"),
    (("运动", "健身", "跑步", "锻炼", "撸铁"), "运动燃烧"),
    (("旅行", "出游", "自驾", "在路上"), "在路上"),
    (("周末", "假期", "放假", "休息"), "周末漫游"),
    (("开心", "高兴", "兴奋", "好心情", "嗨", "爽"), "好心情"),
    (("放松", "松弛", "疗愈", "安静", "平静", "放空"), "松弛时刻"),
)

# 名字上限与日期前缀（口径见下面 naming 那段注释）
NAME_LIMIT = 20          # 名字最多 20 个字（今日歌单含日期前缀）
DATE_PREFIX_LEN = 9      # "20261108-" 正好 9 个字符
PLAYLIST_HISTORY_LIMIT = 100     # 台账只留最近 100 张歌单
GENERIC_NAMES = {"歌单", "今日歌单", "每日歌单", "推荐歌单", "AI推荐", "ai推荐"}

# 没给任何线索时的主题池：**随机抽**（不再是"按日期轮换"，
# 那样会连着两天撞名，而且名字跟内容毫无关系）
DEFAULT_THEMES: tuple[str, ...] = (
    "通勤电台", "深夜松弛", "午后漫游", "City Pop 时刻", "老歌翻新",
    "随机漫步", "独处时光", "多巴胺充电", "复古回潮", "耳机里的远方",
    "窗边一隅", "慢速心跳", "深夜书房", "公路电影", "晚风电台",
    "低电量模式", "想你的频率", "安静放电", "午夜便利店", "温柔着陆",
    "旧唱片架", "清醒梦", "把音量调小", "阳光备用电源", "城市漫游指南",
    "睡前十分钟", "周一续命", "下班路上", "雨天备用", "一个人的房间",
)

# 时段 -> 共鸣词（"此刻"也是共鸣素材之一）
HOUR_THEMES: tuple[tuple[int, int, str], ...] = (
    (5, 9, "清晨电台"), (9, 12, "上午循环"), (12, 17, "午后漫游"),
    (17, 20, "傍晚散场"), (20, 24, "夜里小声播放"), (0, 5, "凌晨三点的歌"),
)

# 天气 -> 共鸣词。**覆盖 WMO_TEXT 里全部 25 个词**：
# 此前只有 14 个键，于是「毛毛雨 / 冻雨 / 雪粒 / 阵雪 / 强雷暴…」既没有 mood_hint
# 也没有命名素材，直接漏掉（2026-11-08 实测：毛毛雨那天 mood_hint 是空的）。
WEATHER_THEMES: dict[str, str] = {
    "晴": "晴天散步", "晴间多云": "多云也有光", "多云": "云层之下",
    "阴": "阴天慢摇", "有雾": "雾里听歌", "雾凇": "雾凇与静电",
    "毛毛雨": "毛毛细雨", "小雨": "雨天窗边", "中雨": "雨天窗边",
    "大雨": "雨大屋里", "冻毛毛雨": "冻雨天不出门", "冻雨": "冻雨天不出门",
    "强冻雨": "冻雨天不出门", "暴雨": "暴雨耳机", "阵雨": "阵雨轻爵士",
    "强阵雨": "阵雨轻爵士", "小雪": "初雪温柔", "中雪": "初雪温柔",
    "大雪": "雪夜安静", "雪粒": "雪粒打在窗上", "阵雪": "阵雪纷飞",
    "强阵雪": "阵雪纷飞", "雷阵雨": "雷雨张力", "雷阵雨伴冰雹": "雷雨张力",
    "强雷暴伴冰雹": "强雷暴夜里",
}


# ---------------------------------------------------------------------------
# 歌单命名
#
# 口径（2026-11-08 与用户定，别再改回"按日期轮换"）：
#   * **共鸣优先、指定优先**：用户点名的名字直接用；说了心情/歌手/年代就贴着说；
#     什么都没说就**随机抽素材**（天气 / 时段 / 主题池）—— 名字是很随意的东西，
#     只要让人有共鸣就行；**不要**出现"雨天就一定叫雨天窗边"这种硬映射。
#   * 今日歌单带 8 位日期前缀（`20261108-`），普通建单**不带**日期。
#   * 名字 ≤20 字（今日歌单含前缀，所以主题只剩 11 字）；超长先丢修饰、保人名与指定词。
#   * **不做同一天强幂等**：重跑可以出不同名字；只保证"单次调用内一致"。
#   * 起名权优先给调用方（agent）：`suggest_playlist_names` 是唯一对外门面，
#     这里只是它和 create_* 兜底共用的同一份引擎。
# ---------------------------------------------------------------------------

_SCENE_SEPARATOR = " · "


def _rng(rng: Any = None) -> Any:
    """随机源。传进来就用传进来的（测试可以固定种子），否则用模块级 random。"""
    return rng if rng is not None else random


def limit_name(name: str, budget: int) -> str:
    """按字数裁剪：先丢修饰段（`·` 分隔的最后几段），实在放不下才硬截。"""
    text = str(name or "").strip()
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    parts = [part.strip() for part in re.split(r"[·×|/、,，]+", text) if part.strip()]
    kept = ""
    for part in parts:
        candidate = f"{kept}{_SCENE_SEPARATOR}{part}" if kept else part
        if len(candidate) > budget:
            break
        kept = candidate
    return kept or text[:budget]


def theme_from_prompt(prompt: str) -> str:
    """用户话里的心情/场景。没提到就返回空串。"""
    text = str(prompt or "").lower()
    for keywords, theme in MOOD_RULES:
        if any(keyword.lower() in text for keyword in keywords):
            return theme
    return ""


_CJK_CHUNK = re.compile(r"[\u4e00-\u9fff]{2,}")


def _artist_aliases(name: str) -> list[str]:
    """歌手名的候选写法：全名 + 其中的中文片段。

    实测场景：曲库里是「G.E.M.邓紫棋」，而用户只会说「邓紫棋」——
    直接拿全名去匹配永远不中，所以要把中文片段也当别名。
    """
    text = str(name or "").strip()
    aliases = [text] if text else []
    for chunk in _CJK_CHUNK.findall(text):
        if chunk != text:
            aliases.append(chunk)
    return aliases


def extract_artist_hint(prompt: str, artists: Any) -> str:
    """用现成的歌手名单去用户话里找（不做中文分词）：命中里取最长的那个。"""
    text = str(prompt or "")
    if not text:
        return ""
    hits: list[str] = []
    for artist in artists:
        for alias in _artist_aliases(artist):
            if len(alias) >= 2 and alias in text:
                hits.append(alias)
    return max(hits, key=len) if hits else ""


_YEAR_RULES: tuple[tuple[re.Pattern[str], Any], ...] = (
    # "2020年以前 / 之前 / 前" → 2019以前（减一年更好懂）
    (re.compile(r"(20\d{2})\s*年?\s*(?:以前|之前|前)"),
     lambda m: f"{int(m.group(1)) - 1}以前"),
    (re.compile(r"(20\d{2})\s*年?\s*(?:及以前)"), lambda m: f"{m.group(1)}以前"),
    (re.compile(r"(20\d{2})\s*年?\s*(?:之后|以后|起)"), lambda m: f"{m.group(1)}以后"),
    (re.compile(r"(\d{2})\s*年代"), lambda m: f"{m.group(1)}年代"),
    (re.compile(r"最近\s*(\d{1,2})\s*年"), lambda m: f"最近{m.group(1)}年"),
    (re.compile(r"(20\d{2})\s*年"), lambda m: f"{m.group(1)}年"),
)


def extract_year_hint(prompt: str) -> str:
    """用户话里的年代线索，**原样搬进名字**（不做真实年份计算，也就不用查曲目年份）。"""
    text = str(prompt or "")
    for pattern, render in _YEAR_RULES:
        match = pattern.search(text)
        if match:
            return render(match)
    return ""


def _hour_word(when: float | None = None) -> str:
    hour = time.localtime(when or time.time()).tm_hour
    for start, end, word in HOUR_THEMES:
        if start <= hour < end:
            return word
    return ""


def _top_artist(artists: list[str], threshold: float = 0.5) -> str:
    if not artists:
        return ""
    name, count = Counter(artists).most_common(1)[0]
    return name if count / len(artists) >= threshold else ""


def _too_similar(left: str, right: str, need: int = 2) -> bool:
    """两个词有没有明显的共同部分（"雨天窗边" vs "雨天备用" → 是）。"""
    if not left or not right:
        return False
    shared = sum(1 for char in set(left) if char in right)
    return shared >= need


def _random_theme(weather: dict[str, Any] | None, artists: list[str], rng: Any) -> str:
    """没指定时的"共鸣"名字：随机抽素材拼，不做任何一一映射。"""
    source = _rng(rng)
    parts: list[str] = []
    weather_word = WEATHER_THEMES.get(str((weather or {}).get("weather") or ""))
    if weather_word and source.random() < 0.5:            # 天气可选，命中也不一定用
        parts.append(weather_word)
    options = list(DEFAULT_THEMES) + [_hour_word()]
    scene = source.choice(options)
    if parts and _too_similar(parts[0], scene):           # 别拼出"雨天窗边 · 雨天备用"
        scene = source.choice([item for item in options if not _too_similar(parts[0], item)] or options)
    parts.append(scene)
    top = _top_artist(artists)                            # 曲库被某歌手占大半时偶尔带上
    if top and source.random() < 0.34:
        parts.append(top)
    return _SCENE_SEPARATOR.join(part for part in parts if part)


def build_theme(prompt: str, date: str, weather: dict[str, Any] | None = None,
                songs: list[Song] | None = None, rng: Any = None) -> tuple[str, str]:
    """没点名整名时定主题，返回 ``(主题, 来源)``。

    来源：``mood``（心情词）/ ``artist`` / ``year`` / ``artist+year`` / ``random``。
    顺序就是优先级：用户说过的 > 没说过时随机。
    """
    mood = theme_from_prompt(prompt)
    artists = [song.artist for song in songs or () if song.artist]
    artist = extract_artist_hint(prompt, artists)
    year = extract_year_hint(prompt)
    # 内容（歌手/年代）优先于心情：用户说"写代码时听邓紫棋2019以前的歌"时，
    # "邓紫棋2019以前"比"专注工作流"更能说明这张单是什么。两个都有就拼接，
    # 名字超长时 limit_name 会从后往前丢（先丢心情这项修饰）。
    tail = f"{_SCENE_SEPARATOR}{mood}" if mood else ""
    if artist and year:
        return f"{artist}{year}{tail}", "artist+year" if not mood else "artist+year+mood"
    if artist:
        scene = _rng(rng).choice(list(DEFAULT_THEMES))
        return f"{artist}{_SCENE_SEPARATOR}{scene}{tail}", "artist" if not mood else "artist+mood"
    if year:
        return f"{year}的老歌{tail}", "year" if not mood else "year+mood"
    if mood:
        return mood, "mood"
    return _random_theme(weather, artists, rng), "random"


AVOID_RECENT_THEMES = 5      # 随机名字避开最近这几张（不是硬保证，撞不上就换一个）


def _recent_themes(limit: int = AVOID_RECENT_THEMES) -> set[str]:
    return {str(row.get("theme") or "") for row in recent_playlists(limit) if row.get("theme")}


def _recent_openers(limit: int = AVOID_RECENT_THEMES) -> set[str]:
    """最近几张名字的**开头词**。

    只比"整串是否相同"不够：「晴天散步」和「晴天散步 · 晚风电台」是两条不同的主题，
    但用户看到的是"连着好几天都从晴天散步开头"。所以开头词也要避。
    """
    out: set[str] = set()
    for theme in _recent_themes(limit):
        out.add(theme.split(_SCENE_SEPARATOR)[0].strip())
    return out


def resolve_theme(prompt: str, explicit: str, date: str,
                  weather: dict[str, Any] | None = None,
                  songs: list[Song] | None = None, rng: Any = None) -> tuple[str, str]:
    """定主题（不含日期前缀），返回 ``(主题, 来源)``。"""
    explicit = str(explicit or "").strip()
    text = str(prompt or "")
    if explicit and explicit in text:
        return limit_name(explicit, NAME_LIMIT), "explicit"     # 用户话里点名整名
    if explicit and explicit not in GENERIC_NAMES:
        return limit_name(explicit, NAME_LIMIT), "explicit"
    theme, source = build_theme(prompt, date, weather, songs, rng)
    if source == "random":
        # 软规则：**随机**来源避开最近几张用过的（撞了就再抽，抽 3 次还撞就用）。
        # 点名的来源不避重 —— 用户要的就是那个名字，重复才是对的。
        taken = _recent_themes()
        openers = _recent_openers()
        for _ in range(4):
            opener = theme.split(_SCENE_SEPARATOR)[0].strip()
            if not taken or (theme not in taken and opener not in openers):
                break
            theme, source = build_theme(prompt, date, weather, songs, rng)
    return limit_name(theme, NAME_LIMIT), source


def resolve_name(prompt: str, explicit: str, date: str,
                 weather: dict[str, Any] | None = None,
                 songs: list[Song] | None = None,
                 with_date: bool = True, rng: Any = None) -> str:
    """歌单名。今日歌单 ``with_date=True``（``20261108-主题``）；普通建单 False（只有主题）。"""
    explicit = str(explicit or "").strip()
    if explicit and explicit in str(prompt or ""):
        return limit_name(explicit, NAME_LIMIT)         # 点名整名：不加日期也不裁
    theme, _ = resolve_theme(prompt, explicit, date, weather, songs, rng)
    if not with_date:
        return limit_name(theme, NAME_LIMIT)
    # 先给主题留预算再拼日期，免得截断时把日期切掉
    return f"{date}-{limit_name(theme, NAME_LIMIT - DATE_PREFIX_LEN)}"


def naming_material(prompt: str = "", date: str = "",
                    weather: dict[str, Any] | None = None,
                    songs: list[Song] | None = None) -> dict[str, Any]:
    """给调用方（agent）起名用的素材包：今天是什么日子、手里有哪些词。"""
    artists = [song.artist for song in songs or () if song.artist]
    counters = Counter(artists)
    return {
        "date": str(date or time.strftime("%Y%m%d")),
        "hour_word": _hour_word(),
        "weather": str((weather or {}).get("weather") or ""),
        "weather_word": WEATHER_THEMES.get(str((weather or {}).get("weather") or ""), ""),
        "weather_mood": WEATHER_MOOD.get(str((weather or {}).get("weather") or ""), ""),
        "mood_from_prompt": theme_from_prompt(prompt),
        "artist_from_prompt": extract_artist_hint(prompt, artists),
        "year_from_prompt": extract_year_hint(prompt),
        "top_artists": [name for name, _ in counters.most_common(5)],
        "recent_names": [str(row.get("name") or "") for row in recent_playlists(5)],
        "name_budget": {"daily": NAME_LIMIT - DATE_PREFIX_LEN, "manual": NAME_LIMIT},
    }


def naming_candidates(prompt: str = "", date: str = "", count: int = 3,
                      weather: dict[str, Any] | None = None,
                      songs: list[Song] | None = None) -> list[dict[str, str]]:
    """给几个候选名字（含来源），供 agent 挑选或参考。

    指定了内容（歌手/年代）时也补几个"换个场景/只留随机素材"的变体 ——
    agent 需要的是**能挑**，而不是只有一个答案。
    """
    want = max(1, int(count or 3))
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    first_theme, first_source = build_theme(prompt, date, weather, songs)
    first_theme = limit_name(first_theme, NAME_LIMIT)
    if first_theme:
        seen.add(first_theme)
        out.append({"theme": first_theme, "source": first_source})
    artists = [song.artist for song in songs or () if song.artist]
    artist = extract_artist_hint(prompt, artists)
    mood = theme_from_prompt(prompt)
    variants: list[tuple[str, str]] = []
    if artist:                                    # 同一个歌手的其他场景
        for scene in list(DEFAULT_THEMES)[:6]:
            variants.append((f"{artist}{_SCENE_SEPARATOR}{scene}", "artist+换场景"))
    if mood:                                      # 心情本身也是一条路
        variants.append((mood, "mood"))
    variants.append((_random_theme(weather, artists, None), "random"))
    for theme, source in variants:
        if len(out) >= want:
            break
        theme = limit_name(theme, NAME_LIMIT)
        if theme and theme not in seen:
            seen.add(theme)
            out.append({"theme": theme, "source": source})
    for _ in range(want * 3):                     # 还不够就再随机抽几个
        if len(out) >= want:
            break
        theme, source = build_theme(prompt, date, weather, songs)
        theme = limit_name(theme, NAME_LIMIT)
        if theme and theme not in seen:
            seen.add(theme)
            out.append({"theme": theme, "source": source})
    return out


CACHE_TTL = 3600
TIMEOUT = 8.0

# WMO weather code -> 中文
WMO_TEXT = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "有雾", 48: "雾凇", 51: "毛毛雨", 53: "小雨", 55: "中雨",
    56: "冻毛毛雨", 57: "冻雨", 61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨", 71: "小雪", 73: "中雪", 75: "大雪",
    77: "雪粒", 80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪",
    86: "强阵雪", 95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴伴冰雹",
}

# 天气 -> 适合的歌单氛围（给命名用）
WEATHER_MOOD = {
    "晴": "晴天适合轻快明亮", "晴间多云": "阳光忽隐忽现，来点 City Pop",
    "多云": "多云天适合慢摇", "阴": "阴天适合安静一点",
    "有雾": "有雾的天气适合氛围感", "雾凇": "雾凇天适合冷冽清透的声音",
    "毛毛雨": "毛毛细雨，来点轻一点的钢琴", "小雨": "雨天适合窗边慢歌",
    "中雨": "雨天适合窗边慢歌", "大雨": "雨大，适合窝着听歌",
    "冻毛毛雨": "冻雨天适合待在屋里的暖歌", "冻雨": "冻雨天适合待在屋里的暖歌",
    "强冻雨": "冻雨天适合待在屋里的暖歌", "暴雨": "暴雨天，适合沉一点的声音",
    "阵雨": "阵雨天适合轻爵士", "强阵雨": "阵雨天适合轻爵士",
    "小雪": "下雪了，适合温柔的歌", "中雪": "下雪了，适合温柔的歌",
    "大雪": "大雪天适合安静的歌", "雪粒": "雪粒天适合干净的器乐",
    "阵雪": "阵雪天适合轻飘的旋律", "强阵雪": "阵雪天适合轻飘的旋律",
    "雷阵雨": "雷雨天适合有张力一点的歌", "雷阵雨伴冰雹": "雷雨天适合有张力一点的歌",
    "强雷暴伴冰雹": "强雷暴适合低频重一点的歌",
}


def _cache_file():
    return paths.state_file("weather_cache.json")


# 服务器上按 IP 定位必然落在机房所在地，所以只要没用 MUSIC_WEATHER_CITY 就提醒一次。
WEATHER_CITY_HINT = (
    "天气是按出口 IP 定位的（当前判定为「{city}」）；在服务器上这通常是机房所在地，"
    "不是你真正所在的城市。想让天气贴合你的城市，请设置环境变量 "
    "MUSIC_WEATHER_CITY=城市名（例如 MUSIC_WEATHER_CITY=杭州），"
    "或者传 with_weather=false 直接关掉天气。"
)


def _read_cache() -> dict[str, Any]:
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_cache(payload: dict[str, Any]) -> None:
    try:
        _cache_file().parent.mkdir(parents=True, exist_ok=True)
        _cache_file().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


async def _locate(client: httpx.AsyncClient, city: str) -> tuple[str, float, float] | None:
    if city:
        response = await client.get("https://geocoding-api.open-meteo.com/v1/search",
                                    params={"name": city, "count": 1, "language": "zh"})
        results = (response.json() or {}).get("results") or []
        if results:
            top = results[0]
            return (str(top.get("name") or city), float(top["latitude"]), float(top["longitude"]))
        return None
    response = await client.get("http://ip-api.com/json/",
                                params={"fields": "status,city,lat,lon"})
    payload = response.json() or {}
    if payload.get("status") == "success":
        return (str(payload.get("city") or "本地"), float(payload["lat"]), float(payload["lon"]))
    return None


async def current(city: str = "") -> dict[str, Any]:
    """今天的天气实况。失败就 ``{"ok": False, "error": ...}``，调用方当作没有天气处理。

    city 留空时按出口 IP 定位（ip-api.com）。伺服在服务器上时出口 IP 落在机房所在地，
    所以这里会带上 ``location_source``（city / ip）与 ``hint``，让 agent 能提醒用户设
    ``MUSIC_WEATHER_CITY`` —— 见 WEATHER_CITY_HINT。
    """
    city = (city or os.environ.get("MUSIC_WEATHER_CITY") or "").strip()
    by_city = bool(city)          # 用户明确给了城市 = 不会用到 IP 定位，也就不必提醒
    cache = _read_cache()
    cache_key = city or "auto"
    if cache.get("key") == cache_key and time.time() - float(cache.get("ts") or 0) < CACHE_TTL:
        return cache.get("value") or {"ok": False, "error": "no cache"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            located = await _locate(client, city)
            if not located:
                value = {"ok": False, "error": "定位失败（可设 MUSIC_WEATHER_CITY）"}
            else:
                name, latitude, longitude = located
                response = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={"latitude": latitude, "longitude": longitude,
                            "current": "temperature_2m,relative_humidity_2m,weather_code",
                            "timezone": "auto"})
                observed = (response.json() or {}).get("current") or {}
                code = int(observed.get("weather_code") or 0)
                text = WMO_TEXT.get(code, "未知")
                value = {"ok": True, "city": name, "weather": text, "weather_code": code,
                         "temperature": observed.get("temperature_2m"),
                         "humidity": observed.get("relative_humidity_2m"),
                         "mood_hint": WEATHER_MOOD.get(text, ""),
                         "location_source": "city" if by_city else "ip"}
                if not by_city:
                    value["hint"] = WEATHER_CITY_HINT.format(city=name)
    except Exception as exc:  # noqa: BLE001 - 天气失败不影响主流程
        value = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
    _write_cache({"key": cache_key, "ts": time.time(), "value": value})
    return value


RECENT_DAYS = 7          # 选曲去重窗口：默认 7 天，agent 可用 avoid_days 覆盖（原写死 14）
MAX_HISTORY = 400        # 去重池最多记多少首


def _history_file() -> Path:
    state = paths.state_dir()
    state.mkdir(parents=True, exist_ok=True)
    return state / paths.RECENT_FILENAME


def _playlist_history_file() -> Path:
    state = paths.state_dir()
    state.mkdir(parents=True, exist_ok=True)
    return state / paths.PLAYLIST_HISTORY_FILENAME


def _read_raw() -> Path | None:
    """旧文件（recent_music_history.json）的兼容读：新名优先，再退更老的名字。"""
    path = _history_file()
    if path.exists():
        return path
    for name in paths.LEGACY_RECENT_FILENAMES:
        legacy = paths.state_dir() / name
        if legacy.exists():
            return legacy
    return None


# ---------------------------------------------------------------------------
# 歌单台账（state/recent_playlist_history.json）
#
# 它是"我们最近生成过什么"的**唯一事实来源**：选曲去重池（recent_songs/recent_keys）
# 和 recent_tracks 工具都从它派生。每张歌单连**曲目**一起记，所以"上周三那张里有啥"
# 直接可查，不用再翻别的地方。
#   历史：以前只记歌（recent_music_history.json，没有歌单维度）—— 那个文件现已停止写入，
#   读取仍兼容（老数据 7 天后自然过期，之后可以把这个兼容分支删掉）。
#   只记日常歌单；**手动建单不写**（用户明确要那份清单，不该污染日常去重池）。
# ---------------------------------------------------------------------------

def recent_playlists(limit: int = 0) -> list[dict[str, Any]]:
    """歌单台账，新 → 旧。``limit=0`` 表示全部。"""
    try:
        payload = json.loads(_playlist_history_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = [row for row in (payload.get("playlists") or []) if isinstance(row, dict)]
    return rows[:limit] if limit and limit > 0 else rows


def recent_playlist_names(limit: int = 5) -> list[str]:
    """最近生成过的歌单名（给返回值 / 起名工具做参考）。"""
    return [str(row.get("name") or "") for row in recent_playlists(limit) if row.get("name")]


def record_playlist(*, name: str, date: str = "", theme: str = "", name_source: str = "",
                    platforms: list[str] | None = None,
                    songs: list[dict[str, str]] | None = None,
                    kind: str = "daily") -> dict[str, Any]:
    """把一次**成功**建单写进台账（只留最近 PLAYLIST_HISTORY_LIMIT 张）。"""
    entry: dict[str, Any] = {
        "name": str(name or ""), "date": str(date or ""), "theme": str(theme or ""),
        "name_source": str(name_source or ""), "kind": str(kind or "daily"),
        "platforms": [str(item) for item in (platforms or [])],
        "songs": [{"name": str(song.get("name") or ""), "artist": str(song.get("artist") or "")}
                  for song in (songs or []) if isinstance(song, dict) and song.get("name")],
        "created_at": time.time(),
    }
    rows = [entry, *recent_playlists()]
    _playlist_history_file().write_text(
        json.dumps({"updated_at": time.time(),
                    "playlists": rows[:PLAYLIST_HISTORY_LIMIT]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    return entry


def recent_songs(days: int = RECENT_DAYS) -> list[dict[str, Any]]:
    """去重池：最近 ``days`` 天生成过的歌（``days=0`` = 不去重）。

    来源：① 歌单台账里的曲目（唯一写入方）；② 旧文件 recent_music_history.json（兼容读）。
    """
    window = max(0, int(days))
    cutoff = time.time() - window * 86400
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in recent_playlists():
        when = float(row.get("created_at") or 0)
        if when < cutoff:
            continue
        for song in row.get("songs") or []:
            key = song_key(song.get("name"), song.get("artist"))
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({"name": song.get("name"), "artist": song.get("artist"),
                        "timestamp": when})
            if len(out) >= MAX_HISTORY:
                return out
    source = _read_raw()
    if source is not None and window > 0:
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        for item in payload.get("songs", []):
            if not isinstance(item, dict):
                continue
            if float(item.get("timestamp") or 0) < cutoff:
                continue
            key = song_key(item.get("name"), item.get("artist"))
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= MAX_HISTORY:
                break
    return out


def recent_keys(days: int = RECENT_DAYS) -> set[str]:
    """去重池的 key 集合（选曲时用来避开最近推过的歌）。"""
    return {song_key(item.get("name"), item.get("artist")) for item in recent_songs(days)}


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_liked(platform: str, songs: list[Song],
               sources: list[str] | None = None) -> dict[str, Any]:
    """把某个平台的收藏落一份快照（每次拉取时覆盖刷新）。"""
    payload = {"platform": platform,
               "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "count": len(songs), "sources": list(sources or []),
               "songs": [song.as_dict() for song in songs]}
    _write(paths.liked_file(key_of(platform)), payload)
    return payload


def load_liked(platform: str) -> dict[str, Any]:
    return _read(paths.liked_file(key_of(platform)))


def read_profile() -> dict[str, Any]:
    return _read(paths.state_file(paths.PROFILE_FILENAME))


def write_profile(payload: dict[str, Any]) -> dict[str, Any]:
    path = paths.state_file(paths.PROFILE_FILENAME)
    merged = _read(path)
    merged.update(payload or {})
    merged["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write(path, merged)
    return merged


def migrate_legacy() -> list[str]:
    """把旧命名的 state 文件迁到新口径（只做一次，重复跑是幂等的）。"""
    state = paths.state_dir()
    moved: list[str] = []

    # 1) 推荐历史：recent_json -> recent_music_json
    recent_target = state / paths.RECENT_FILENAME
    if not recent_target.exists():
        for name in paths.LEGACY_RECENT_FILENAMES:
            source = state / name
            if source.exists():
                source.replace(recent_target)
                moved.append(f"{name} -> {paths.RECENT_FILENAME}")
                break

    # 2) 画像：profile.json + profile_summary.json -> music_prefer_profile.json
    profile_target = state / paths.PROFILE_FILENAME
    merged = _read(profile_target)
    for name in paths.LEGACY_PROFILE_FILENAMES:
        source = state / name
        if not source.exists():
            continue
        old = _read(source)
        if name == "profile.json":
            merged["profile"] = old or merged.get("profile")
        else:
            merged["summary"] = old.get("summary") or merged.get("summary")
            for key in ("updated_at", "sources", "total"):
                if old.get(key) is not None:
                    merged.setdefault(key, old[key])
        source.unlink()
        moved.append(f"{name} -> {paths.PROFILE_FILENAME}")
    if moved and merged:
        merged["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write(profile_target, merged)

    # 3) songs_raw.json：各平台 *_liked.json 已覆盖它的作用，直接清掉
    stale = state / paths.LEGACY_SONGS_RAW_FILENAME
    if stale.exists():
        stale.unlink()
        moved.append(f"删除 {paths.LEGACY_SONGS_RAW_FILENAME}")
    return moved


async def _safe(operation: Callable[[], Awaitable[Any]], label: str
                ) -> tuple[list[Song], list[str], dict[str, Any]]:
    """平台能力出错不应该拖垮整条链路：吞掉异常、记进诊断，返回空结果。"""
    try:
        return await operation()
    except Exception as exc:  # noqa: BLE001 - 诊断信息就是要原样带给用户
        return [], [], {"errors": [f"{label}: {exc.__class__.__name__}: {exc}"]}


def _profile_ranked(songs: list[Song], profile: dict[str, Any]) -> list[Song]:
    """按画像给每首歌打权重，命中 top_artists 的歌手 rank=2，命中 mood 的 rank=1。

    画像为空或字段缺失时全部返回 rank=0，行为和以前完全一致。
    """
    top = [normalize(a) for a in (profile.get("top_artists") or []) if a]
    moods = [normalize(m) for m in (profile.get("mood_tendencies") or []) if m]
    ranked: list[Song] = []
    for song in songs:
        rank = 0
        artist_norm = normalize(song.artist)
        if artist_norm:
            for target in top:
                if target and (artist_norm == target or artist_norm in target or target in artist_norm):
                    rank = 2
                    break
        if rank == 0 and moods:
            name_norm = normalize(song.name)
            for mood in moods:
                if mood and (mood in name_norm or name_norm in mood):
                    rank = 1
                    break
        ranked.append(Song(song.name, song.artist, rank))
    return ranked


async def build_pool(services: Any = "") -> dict[str, Any]:
    """全平台候选池。services 为空 = 所有平台（含未登录的，未登录只贡献今日推荐）。

    池子会按画像加权：命中 top_artists 的歌手 rank=2，命中 mood_tendencies 的 rank=1。
    """
    keys = resolve_keys(services)
    pool: list[Song] = []
    seen: set[str] = set()
    sources: dict[str, list[str]] = {}
    diagnostics: dict[str, Any] = {}
    need_login: list[str] = []

    for key in keys:
        platform = get_platform(key)
        if platform is None:
            continue
        recommended, rec_sources, rec_diag = await _safe(platform.recommendations, f"{key}.recommendations")
        favorites, fav_sources, fav_diag = await _safe(platform.favorites, f"{key}.favorites")
        diagnostics[key] = {"recommend": rec_diag, "favorites": fav_diag}
        if rec_diag.get("need_login") or fav_diag.get("need_login"):
            need_login.append(key)
        sources[key] = [f"今日推荐: {', '.join(rec_sources) or '无'}",
                        f"收藏: {', '.join(fav_sources) or '无'}"]
        if favorites:
            # 顺手把收藏落一份快照：state/<platform>_liked.json
            save_liked(key, favorites, fav_sources)
        for song in list(recommended) + list(favorites):
            normalized = song_key(song.name, song.artist)
            if not song.name or normalized in seen:
                continue
            seen.add(normalized)
            pool.append(song)

    profile = read_profile().get("profile") or {}
    if profile:
        pool = _profile_ranked(pool, profile)
    return {"ok": bool(pool), "services": keys, "songs": pool, "sources": sources,
            "need_login": need_login, "diagnostics": diagnostics,
            "profile_applied": bool(profile), "profile_top_artists": profile.get("top_artists", [])}


def parse_include(text: str) -> list[Song]:
    """解析用户点名要的歌：每首写成 ``歌名 - 歌手``（只写歌名也行）。

    用什么分隔都认：中英文逗号 / 中英文分号 / 换行。
    """
    items: list[Song] = []
    normalized = (str(text or "").replace("；", ",").replace(";", ",")
                  .replace("，", ",").replace("\r", ",").replace("\n", ","))
    for raw in normalized.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if " - " in entry:
            name, artist = entry.split(" - ", 1)
        else:
            name, artist = entry, ""
        if name.strip():
            items.append(Song(name.strip(), artist.strip()))
    return items


def select_songs(songs: list[Song], count: int,
                 include: list[Song] | None = None,
                 avoid_days: int = RECENT_DAYS) -> tuple[list[Song], int]:
    """去重 -> 优先挑最近 ``avoid_days`` 天没推过的 -> 取前 count 首。

    include 里点名的歌**永远排在前面**（用户要的必须进），剩下的用池子补齐。
    ``avoid_days`` 由调用方决定（默认 7 天）：给 0 就是"不看去重池，随便挑"。
    返回 (选中的, 复用了几首)。
    """
    recent = recent_keys(avoid_days)
    seen: set[str] = set()
    unique: list[Song] = []
    by_key: dict[str, Song] = {}
    for song in songs:
        key = song_key(song.name, song.artist)
        if not song.name or key in seen:
            continue
        seen.add(key)
        unique.append(song)
        by_key[key] = song

    forced: list[Song] = []
    for song in include or []:
        key = song_key(song.name, song.artist)
        if key in by_key:                 # 池子里有就用池子的（歌手信息更全）
            forced.append(by_key[key])
        else:
            forced.append(song)
            by_key[key] = song

    forced_keys = {song_key(song.name, song.artist) for song in forced}
    unique = [song for song in unique if song_key(song.name, song.artist) not in forced_keys]
    fresh = sorted((song for song in unique if song_key(song.name, song.artist) not in recent),
                   key=lambda s: -s.rank)
    repeated = sorted((song for song in unique if song_key(song.name, song.artist) in recent),
                      key=lambda s: -s.rank)
    selected = (forced + fresh + repeated)[:max(1, count)]
    reused = sum(1 for song in selected if song_key(song.name, song.artist) in recent)
    return selected, reused


async def create_daily_playlist(services: Any = "", count: int = 40,
                                playlist_name: str = "", date: str = "",
                                prompt: str = "", include: str = "",
                                with_weather: bool = True,
                                avoid_days: int = RECENT_DAYS) -> dict[str, Any]:
    """今日歌单：全平台池子挑 count 首 -> 每个已登录平台各建一份同名歌单。"""
    pool = await build_pool(services)
    if not pool["songs"]:
        return {"ok": False, "error": "没有取到任何候选歌曲",
                "services": pool["services"], "need_login": pool["need_login"],
                "service": (pool["need_login"] or [""])[0],
                "diagnostics": pool["diagnostics"]}

    day = date or time.strftime("%Y%m%d")
    weather = await current() if with_weather else {"ok": False}
    selected, reused = select_songs(pool["songs"], count, parse_include(include), avoid_days)
    # 今日歌单：8 位日期前缀必须保留，所以先给主题留预算再拼，截断不会切到日期
    theme, name_source = resolve_theme(prompt, playlist_name, day, weather, selected)
    name = f"{day}-{limit_name(theme, NAME_LIMIT - DATE_PREFIX_LEN)}"

    results: list[dict[str, Any]] = []
    for key in pool["services"]:
        platform: MusicPlatform | None = get_platform(key)
        if platform is None:
            continue
        if not platform.status().get("configured"):
            # 没登录就跳过这个平台，但要说清楚，别让用户以为歌单建出来了
            results.append({"platform": key_of(key), "ok": False, "skipped": "not_logged_in",
                            "playlist_name": name, "error": platform.auth_hint()})
            continue
        try:
            created = await platform.create_playlist(name, selected, len(selected))
            results.append(created.as_dict())
        except Exception as exc:  # noqa: BLE001 - 一个平台失败不该影响其他平台
            results.append({"platform": key_of(key), "ok": False, "playlist_name": name,
                            "error": f"{exc.__class__.__name__}: {exc}"})

    created = [item for item in results if item.get("ok")]
    if created:
        # 台账记「真的加进去的歌」（多平台合并去重后一份）
        merged: list[dict[str, str]] = []
        seen_keys: set[str] = set()
        for item in created:
            for song in item.get("matched") or []:
                key = song_key(song.get("name"), song.get("artist"))
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    merged.append({"name": str(song.get("name") or ""),
                                   "artist": str(song.get("artist") or "")})
        record_playlist(name=name, date=day, theme=theme, name_source=name_source,
                        platforms=[str(item.get("platform") or "") for item in created],
                        songs=merged, kind="daily")
    return {"ok": bool(created), "playlist_name": name, "count": len(selected),
            "services": pool["services"], "pool_size": len(pool["songs"]),
            "pool_sources": pool["sources"], "reused_recent": reused,
            "theme": theme, "name_source": name_source,
            # 起名/改名用：主题还能写几个字、最近生成过什么名字
            "name_budget": NAME_LIMIT - DATE_PREFIX_LEN,
            "recent_names": recent_playlist_names(5),
            "avoid_days": avoid_days,
            "weather": weather,
            # 服务器上按 IP 定位时把「提醒用户设 MUSIC_WEATHER_CITY」透出来，agent 直接转述即可
            "weather_hint": weather.get("hint", "") if isinstance(weather, dict) else "",
            "results": results, "need_login": pool["need_login"],
            # 给 _with_login_options 用：把「缺哪个平台」挑一个出来附登录方式清单
            "service": (pool["need_login"] or [""])[0],
            "diagnostics": pool["diagnostics"],
            "profile_applied": pool.get("profile_applied", False),
            "profile_top_artists": pool.get("profile_top_artists", [])}


def build_summary(songs: list[tuple[str, str]], sources: list[str]) -> str:
    artists: Counter[str] = Counter()
    langs: Counter[str] = Counter()
    for name, artist in songs:
        if artist and not artist.startswith("("):
            artists[artist] += 1
        text = f"{name} {artist}"
        if re.search(r"[\u4e00-\u9fff]", text):
            langs["中文"] += 1
        elif re.search(r"[\uac00-\ud7af]", text):
            langs["韩文"] += 1
        elif re.search(r"[\u3040-\u30ff]", text):
            langs["日文"] += 1
        else:
            langs["英文/其他"] += 1
    total_lang = sum(langs.values()) or 1
    samples: list[str] = []
    for index in list(range(min(5, len(songs)))) + list(range(50, len(songs), 50)):
        entry = f"{songs[index][0]} - {songs[index][1]}"
        if entry not in samples:
            samples.append(entry)
    return "\n".join([
        f"总计 {len(songs)} 首歌",
        f"来源: {'; '.join(sources) or '无'}",
        "",
        "歌手 TOP20:",
        ", ".join(f"{name}({count})" for name, count in artists.most_common(20)),
        "",
        "语种分布: " + ", ".join(f"{key} {value / total_lang * 100:.0f}%" for key, value in langs.most_common()),
        "",
        f"采样歌曲 ({len(samples)} 首):",
        "\n".join(f"  - {entry}" for entry in samples),
    ])


# ---------------------------------------------------------------------------
# 画像（profile server 用）：把全平台收藏汇总成一份统计摘要
# ---------------------------------------------------------------------------

def recent_tracks() -> dict[str, Any]:
    """最近 14 天推过的歌（去重上下文）。"""
    recent = recent_songs(RECENT_DAYS)
    text = "\n".join(f"- {item.get('name')} - {item.get('artist', '')}".strip(" -")
                     for item in recent[:60]) or "无"
    return {"ok": True, "count": len(recent), "text": text}


async def analyze(services: Any = "") -> dict[str, Any]:
    """拉取各平台收藏，做一份统计摘要，交给模型写画像。

    和建歌单一样按注册表遍历：services 留空 = 所有平台，新加一个平台不用改这里。
    """
    targets = resolve_keys(services)
    all_songs: list[tuple[str, str]] = []
    sources: list[str] = []
    diagnostics: dict[str, Any] = {}
    for member in targets:
        platform = get_platform(member)
        if platform is None:
            continue
        try:
            songs, src, diag = await platform.favorites()
        except Exception as exc:  # noqa: BLE001 - 诊断信息原样带给用户
            songs, src = [], []
            diag = {"errors": [f"{member.value}.favorites: {exc.__class__.__name__}: {exc}"]}
        diagnostics[member.value] = diag
        if songs:
            sources.append(f"{platform.label}: {', '.join(src) or '无'}")
            # 收藏快照与建单流程**同一口径**：state/<platform>_liked.json。
            # 以前只有建歌单会写，跑完 analyze 反而在 state/ 里找不到快照。
            save_liked(member.value, songs, src)
        all_songs.extend((song.name, song.artist) for song in songs)
    need_login = [name for name, info in diagnostics.items() if info.get("need_login")]
    if not all_songs:
        return {"ok": False, "error": "未取到任何歌曲", "sources": sources,
                "diagnostics": diagnostics, "need_login": need_login,
                "hint": "逐平台看 diagnostics[平台].errors：常见是没登录（need_login）或账号体系登错了"
                        "（例如微信绑定的 QQ 音乐号）。先 credential_logout(service=...)，"
                        "再用 login_methods / login_start 重新登录对应账号。"}
    summary = build_summary(all_songs, sources)
    # 只落一份画像素材（music_prefer_profile.json）；原始歌单不再另存，
    # 各平台的 *_liked.json 快照已经承担了「原始收藏」这件事。
    write_profile({"summary": summary, "sources": sources, "total": len(all_songs)})
    return {"ok": True, "total": len(all_songs), "sources": sources, "summary": summary,
            "diagnostics": diagnostics, "need_login": need_login,
            "hint": "依据 summary 生成画像 JSON 后调用 save_profile"}


def save_profile(profile: dict[str, Any]) -> dict[str, Any]:
    merged = write_profile({"profile": profile})
    return {"ok": True, "path": str(paths.state_file(paths.PROFILE_FILENAME)),
            "profile": merged.get("profile", profile)}


def get_profile() -> dict[str, Any]:
    return read_profile().get("profile", {})


# ---------------------------------------------------------------------------
# 「需要登录」的统一应答
# ---------------------------------------------------------------------------

def unconfigured_services() -> list[str]:
    """哪些平台还没登录。生成歌单前用它回答「要不要全部登录」。"""
    missing: list[str] = []
    for member in DEFAULT_ORDER:
        platform = get_platform(member)
        if platform is None or not platform.status().get("configured"):
            missing.append(member.value)
    return missing


def need_login(service: Any, error: str) -> dict[str, Any]:
    """统一的「需要登录」结构化返回值，由 servers 层补上登录方式清单。"""
    configured = {member.value: member.value not in unconfigured_services()
                  for member in DEFAULT_ORDER}
    key = key_of(service)
    configured[key] = False  # 能走到这里就说明这个平台不可用，哪怕 vault 里还留着一段废凭证
    return {"ok": False, "need_login": True, "service": key, "error": error,
            "configured": configured}
