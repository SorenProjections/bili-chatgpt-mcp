"""Personal, tool-only MCP bridge from Bilibili videos to Xiaomi MiMo."""

import base64
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import bili_video


API_URL = "https://api.xiaomimimo.com/v1/chat/completions"
MODEL = "mimo-v2.6-flash"
PROMPT_VERSION = 4
ANALYSIS_PROMPT = """你是视频逐段审阅工具。请用中文尽可能完整、准确地还原这一片段的画面和声音，供没有看过视频的人理解。不要自行限制篇幅，也不要为了拉长篇幅而重复。
1. 首先输出带时间戳的【画面与声音时间线】，从片段开始到结束连续覆盖，至少每 30 秒一条；字幕、人物动作或镜头快速变化时加密。不要只给逐字稿。重复镜头也要交代其出现的时间，但可以说明它与之前镜头相同，避免复制同一段文字。
2. 每条写清可确认的场景、人物或物体、动作、镜头变化、屏幕文字，以及能听清的对白、旁白和其他重要声音。优先完整保留讲述内容、数字、专有名词与关键论证步骤。
3. 区分【确认听到】、【屏幕字幕】和【推测】。无法辨认时标明时间和原因，不能补写。歌曲可以描述主题、结构和画面配合，不要反复抄写相同副歌。
4. 时间线之后，整理视频中提出的重要事实、观点与论证关系、屏幕文字清单，以及听不清或看不清之处。不要依据训练截止日期猜测视频中提到的近期名称、事件或产品不存在；无法独立核实的外部事实只标为待核实。总结不能代替时间线正文。
5. 覆盖整段，尤其不要在片头花完输出空间而省略后半段。"""
VISUAL_PROMPT = """请只分析这段视频的画面与屏幕文字，不要转录旁白或歌曲。用原视频的绝对时间戳编写连续的视觉时间线，至少每 30 秒一条，覆盖片段开头、中间和结尾；画面变化快时增加条目。每条描述可确认的场景、人物动作、镜头、图表、字幕与数字。不要凭声音推测画面，也不要省略最后 30 秒。"""
DB_PATH = Path(os.environ.get("BILI_MCP_DB", str(Path.home() / ".local/share/bili-chatgpt-mcp/jobs.sqlite3")))
MAX_DOWNLOAD_MB = 250
MAX_ENCODED_VIDEO_MB = 50
MAX_VIDEO_MINUTES = 60
CHUNK_SECONDS = 300
MAX_SPLIT_DEPTH = 2
MAX_POLL_SECONDS = 35
MAX_JOBS_PER_DAY = 60
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bili-mimo")
db_lock = threading.RLock()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bili-chatgpt-mcp")

mcp = FastMCP(
    "Bilibili Video Understanding",
    instructions=(
        "提交视频后保存 job_id。读取工具会等待进度，勿在几秒内反复轮询；"
        "若仍在分析，告诉用户任务在后台运行，可稍后凭 job_id 继续。"
        "状态 partial 表示只有部分时间段完成，必须明确说明未覆盖的范围。"
    ),
    host="127.0.0.1",
    port=int(os.environ.get("BILI_MCP_PORT", "18791")),
    stateless_http=True,
    json_response=True,
)


def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    with db_lock, connect_db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, bvid TEXT NOT NULL, status TEXT NOT NULL,
            info_json TEXT NOT NULL, result TEXT, error TEXT,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            parts_done INTEGER NOT NULL DEFAULT 0,
            parts_total INTEGER NOT NULL DEFAULT 0,
            model TEXT NOT NULL DEFAULT 'mimo-v2.5',
            prompt_version INTEGER NOT NULL DEFAULT 1
        )""")
        columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
        for name in ("parts_done", "parts_total"):
            if name not in columns:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
        if "model" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN model TEXT NOT NULL DEFAULT 'mimo-v2.5'")
        if "prompt_version" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN prompt_version INTEGER NOT NULL DEFAULT 1")
        con.execute("CREATE INDEX IF NOT EXISTS jobs_bvid ON jobs(bvid, created_at)")
        con.execute("UPDATE jobs SET status='failed', error='服务器重启，请重新提交视频', updated_at=? WHERE status IN ('queued','downloading','analyzing')", (int(time.time()),))
    DB_PATH.parent.chmod(0o700)
    DB_PATH.chmod(0o600)


def read_job(job_id):
    with db_lock, connect_db() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def update_job(job_id, status, result=None, error=None, parts_total=None):
    with db_lock, connect_db() as con:
        con.execute(
            "UPDATE jobs SET status=?, result=COALESCE(?,result), error=?, parts_total=COALESCE(?,parts_total), updated_at=? WHERE id=?",
            (status, result, error, parts_total, int(time.time()), job_id),
        )


def append_part(job_id, part, parts_done):
    with db_lock, connect_db() as con:
        con.execute(
            "UPDATE jobs SET result=COALESCE(result,'')||?, parts_done=?, updated_at=? WHERE id=?",
            (part, parts_done, int(time.time()), job_id),
        )


def parse_bvid(value):
    value = value.strip()
    if re.fullmatch(r"BV[A-Za-z0-9]{10}", value):
        return value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("请提供 B 站 BV 号或 B 站视频链接")
    host = (parsed.hostname or "").lower()
    if host in ("b23.tv", "www.b23.tv", "bili2233.cn"):
        resolved = bili_video.resolve_short_url(value)
        if not resolved:
            raise ValueError("B 站短链接无法解析")
        parsed = urllib.parse.urlsplit(resolved)
        host = (parsed.hostname or "").lower()
    if host != "bilibili.com" and not host.endswith(".bilibili.com"):
        raise ValueError("仅支持 B 站链接，不能读取其他网站")
    bvid = bili_video.extract_bvid(parsed.path)
    if not bvid:
        raise ValueError("链接中没有找到 BV 号")
    return bvid


def download(info):
    urls, declared_size = bili_video.get_video_urls(info["bvid"], info["cid"])
    if declared_size > MAX_DOWNLOAD_MB * 1024 * 1024:
        raise ValueError(f"视频文件超过 {MAX_DOWNLOAD_MB} MB 的个人服务限制")
    errors = []
    for url in urls:
        try:
            path, _ = bili_video.download_video(url, max_size_mb=MAX_DOWNLOAD_MB)
            return path
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("所有 B 站视频源均下载失败：" + "; ".join(errors[:3]))


def encode_chunk(source, start, seconds, output):
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(start), "-i", source, "-t", str(seconds),
        "-vf", "scale=960:-2", "-c:v", "libx264", "-preset", "ultrafast",
        "-b:v", "500k", "-maxrate", "600k", "-bufsize", "1200k",
        "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", output,
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=900)
    if not Path(output).is_file() or Path(output).stat().st_size == 0:
        raise RuntimeError("视频转码没有生成有效文件")


class IncompleteAnalysisError(RuntimeError):
    """MiMo ended before producing a complete answer."""

    def __init__(self, reason, content_chars):
        self.reason = reason
        self.content_chars = content_chars
        super().__init__(
            "MiMo API 未返回完整视频分析正文"
            f" (finish_reason={reason}, content_chars={content_chars})"
        )


def call_mimo(video_path, prompt, media_resolution="default"):
    key = os.environ.get("MIMO_API_KEY")
    if not key:
        raise RuntimeError("MiMo API 密钥未配置")
    raw = Path(video_path).read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    if len(encoded) > MAX_ENCODED_VIDEO_MB * 1000 * 1000:
        raise ValueError("视频片段编码后超过 MiMo 的 50 MB 限制")
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + encoded},
             "fps": bili_video.select_video_fps(video_path),
             "media_resolution": media_resolution},
            {"type": "text", "text": prompt},
        ]}],
        "max_completion_tokens": 24000,
        "thinking": {"type": "disabled"},
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(API_URL, data=payload, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key,
    })
    try:
        with urllib.request.urlopen(req, timeout=900) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", "replace")
        raise RuntimeError(f"MiMo API HTTP {exc.code}: {detail}") from exc
    choice = data.get("choices", [{}])[0]
    content = choice.get("message", {}).get("content")
    if isinstance(content, list):
        content = "\n".join(str(item.get("text", "")) for item in content)
    if choice.get("finish_reason") != "stop":
        raise IncompleteAnalysisError(choice.get("finish_reason"), len(str(content or "")))
    if not content or len(str(content).strip()) < 200:
        raise RuntimeError("MiMo API 没有返回有效的视频分析正文")
    return str(content)


def format_time(seconds):
    value = int(seconds)
    return f"{value // 60:02d}:{value % 60:02d}"


def has_timeline_coverage(content, start, seconds):
    if seconds < 90:
        return True
    timestamps = [
        int(minutes) * 60 + int(secs)
        for minutes, secs in re.findall(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", content)
    ]
    within = [value for value in timestamps if start <= value <= start + seconds + 5]
    return len(within) >= 3 and max(within) >= start + seconds - 45


def analyze_range(source, start, seconds, directory, depth=0):
    piece = str(Path(directory) / f"clip_{uuid.uuid4().hex}.mp4")
    encode_chunk(source, start, seconds, piece)
    prompt = (
        f"这是完整视频中原时间 {format_time(start)} 至 {format_time(start + seconds)} 的片段。"
        "请用原视频的绝对时间标记，不要把这一片段误当作完整视频。\n\n"
        + ANALYSIS_PROMPT
    )
    try:
        answer = call_mimo(piece, prompt)
        if not has_timeline_coverage(answer, start, seconds):
            log.warning("Visual timeline missing for %s-%s; requesting visual pass", format_time(start), format_time(start + seconds))
            visual_prompt = (
                f"这是原视频 {format_time(start)} 至 {format_time(start + seconds)} 的片段。\n\n"
                + VISUAL_PROMPT
            )
            try:
                visual = call_mimo(piece, visual_prompt, media_resolution="max")
                answer = "### 画面与屏幕文字\n\n" + visual + "\n\n### 声音与讲述补充\n\n" + answer
                if not has_timeline_coverage(visual, start, seconds):
                    answer = "[完整性提示：模型仍未提供可靠的画面时间线，以下内容不能视为完整画面解读。]\n\n" + answer
            except IncompleteAnalysisError as visual_exc:
                log.warning("Visual pass unavailable for %s-%s: %s", format_time(start), format_time(start + seconds), visual_exc)
                answer = "[完整性提示：模型未提供可靠的画面时间线；以下主要是声音与讲述内容。]\n\n" + answer
        return answer
    except IncompleteAnalysisError as exc:
        if exc.reason not in ("length", "repetition_truncation") or depth >= MAX_SPLIT_DEPTH or seconds <= 75:
            raise
        half = seconds / 2
        log.warning("Retrying truncated segment %s-%s in halves: %s", format_time(start), format_time(start + seconds), exc)
        left = analyze_range(source, start, half, directory, depth + 1)
        right = analyze_range(source, start + half, seconds - half, directory, depth + 1)
        return (
            f"### {format_time(start)}–{format_time(start + half)}\n\n{left}\n\n"
            f"### {format_time(start + half)}–{format_time(start + seconds)}\n\n{right}"
        )


def process_job(job_id, info):
    path = None
    try:
        update_job(job_id, "downloading")
        path = download(info)
        duration = bili_video.get_video_duration_seconds(path) or info["duration"]
        if duration > MAX_VIDEO_MINUTES * 60:
            raise ValueError(f"视频超过 {MAX_VIDEO_MINUTES} 分钟的个人服务限制")
        update_job(job_id, "analyzing")
        with tempfile.TemporaryDirectory(prefix="bili_mcp_") as directory:
            count = max(1, math.ceil(duration / CHUNK_SECONDS))
            update_job(job_id, "analyzing", parts_total=count)
            for index in range(count):
                start = index * CHUNK_SECONDS
                seconds = min(CHUNK_SECONDS, duration - start)
                part = (
                    f"## 第 {index + 1}/{count} 段（原视频 {format_time(start)} 起）\n\n"
                    + analyze_range(path, start, seconds, directory) + "\n\n"
                )
                append_part(job_id, part, index + 1)
                log.info("Job part completed: %s %s/%s", job_id, index + 1, count)
        update_job(job_id, "completed")
        log.info("Job completed: %s %s", job_id, info["bvid"])
    except Exception as exc:
        row = read_job(job_id)
        status = "partial" if row and row.get("parts_done", 0) > 0 else "failed"
        update_job(job_id, status, error=str(exc)[:2000])
        log.exception("Job %s: %s", status, job_id)
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


@mcp.tool(
    title="分析 B 站视频",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
)
def analyze_bilibili_video(url: str) -> dict:
    """Use this when the user asks to understand or transcribe a Bilibili video. Submit a BV number or Bilibili video link; returns a job ID. Call get_bilibili_analysis with that ID until complete, then read every result segment before answering. Video audio and visuals are analyzed by Xiaomi MiMo V2.6 Flash."""
    try:
        bvid = parse_bvid(url)
        info = bili_video.get_video_info(bvid)
        if info["duration"] > MAX_VIDEO_MINUTES * 60:
            return {"status": "error", "error": f"视频超过 {MAX_VIDEO_MINUTES} 分钟限制"}
        with db_lock, connect_db() as con:
            previous = con.execute(
                "SELECT id,status FROM jobs WHERE bvid=? AND model=? AND prompt_version=? AND status IN ('queued','downloading','analyzing','completed','partial') ORDER BY created_at DESC LIMIT 1",
                (bvid, MODEL, PROMPT_VERSION),
            ).fetchone()
            if previous:
                return {"status": previous["status"], "job_id": previous["id"], "video": info, "model": MODEL, "cached": True}
            today = int(time.time()) - 86400
            count = con.execute("SELECT COUNT(*) FROM jobs WHERE created_at>=?", (today,)).fetchone()[0]
            if count >= MAX_JOBS_PER_DAY:
                return {"status": "error", "error": "过去 24 小时的分析任务已达到 60 个，请稍后再试"}
            job_id = uuid.uuid4().hex
            now = int(time.time())
            con.execute(
                "INSERT INTO jobs (id,bvid,status,info_json,model,prompt_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (job_id, bvid, "queued", json.dumps(info, ensure_ascii=False), MODEL, PROMPT_VERSION, now, now),
            )
        executor.submit(process_job, job_id, info)
        log.info("MCP job submitted: %s %s model=%s prompt=%s", job_id, bvid, MODEL, PROMPT_VERSION)
        return {"status": "queued", "job_id": job_id, "video": info, "model": MODEL,
                "next_step": "调用 get_bilibili_analysis(job_id) 查询；若仍在分析，请稍后再查。"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:1000]}


@mcp.tool(
    title="读取 B 站视频分析",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
)
def get_bilibili_analysis(job_id: str, offset: int = 0, max_chars: int = 12000, wait_seconds: int = 35) -> dict:
    """Use this after analyze_bilibili_video. Wait up to 35 seconds for new progress, then read the analysis using next_offset until has_more is false. If still analyzing, tell the user the job continues in the background; do not rapidly repeat calls. For partial status, state the missing video time range clearly."""
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return {"status": "error", "error": "无效的任务编号"}
    row = read_job(job_id)
    if not row:
        return {"status": "error", "error": "任务不存在"}
    offset = max(0, int(offset))
    wait_seconds = max(0, min(int(wait_seconds), MAX_POLL_SECONDS))
    if row["status"] in ("queued", "downloading", "analyzing") and len(row["result"] or "") <= offset:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1, remaining))
            row = read_job(job_id)
            if row["status"] not in ("queued", "downloading", "analyzing") or len(row["result"] or "") > offset:
                break
    output = {"status": row["status"], "job_id": job_id, "video": json.loads(row["info_json"]), "model": row["model"]}
    output["parts_done"] = row.get("parts_done", 0)
    output["parts_total"] = row.get("parts_total", 0)
    if "[完整性提示：" in (row["result"] or ""):
        output["quality_warning"] = "部分片段缺少可靠的画面时间线；请将对应内容说明为声音或讲述分析，不要称为完整的画面解读。"
    if row["status"] in ("failed", "partial"):
        output["error"] = row["error"]
    if row["status"] == "partial":
        covered_until = min(row.get("parts_done", 0) * CHUNK_SECONDS, output["video"].get("duration", 0))
        output["covered_until"] = format_time(covered_until)
        output["missing_from"] = format_time(covered_until)
        output["next_step"] = "仅前面时间段完成，后续被上游模型拒绝或处理失败。请明确告知用户缺失范围，不要把已有内容当作全片分析。"
    if row["result"]:
        content = row["result"] or ""
        offset = max(0, min(int(offset), len(content)))
        max_chars = max(1000, min(int(max_chars), 20000))
        piece = content[offset:offset + max_chars]
        next_offset = offset + len(piece)
        output.update({"analysis": piece, "offset": offset, "next_offset": next_offset,
                       "total_chars": len(content), "has_more": next_offset < len(content),
                       "is_final": row["status"] in ("completed", "partial", "failed")})
        if row["status"] == "analyzing":
            output["next_step"] = "已返回目前完成的部分。后续片段仍在分析，稍后用 next_offset 再次查询。"
    elif row["status"] not in ("completed", "failed"):
        output["next_step"] = "视频仍在后台分析。请告诉用户保留任务编号，稍后继续；不要在几秒内反复查询。"
        output["retry_after_seconds"] = MAX_POLL_SECONDS
    log.info("MCP job read: %s status=%s offset=%s next=%s parts=%s/%s", job_id, row["status"], offset, output.get("next_offset"), output["parts_done"], output["parts_total"])
    return output


if __name__ == "__main__":
    init_db()
    mcp.run(transport="streamable-http")
