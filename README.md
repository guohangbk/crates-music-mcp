# crates-music-mcp

> ### 把你三个音乐 App 的收藏交给 Agent，让它每天替你挑一张对味的歌单。
>
> **网易云 · QQ 音乐 · 酷狗** —— 一个 MCP server、21 个工具、一份共享凭证、**零原生依赖**。

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

```bash
pip install .            # 或 pip install git+https://github.com/<owner>/crates-music-mcp.git
```

装完得到 4 个命令：

```bash
crates-music      # 单 server，全部 21 个工具（默认 stdio）—— 推荐
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

## 21 个工具

**登录与凭证（8）**
`login_methods` · `login_start` · `login_poll` · `login_submit` · `credential_status` · `credential_refresh` · `credential_set_cookie` · `credential_logout`

**画像与听歌数据（4）**
`analyze_music_profile` · `get_profile` · `save_profile` · `recent_tracks`

**歌单与推荐（9）**
`create_daily_playlist` · `create_qq_playlist` · `create_ncm_playlist` · `create_kugou_playlist` · `daily_recommendations` · `ncm_liked_songs` · `delete_playlist` · `my_playlists` · `qq_recent`

## 登录

| 平台 | 方式 |
| --- | --- |
| QQ 音乐 | `phone_code`（默认）/ `mobile_qr` / `qq_qr` / `wx_qr` |
| 网易云 | `cookie`（推荐）/ `phone_code` / `qr` |
| 酷狗 | `qr`（默认）/ `phone_code` |

最省事的是手机验证码（全程纯文本、无二维码、跨设备零差异）：

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

| 平台 | 上游项目 | 许可 | 我们和它的关系 |
| --- | --- | --- | --- |
| QQ 音乐 | [L-1124/QQMusicApi](https://github.com/L-1124/QQMusicApi)（[文档](https://l-1124.github.io/QQMusicApi/)） | MIT | **直接依赖**：`pip install qqmusic-api-python`，登录/搜索/歌单都走它 |
| 网易云 | [NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced) | MIT | **协议参照**：weapi/eapi、APP_CONF、指纹 Cookie 照它实现（`ncm_crypto.py` / `ncm_native.py`） |
| 网易云（前身） | [Binaryify/NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi) | MIT | 上面那个的上游 —— **2024-02 已归档、仓库清空**，所以我们的底稿取自引擎打包产物而不是它 |
| 网易云（打包方） | [2061360308/NeteaseCloudMusic_PythonSDK](https://github.com/2061360308/NeteaseCloudMusic_PythonSDK) | MIT | 早期装过的 `pymusiclibrary` 就是它；我们读的就是它打出来的 `ncm_music_api.dll` 里**明文内嵌的 JS** |
| 酷狗 | [MakcRe/KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi) | MIT | **协议参照**：`kugou_music_api.dll` 里那份 JS 就是它的压缩版，签名/AES/RSA 逐字节对拍（`kugou_crypto.py` / `kugou_native.py`） |

顺手记一笔差异：网易云**加歌**我们用 `/api/playlist/manipulate/tracks`（eapi），api-enhanced 用
`/api/playlist/track/add`（weapi）；酷狗读曲目我们用 App 现行的 `/v4/get_list_all_file_v3`，老的
gcid 路由 2026-11-07 起回空。两条路都实测可用，只是各走各的。

## 依赖与自检

运行依赖只有 `mcp` / `qqmusic-api-python` / `qrcode` / `Pillow` / `httpx` / `uvicorn`，
由 `pyproject.toml` 声明，pip 自动装齐。

**三平台都跟原生库解耦了**：网易云、酷狗纯 Python 直连官方 WebAPI（自己算签名/加密），
QQ 走第三方的纯 Python 库。所以不需要容器、不需要 sidecar、不需要 `pymusiclibrary`
（那个包的 manylinux wheel 里塞的是 Windows DLL，本来也装不起来）。
密码学部分全部是标准库实现（`aes.py` / `ncm_crypto.py` / `kugou_crypto.py`），并用
FIPS-197、NIST SP 800-38A 官方向量 + 上游 JS 输出双重对拍：

```bash
python tests/test_ncm_native.py                # 网易云：AES/RSA/weapi/eapi 与官方向量
NCM_LIVE=1 python tests/test_ncm_native.py     #   再加真实搜索 / 推荐 / 登录态
python tests/test_kugou_native.py              # 酷狗：签名/RSA/AES 与上游 JS 逐字节对拍
KUGOU_LIVE=1 python tests/test_kugou_native.py #   再加真实搜索 / 歌单 / 曲目 / 二维码
python tests/test_login_resumable.py           # 登录会话：有效期 + 跨进程续（含另起进程验证）
```

## 开发

```bash
pip install -e .
python tests/test_stdio.py all   # 冒烟：21 个工具
```
