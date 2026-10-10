# Bilibili Video Understanding MCP

一个供个人使用的、没有界面的 ChatGPT MCP 工具服务：提交 B 站视频链接，由服务器下载视频并分段调用小米 MiMo `mimo-v2.6-flash` 分析画面与声音，再按需读取带时间戳的中文结果。

> 本仓库只包含程序和部署模板。你需要自己的 MiMo API Key、服务器和 HTTPS 入口。Bilibili 与 Xiaomi MiMo 均为各自权利人的服务，本项目与它们没有官方关联。请遵守视频版权及相关服务条款。

## 工作方式

1. `analyze_bilibili_video(url)` 接受 BV 号、B 站视频网址或受支持的 B 站短链接，返回任务 ID。
2. 服务器查询视频信息，下载一个可用的视频流，使用 FFmpeg 按最多 5 分钟切片并压缩。
3. 每个片段独立以 MP4 Base64 视频输入请求 MiMo。结果逐段写入本地 SQLite 数据库；完整视频和临时片段在任务结束后删除。
4. `get_bilibili_analysis(job_id, offset, max_chars, wait_seconds)` 返回进度及结果文本。等待新进度最长 35 秒，使用 `next_offset` 继续读取。

同一 BV 号在相同模型和提示词版本下复用已完成或部分完成的结果。视频最长 60 分钟，原文件最大 250 MB，单段 Base64 最大 50 MB；整个服务同时只处理一个分析任务，滚动 24 小时最多新建 60 个任务。若 MiMo 返回 `content_filter`，已完成片段会保留，并明确标出缺失时间段。正常结束或异常退出时会删除临时视频；进程被强制杀死或机器断电可能留下临时文件，需要人工清理。

## 环境要求

- Python 3.11+
- `ffmpeg` 和 `ffprobe` 在 `PATH` 中
- 小米 MiMo 开放平台 API Key，可用 `mimo-v2.6-flash`
- 能访问 B 站和 MiMo API 的服务器
- 若连接 ChatGPT：公网可访问的 HTTPS MCP 地址

MiMo 的视频输入格式和计费以[官方视频理解文档](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/multimodal-understanding/video-understanding)为准。视频、音频和输出均可能消耗模型 token。

## 本地启动

```bash
git clone https://github.com/SorenProjections/bili-chatgpt-mcp.git
cd bili-chatgpt-mcp
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export MIMO_API_KEY='your-key-here'
python server.py
```

默认只监听 `127.0.0.1:18791`，MCP 路径为 `/mcp`。可通过 `BILI_MCP_PORT` 和 `BILI_MCP_DB` 改端口与数据库位置。不要把真实密钥写进源码或提交到 Git。

## 部署到 ChatGPT

`deploy/bili-chatgpt-mcp.service` 是 Linux systemd 用户服务示例，假设仓库克隆到 `~/bili-chatgpt-mcp`，并在 `~/.config/bili-chatgpt-mcp.env` 中保存 `MIMO_API_KEY=...`。将环境文件权限设为 `0600`，并按自己的目录调整服务文件。

`deploy/nginx-tunnel.conf.template` 提供只监听本机的 Nginx 代理示例。先把 `__PRIVATE_TOKEN__` 换成随机长路径，再通过你控制的 HTTPS 域名或隧道把该入口暴露给 ChatGPT。创建 MCP App 时填写 `https://<your-domain>/<your-private-path>/mcp`；当前示例没有 OAuth，因此选择无认证。

**注意：随机路径不等于账号认证。** 只把该地址交给可信任的客户端，不要公开真实连接地址。公开部署或多人共用前，应增加合适的身份验证和费用控制。临时隧道的地址可能变化，需要同步更新 ChatGPT 中的连接。

## 代码与数据

- `server.py`：MCP 工具、任务队列、MiMo 调用、结果缓存。
- `bili_video.py`：B 站信息查询、视频下载、短链接与视频时长处理。
- `deploy/`：可修改的 systemd 与 Nginx 示例。
- SQLite 中保留任务和分析文字；未实现自动清理历史分析。

本项目采用 [MIT License](LICENSE)。
