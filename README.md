# crates-music-mcp

[![CI](https://github.com/guohangbk/crates-music-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/guohangbk/crates-music-mcp/actions/workflows/ci.yml)

> ### 把你三个音乐 App 的收藏交给 Agent，让它每天替你挑一张对味的歌单。
>
> **网易云 · QQ 音乐 · 酷狗** —— 一个 MCP server、27 个工具、一份共享凭证、**零原生依赖**。

大多数"音乐 MCP"只做一件事：把接口包一层，让模型能搜歌。这个包多做两步——
**先读懂你的耳朵，再替你选歌**：

| 它做什么 | 具体怎么做的 |
| --- | --- |
| **读** | 三家「我喜欢 / 收藏」+ 今日推荐全部拉下来，按「歌名 + 歌手」跨平台去重（实测一份池子 **1500+ 首**） |
| **懂** | 把候选池交给模型，产出结构化偏好画像：常听歌手、口味、情绪倾向，缓存到本地，下次不用重算 |
| **推** | 按画像权重从池子里挑歌，避开最近 14 天推过的，**在目标平台现场搜歌**再建单（搜不到就跳过，不硬塞） |

全程在你自己机器上跑：**没有容器、没有 sidecar、没有云端账号**，凭证就是你自己的登录 Cookie。
Windows / Linux 行为一致——三平台的协议都是纯 Python 实现的。

## 一分钟跑通

**推荐**：直接从仓库装，不用先克隆：

```bash
pip install git+https://github.com/guohangbk/crates-music-mcp.git
```

如果卡在 `Could not connect to server`（国内直连 github.com:443 经常被挡），两个办法：
挂代理，或改用 SSH 地址：

```bash
pip install "git+ssh://git@github.com/guohangbk/crates-music-mcp.git"
```

或者克隆下来再装 —— 想改代码、跑自检就用这种：

```bash
git clone https://github.com/guohangbk/crates-music-mcp.git   # 连不上就用 git@github.com:guohangbk/crates-music-mcp.git
cd crates-music-mcp
pip install -e .
```

> 目前**还没发布到 PyPI**，所以没有 `pip install crates-music-mcp` 这条路；上面两种是现在就能用的。

装完得到 4 个命令：

```bash
crates-music      # 单 server，全部 27 个工具（默认 stdio）—— 推荐
crates-auth       # 只带登录/凭证工具
crates-profile    # 只带画像工具
crates-playlist   # 只带建单/推荐工具
```

接到客户端（**客户端调用时自动拉起进程，用完即退**，不需要常驻、不需要 systemd）：

```json
{ "mcpServers": { "music": { "command": "crates-music", "args": [] } } }
```

Codex 用 TOML：

```toml
[mcp_servers.music]
command = "crates-music"
args = []
```

工具名会带 server 前缀（如 `music_create_daily_playlist`）。然后就能这样用：

```
你：看看我最近都听什么，给我建一张今天的歌单
Agent：analyze_music_profile → 画像：偏爱华语流行 + 电子，常听 邓紫棋 / 陈奕迅 / 梁博
       create_daily_playlist(count=40, prompt="今天想安静点")
       → 网易云《20261107-独处时光》40 首 / QQ音乐 40 首 / 酷狗 40 首
```

## 27 个工具

**登录与凭证（8）**
`login_methods` · `login_start` · `login_poll` · `login_submit` · `credential_status` · `credential_refresh` · `credential_set_cookie` · `credential_logout`

**画像与听歌数据（4）**
`analyze_music_profile` · `get_profile` · `save_profile` · `recent_tracks`

**歌单、搜索与推荐（15）**
`create_daily_playlist` · `create_qq_playlist` · `create_ncm_playlist` · `create_kugou_playlist` ·
`liked_songs` · `ncm_liked_songs` · `search_songs` · `playlist_songs` · `add_songs_to_playlist` ·
`recent_played` · `daily_recommendations` · `my_playlists` · `delete_playlist` ·
`recent_recommended_tracks` · `qq_recent`

三个工具容易混，先分清：

| 工具 | 返回什么 | 不是什么 |
| --- | --- | --- |
| `liked_songs(service)` | 某平台**全部收藏**（「我喜欢」全量） | 不是统计摘要，也不是"最近听的" |
| `recent_played(service)` | **用户最近听过的歌**（网易云听歌排行 / 酷狗听歌历史） | 不是我们推荐过的歌；QQ 暂不支持 |
| `recent_recommended_tracks` / `recent_tracks` / `qq_recent` | **我们最近 14 天推荐过的歌**（建单去重用） | 与用户听歌历史无关（`qq_recent` 这名字是历史遗留，也不专指 QQ） |

## 登录

| 平台 | 方式 |
| --- | --- |
| QQ 音乐 | `phone_code`（默认）/ `mobile_qr` / `qq_qr` / `wx_qr` |
| 网易云 | `phone_code`（默认）/ `cookie` / `qr` |
| 酷狗 | `phone_code`（默认）/ `qr` |

三端都默认手机验证码（纯文本、不用截图扫码、跨设备零差异）：

```
login_start(service="qq_music", method="phone_code", phone="138...")
   → 用户把验证码输入对话框
login_submit(session_id=..., code="123456")
```

凭证**自动续期**：QQ 到期前 6 小时按需刷新，网易云距上次续期超 12 小时主动调 `/login/refresh`；
跨进程 `flock` 互斥，保证同一份凭证不会被两个进程同时刷新。

### 登录会话：有效期按真实值上报，进程重启也能续

- `login_start` 返回的 `expires_in_seconds` 是**该平台二维码的真实寿命**，不是一刀切的会话 TTL
  （另有 `session_ttl_seconds` 表示我们最多留多久）：

  | 方式 | 实测有效期 | 依据 |
  | --- | --- | --- |
  | 网易云 `qr` | 300 s | 291 s 仍回 801、306 s 回 800 |
  | QQ `qq_qr` / `wx_qr` | 120 s | 114 s 仍回 SCAN、131 s 回 TIMEOUT |
  | QQ `mobile_qr` | 300 s（MQTT 监听窗口） | 走推送，没有可询的 HTTP 状态 |
  | 酷狗 `qr` | ≥480 s | 实测 8 分钟仍可用，上报按会话窗口 600 s |

- **会话落盘**（`state/login_sessions.json`）：票据是纯数据的那些（网易云/酷狗的 `unikey`、
  QQ 的 `qrsig`、验证码登录的 `phone`）会被持久化，MCP 进程重启后 `login_poll` 照样能续。
  QQ `mobile_qr` 例外——它的登录结果由 MQTT 推送带回来，错过就没了，必须在当前进程里完成。

## 生成歌单

```
create_daily_playlist(count=40)                              # 全平台各建一份
create_daily_playlist(count=100, prompt="今天心情不好")      # 主题自动取心情
create_daily_playlist(include="晴天 - 周杰伦", count=20)     # 点名的歌排最前
```

- 候选池 = 所有平台「今日推荐 + 全部收藏」去重，避开最近 14 天推过的
- **画像权重选曲**：命中 `top_artists` 的歌 rank=2，命中 `mood_tendencies` 的 rank=1
- 每首歌在目标平台现场搜索，找不到就跳过（记进 `failed`，绝不硬塞一首不相干的）
- 歌单名 = `<日期>-<主题>`，主题取自心情 / 场景 / 天气

## 凭证 vault

数据默认落在**用户级目录**（不写进安装目录）：

| 文件 | 内容 |
| --- | --- |
| `secrets/qq_music.json` | QQ 音乐完整 Credential（含 refresh_token） |
| `secrets/netease.json` | 网易云 Cookie |
| `secrets/kugou.json` | 酷狗 Cookie（token + userid） |
| `state/*_liked.json` | 各平台收藏快照 |
| `state/music_prefer_profile.json` | 偏好画像 |
| `state/recent_music_history.json` | 最近 14 天推荐历史（去重口径） |
| `state/login_sessions.json` | 未完成的登录会话（可跨进程续，见上） |

覆盖路径用 `MUSIC_DATA_DIR`（一处决定 `secrets/` 与 `state/`）。

## 三个平台的上游出处

协议不是凭空写的：**网易云、酷狗的签名与加密都是照公开实现逐字复刻的**，QQ 直接用现成的纯 Python 库。

| 平台 | 上游项目 | 许可 |
| --- | --- | --- |
| QQ 音乐 | [L-1124/QQMusicApi](https://github.com/L-1124/QQMusicApi)（[文档](https://l-1124.github.io/QQMusicApi/)） | MIT |
| 网易云 | [NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced) | MIT |
| 酷狗 | [MakcRe/KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi) | MIT |

## 依赖与自检

运行依赖只有 `mcp` / `qqmusic-api-python` / `qrcode` / `Pillow` / `httpx` / `uvicorn`，
由 `pyproject.toml` 声明，pip 自动装齐。

自检：

```bash
python tests/test_ncm_native.py                # 网易云：AES/RSA/weapi/eapi 与官方向量
NCM_LIVE=1 python tests/test_ncm_native.py     #   再加真实搜索 / 推荐 / 登录态
python tests/test_kugou_native.py              # 酷狗：签名/RSA/AES 与上游 JS 逐字节对拍
KUGOU_LIVE=1 python tests/test_kugou_native.py #   再加真实搜索 / 歌单 / 曲目 / 二维码
python tests/test_login_resumable.py           # 登录会话：有效期 + 跨进程续（含另起进程验证）
```

## 项目结构

```
music_mcp/
├── app.py              入口：起 MCP server（stdio / http 两种传输，4 个命令都走它）
├── servers.py          MCP 工具定义（21 个）：校验参数 → 转给业务层 → 结果 JSON 化
├── compose.py          业务编排：画像加权选曲、跨平台建单、最近 14 天去重
├── login.py            登录会话注册表：发号 / 回收 / 落盘（跨进程可续）
├── vault.py            共享凭证库：三个进程共用一份，读-刷新-回写用 flock 互斥
├── paths.py            数据目录（用户级，MUSIC_DATA_DIR 一处覆盖）
├── qr.py               二维码本地渲染 + 127.0.0.1 回环图片服务
└── platforms/
    ├── __init__.py     平台注册表：枚举 ↔ 实现
    ├── qq.py           QQ 音乐 · 平台层（登录流程、收藏/推荐/搜索/建单）
    ├── netease.py      网易云 · 平台层（同上 + Cookie 续期）
    ├── kugou.py        酷狗 · 平台层（同上）
    └── core/           ← 共用底座 + 各平台协议实现（纯 Python 传输层）
        ├── base.py          平台接口 + 通用结构/工具（Song / LoginStart / normalize / dedupe…）
        ├── aes.py           通用 AES-128/192/256（CBC/ECB + PKCS7，标准库实现）
        ├── ncm_crypto.py    网易云 · 参数加密：weapi（双层 AES + 裸 RSA）/ eapi（AES-ECB + md5）
        ├── ncm_native.py    网易云 · 路由与请求：域名、参数、指纹 Cookie、响应归一
        ├── kugou_crypto.py  酷狗 · 签名/加密：md5 盐值签名、两种 RSA、三处 AES、设备指纹
        └── kugou_native.py  酷狗 · 路由与请求：14 条上游接口
```

一句话分工：

| 层 | 负责 | 不知道的事 |
| --- | --- | --- |
| `core/*_crypto.py` | **怎么签名**：把参数算成上游要的样子 | 不知道要调哪个接口 |
| `core/*_native.py` | **打哪个请求**：URL / 参数 / 头 / 响应 | 不知道"我喜欢"是什么 |
| `platforms/*.py` | **业务上要干什么**：登录编排、收藏、搜索、建单 | 不碰签名细节 |
| `servers.py` | **对外暴露哪些工具** | 任何平台差异 |

新增一个平台 = 一个 `x_crypto.py` + 一个 `x_native.py`（放 `core/`）+ 一个 `x.py`（放 `platforms/`），
再加一行注册。

## 开发

```bash
pip install -e .
python tests/test_stdio.py all    # 冒烟：27 个工具
python tests/test_methods.py      # 登录方式目录 + Cookie 导入 + 凭证状态
python tests/test_analyze_all.py  # 三平台画像（需要本地已有凭证）
```

CI（`.github/workflows/ci.yml`）在每次 push / PR 跑 **离线**自检：

| 跑什么 | 为什么是离线 |
| --- | --- |
| `test_ncm_native.py`（默认不带 `NCM_LIVE=1`） | 密码学对 FIPS-197 / NIST SP 800-38A 官方向量，不需要账号 |
| `test_kugou_native.py`（默认不带 `KUGOU_LIVE=1`） | 签名/RSA/AES 对上游 JS 的实际输出，参照值已固化进测试 |
| `test_profile_rank.py` / `test_methods.py` / `test_poll_fast.py` / `test_stdio.py` | 纯本地逻辑与 stdio 协议 |
| 打包检查 | 构建 wheel/sdist，断言 `platforms/core/` 与 `LICENSE` 都在里面 |

联网那部分（`NCM_LIVE=1` / `KUGOU_LIVE=1`）故意不进 CI：需要真实账号凭证，而且 CI 机房在境外、
访问国内音乐接口不稳定，跑出来是假红。要验联网就看「自检」那一节，在本地跑。


## 许可证

[MIT](LICENSE)。三个平台的上游实现同样是 MIT——**我们只对齐了协议行为、没有复制它们的代码**：
网易云/酷狗的签名与加密都是照公开协议自己实现的，QQ 那个库是以依赖形式引入的。
