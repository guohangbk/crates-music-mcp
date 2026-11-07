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

### 登录会话：有效期按真实值上报，跨进程可续

- `login_start` 返回的 `expires_in_seconds` 是**该平台二维码的真实寿命**，不是一刀切的会话 TTL
  （另有 `session_ttl_seconds` 表示我们最多留多久）：

  | 方式 | 实测有效期 | 依据 |
  | --- | --- | --- |
  | 网易云 `qr` | 300 s | 2026-11-07 实测：291 s 仍回 801、306 s 回 800 |
  | QQ `qq_qr` / `wx_qr` | 120 s | 实测：114 s 仍回 SCAN、131 s 回 TIMEOUT |
  | QQ `mobile_qr` | 300 s（MQTT 监听窗口） | 走推送，没有可询的 HTTP 状态 |
  | 酷狗 `qr` | ≥480 s（实测 8 分钟仍可用） | 没等到过期点，上报按会话窗口 600 s |

- **会话落盘**（`state/login_sessions.json`）：票据是纯数据的那些（网易云/酷狗的 `unikey`、
  QQ 的 `qrsig`、验证码登录的 `phone`）会被持久化，MCP 进程重启后 `login_poll` 照样能续。
  QQ `mobile_qr` 例外 —— 它的登录结果由 MQTT 推送带回来，错过就没了，所以必须在当前进程里完成。


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

`mcp` / `qqmusic-api-python` / `qrcode` / `Pillow` / `httpx` / `uvicorn`，由 `pyproject.toml` 声明，pip 自动装齐。

**三平台都跟原生库解耦了**：网易云、酷狗都是纯 Python 直连官方 WebAPI（自己算签名/加密），
QQ 走第三方的纯 Python 库。所以 Windows / Linux 行为一致，**不需要容器、不需要 sidecar、
不需要 `pymusiclibrary`**（那个包的 manylinux wheel 里塞的是 Windows DLL，本来也装不起来）。

### 网易云后端：只有一套，纯 Python

`music_mcp/platforms/ncm_native.py`（路由）+ `ncm_crypto.py`（weapi/eapi 加密）直连官方
WebAPI。以前这里还有两条路，都删了：本地 `MusicLibrary` 引擎（Linux 上永远起不来）和
ncm-api sidecar 容器（要多养一个服务）。`NCM_BACKEND` 只做兼容：设了别的值也照样走这一套。

### 酷狗后端：同样纯 Python

`music_mcp/platforms/kugou_native.py`（12 条路由）+ `kugou_crypto.py`（签名/AES/RSA/设备指纹）
逐字对照上游 [MakcRe/KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi)（MIT）实现 ——
那份 JS 就是 `kugou_music_api.dll` 里内嵌的代码。签名公式：

```
android: md5(SALT + 排序后的 k=v 串 + body + SALT)     SALT = OIlwieks28dk2k092lksi2UIkp
web    : 同上，盐换成 NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt（登录二维码那几条）
```

读歌单曲目用 App 现在的 `/v4/get_list_all_file_v3`（listid + token）；老的 gcid 路由
（`pubsongs/v2/get_other_list_file_nofilt`）2026-11-07 起回空，已降为兜底。

自检（离线部分拿上游 JS 的输出当参照值，联网部分真打上游）：

```bash
python tests/test_ncm_native.py               # AES/RSA/weapi/eapi 与官方向量
NCM_LIVE=1 python tests/test_ncm_native.py    # 真实搜索/推荐/登录态
python tests/test_kugou_native.py             # 签名/RSA/AES 与上游 JS 对拍
KUGOU_LIVE=1 python tests/test_kugou_native.py
```



## 开发

```bash
pip install -e .
python tests/test_stdio.py all   # 冒烟：21 个工具
```
