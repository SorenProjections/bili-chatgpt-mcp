"""Small Bilibili resolver and downloader used by the MCP server."""

import json
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


BILI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Accept": "*/*",
}


def bili_api(url):
    req = urllib.request.Request(url, headers=BILI_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def get_video_info(bvid):
    data = bili_api(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    if data.get("code") != 0:
        raise RuntimeError(f"B站视频信息获取失败：{data.get('message', 'unknown')}")
    video = data["data"]
    return {
        "bvid": bvid,
        "aid": video.get("aid"),
        "cid": video.get("cid"),
        "title": video.get("title", ""),
        "desc": video.get("desc", ""),
        "owner": video.get("owner", {}).get("name", ""),
        "duration": video.get("duration", 0),
        "views": video.get("stat", {}).get("view", 0),
    }


def get_video_urls(bvid, cid, qn=16):
    """Return MP4/CDN candidates, preferring backup mirrors."""
    data = bili_api(
        f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn={qn}&fnval=1"
    )
    if data.get("code") != 0:
        raise RuntimeError(f"B站播放地址获取失败：{data.get('message', 'unknown')}")
    streams = data.get("data", {}).get("durl", [])
    if not streams:
        raise RuntimeError("B站没有返回可下载的视频流")
    stream = streams[0]
    urls = stream.get("backup_url", []) + [stream["url"]]
    return list(dict.fromkeys(urls)), stream.get("size", 0)


def download_video(url, max_size_mb=250, max_attempts=2):
    """Download to a temporary MP4; resume once after a network error."""
    temp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    path = temp.name
    temp.close()
    downloaded = 0
    limit = max_size_mb * 1024 * 1024
    try:
        for attempt in range(max_attempts):
            headers = dict(BILI_HEADERS)
            if downloaded:
                headers["Range"] = f"bytes={downloaded}-"
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    append = downloaded > 0 and response.status == 206
                    if not append:
                        downloaded = 0
                    with open(path, "ab" if append else "wb") as output:
                        while True:
                            chunk = response.read(256 * 1024)
                            if not chunk:
                                return path, downloaded
                            downloaded += len(chunk)
                            if downloaded > limit:
                                raise ValueError(f"视频文件超过 {max_size_mb} MB 限制")
                            output.write(chunk)
            except (socket.timeout, TimeoutError, urllib.error.URLError):
                if attempt + 1 >= max_attempts:
                    raise
                time.sleep(attempt + 1)
    except Exception:
        os.unlink(path)
        raise
    os.unlink(path)
    raise RuntimeError("视频下载未完成")


def resolve_short_url(url):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, new_url):
            return None

    request = urllib.request.Request(url, headers=BILI_HEADERS)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
            return response.url
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return exc.headers.get("Location")
        return None


def extract_bvid(value):
    match = re.search(r"BV[A-Za-z0-9]{10}", value)
    return match.group(0) if match else None


def get_video_duration_seconds(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            check=True, capture_output=True, text=True, timeout=15,
        )
        duration = float(result.stdout.strip())
        return duration if duration > 0 else None
    except (FileNotFoundError, ValueError, subprocess.SubprocessError):
        return None


def select_video_fps(path):
    duration = get_video_duration_seconds(path)
    if duration is None:
        return 2
    if duration <= 120:
        return 3
    if duration <= 600:
        return 2
    return 1
