# crates-music-mcp

音乐 MCP 服务器，覆盖 **QQ 音乐 / 网易云 / 酷狗** 三平台：扫码与验证码登录、偏好画像分析、歌单创建与推荐。一个包、21 个工具、一份共享凭证。

## 安装

```bash
pip install .                 # 本地路径
pip install git+https://github.com/<owner>/crates-music-mcp.git   # 从 git
pip install crates-mcp        # 发布到 PyPI 后
```

装完得到 4 个命令：

```bash
crates-music      # 单 server，全部 21 个工具（默认 stdio）—— 推荐
crates-auth       # 只带登录/凭证工具
crates-profile    # 只带画像工具
crates-playlist   # 只带建单/推荐工具
```

## 接到客户端

**和普通 MCP 一样：客户端调用时自动拉起进程，用完即退。不需要服务器常驻、不需要 systemd、不需要 HTTP。**

客户端配置里写启动命令即可：

```json
{ "mcpServers": { "music": { "command": "crates-music", "args": [] } } }
```

Codex 用 TOML：

```toml
[mcp_servers.music]
command = "crates-music"
args = []
```

工具名会带 server 前缀（如 `music_create_daily_playlist`）。

## 21 个工具

**登录与凭证（8）**
`login_methods` · `login_start` · `login_poll` · `login_submit` · `credential_status` · `credential_refresh` · `credential_set_cookie` · `credential_logout`

**画像与听歌数据（4）**
`analyze_music_profile` · `get_profile` · `save_profile` · `recent_tracks`

**歌单与推荐（9）**
`create_daily_playlist` · `create_qq_playlist` · `create_ncm_playlist` · `create_kugou_playlist` · `daily_recommendations` · `ncm_liked_songs` · `delete_playlist` · `my_playlists` · `qq_recent`

## 登录

三平台登录方式：

| 平台 | 方式 |
| --- | --- |
| QQ 音乐 | `phone_code`（默认）/ `mobile_qr` / `qq_qr` / `wx_qr` |
| 网易云 | `cookie`（推荐）/ `phone_code` / `qr` |
| 酷狗 | `qr`（默认）/ `phone_code` |

推荐用手机验证码（全程纯文本、无二维码、跨设备零差异）：

```
login_start(service="qq_music", method="phone_code", phone="138...")
   → 用户把验证码输入对话框
login_submit(session_id=..., code="123456")
```

凭证**自动刷新**：QQ 到期前 6 小时按需刷新，网易云距上次续期超 12 小时主动调 `/login/refresh`。跨进程 `flock` 互斥，保证不会同时刷新。

## 生成歌单

```
create_daily_playlist(count=40)                              # 全平台各建一份
create_daily_playlist(count=100, prompt="今天心情不好")      # 主题自动取心情
create_daily_playlist(include="晴天 - 周杰伦", count=20)     # 点名的歌排最前
```

- 候选池 = 所有平台「今日推荐 + 全部收藏」去重，避开最近 14 天推过的
- **画像权重选曲**：命中 `top_artists` 的歌 rank=2，命中 `mood_tendencies` 的 rank=1，自动偏向口味
- 每首歌在目标平台现场搜索，找不到就跳过（记进 `failed`）
- 歌单名 = `<日期>-<主题>`，主题取自心情/场景/天气

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

覆盖路径用 `MUSIC_DATA_DIR`（一处决定 `secrets/` 与 `state/`）。

## 依赖

`mcp` / `qqmusic-api-python` / `pymusiclibrary` / `qrcode` / `Pillow` / `httpx` / `uvicorn`，由 `pyproject.toml` 声明，pip 自动装齐。

- 网易云**默认走纯 Python 后端**：自己算 weapi/eapi 参数直连官方 WebAPI，零原生依赖，
  Linux 上照样跑，不需要容器、不需要 `pymusiclibrary`
- 酷狗引擎调用走一次性子进程，崩溃不污染主进程（这条仍然依赖 `pymusiclibrary`）

### 网易云后端：只有一套，纯 Python

`music_mcp/platforms/ncm_native.py`（路由）+ `ncm_crypto.py`（weapi/eapi 加密）直连官方
WebAPI，**零原生依赖、不需要容器**，Windows / Linux 行为一致。

以前这里还有两条路，都删了：本地 `MusicLibrary` 引擎（pymusiclibrary 的 manylinux
wheel 里装的是 Windows DLL，Linux 上永远起不来）和 ncm-api sidecar 容器（要多养一个服务）。
`NCM_BACKEND` 环境变量现在只做兼容：设了别的值也照样走这一套，只在 stderr 提示一句。

想确认当前用的是哪套：`make_ncm_client().backend`（恒为 `native`），或看
`credential_status` 里 netease 的 `backend` 字段。离线自检（不需要凭证、不需要网络）：

```bash
python tests/test_ncm_native.py              # AES/RSA/weapi/eapi 结构与官方向量
NCM_LIVE=1 python tests/test_ncm_native.py   # 加打一次真实搜索/推荐，验加密是否被上游接受
```


## 开发

```bash
pip install -e .
python tests/test_stdio.py all   # 冒烟：21 个工具
```
