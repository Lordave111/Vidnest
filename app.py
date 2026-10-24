"""VidNest API: paste a YouTube or Facebook link, pick a quality, download.

Endpoints
  POST /api/info          -> video details + available formats
  POST /api/prepare       -> starts a download job, returns job_id
  GET  /api/status/<id>   -> job status + progress
  GET  /api/file/<id>     -> the finished file (attachment)
  GET  /health            -> liveness check
"""
import glob
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from functools import wraps
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------- config ---
MAX_DURATION = int(os.getenv("MAX_DURATION_SECONDS", "3600"))      # 60 min
MAX_FILESIZE = int(os.getenv("MAX_FILESIZE_MB", "500")) * 1024 * 1024
MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", "1080"))
INFO_LIMIT = int(os.getenv("INFO_LIMIT_PER_HOUR", "60"))           # per IP
DOWNLOAD_LIMIT = int(os.getenv("DOWNLOAD_LIMIT_PER_HOUR", "15"))   # per IP
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_JOBS", "3"))
COOKIES_FILE = os.getenv("YTDLP_COOKIES", "cookies.txt")
JOB_TTL = 10 * 60                                                  # seconds

ALLOWED_ORIGINS = {
    o.strip().rstrip("/")
    for o in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if o.strip()
}
ALLOWED_DOMAINS = ("youtube.com", "youtu.be", "facebook.com", "fb.watch")
FORMAT_RE = re.compile(r"^(mp3|mp4-best|mp4-\d{3,4})$")
WORK_ROOT = os.path.join(tempfile.gettempdir(), "vidnest")


def match_filter(info, incomplete=False):
    """yt-dlp calls this before downloading; returning text skips the video."""
    if info.get("is_live"):
        return "Live streams are not supported"
    duration = info.get("duration")
    if duration and duration > MAX_DURATION:
        return "Video is longer than the allowed limit"
    return None


JOBS = {}
JOBS_LOCK = threading.Lock()
SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)

_hits = defaultdict(deque)
_hits_lock = threading.Lock()


# --------------------------------------------------------------- helpers ---
def rate_limit(bucket, limit):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            now = time.time()
            key = (bucket, request.remote_addr)
            with _hits_lock:
                q = _hits[key]
                while q and now - q[0] > 3600:
                    q.popleft()
                if len(q) >= limit:
                    return jsonify(error="Too many requests. Please try again later."), 429
                q.append(now)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def clean_url(raw):
    """Return a normalised URL if it points at a supported site, else None."""
    raw = (raw or "").strip()
    if not raw or len(raw) > 500:
        return None
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
        return raw
    return None


def friendly_error(exc):
    msg = str(exc).lower()
    if "not a bot" in msg or "429" in msg or "too many requests" in msg:
        return "The video site is temporarily limiting requests from our server. Please try again later."
    if "private" in msg or "sign in" in msg or "log in" in msg or "login" in msg:
        return "This video is private or needs a login, so it can't be downloaded."
    if "age" in msg and "restrict" in msg:
        return "This video is age-restricted and can't be downloaded."
    if "drm" in msg:
        return "This video is DRM-protected and can't be downloaded."
    if any(w in msg for w in ("unavailable", "not available", "removed", "copyright", "blocked")):
        return "This video is unavailable. It may be removed, region-locked or restricted."
    return "We couldn't read this link. Make sure it points to a single public video and try again."


def base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "playlist_items": "1",
        "socket_timeout": 20,
        "retries": 3,
        "extractor_retries": 2,
    }
    if os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def check_info(info):
    if not info or info.get("_type") == "playlist" or info.get("entries"):
        return "Playlist links aren't supported. Paste the link to a single video."
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        return "Live streams can't be downloaded until they have finished."
    duration = info.get("duration")
    if duration and duration > MAX_DURATION:
        return f"This video is longer than the {MAX_DURATION // 60}-minute limit."
    return None


def video_payload(info):
    formats = info.get("formats") or []

    audio_sizes = [
        f.get("filesize") or f.get("filesize_approx") or 0
        for f in formats
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
    ]
    audio_size = max(audio_sizes or [0])

    heights = {}
    for f in formats:
        h = f.get("height")
        if f.get("vcodec") in (None, "none") or not h or h > MAX_HEIGHT or h < 144:
            continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        heights[h] = max(heights.get(h, 0), size)

    options = []
    for h in sorted(heights, reverse=True)[:6]:
        size = heights[h] + audio_size if heights[h] else None
        options.append({"id": f"mp4-{h}", "label": f"{h}p", "hint": "MP4 video", "size": size})
    if not options:
        options.append({"id": "mp4-best", "label": "Best quality", "hint": "MP4 video", "size": None})
    options.append({"id": "mp3", "label": "MP3", "hint": "Audio only", "size": None})

    extractor = (info.get("extractor_key") or "").lower()
    platform = "YouTube" if "youtube" in extractor else "Facebook" if "facebook" in extractor else "Video"

    return {
        "title": info.get("title") or "Untitled video",
        "thumbnail": info.get("thumbnail"),
        "duration": int(info["duration"]) if info.get("duration") else None,
        "uploader": info.get("uploader") or info.get("channel"),
        "platform": platform,
        "formats": options,
    }


def build_opts(fmt, workdir, hook):
    opts = base_opts()
    opts.update(
        outtmpl=os.path.join(workdir, "%(id)s.%(ext)s"),
        max_filesize=MAX_FILESIZE,
        match_filter=match_filter,
        progress_hooks=[hook],
        restrictfilenames=True,
    )
    if fmt == "mp3":
        opts.update(
            format="bestaudio/best",
            postprocessors=[
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ],
        )
    else:
        h = MAX_HEIGHT if fmt == "mp4-best" else min(int(fmt.split("-")[1]), MAX_HEIGHT)
        opts.update(
            format=(
                f"bv*[height<={h}][vcodec^=avc1]+ba[acodec^=mp4a]"
                f"/bv*[height<={h}]+ba/b[height<={h}]/b"
            ),
            merge_output_format="mp4",
        )
    return opts


def safe_name(title, ext):
    name = re.sub(r"[^\w\s\-\.\(\)]", "", title or "", flags=re.UNICODE).strip()
    name = re.sub(r"\s+", " ", name)[:80].strip(" .") or "video"
    return f"{name}{ext}"


def run_job(job, url, fmt):
    def hook(d):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                pct = min(d.get("downloaded_bytes", 0) / total * 90, 90)
                job["progress"] = max(job["progress"], round(pct, 1))
        elif status == "finished":
            job["progress"] = max(job["progress"], 90)

    try:
        with yt_dlp.YoutubeDL(build_opts(fmt, job["dir"], hook)) as ydl:
            info = ydl.extract_info(url, download=True)

        ext = ".mp3" if fmt == "mp3" else ".mp4"
        files = [
            p
            for p in glob.glob(os.path.join(job["dir"], "*"))
            if not p.endswith((".part", ".ytdl", ".temp"))
        ]
        files = [p for p in files if p.endswith(ext)] or files
        if not files:
            raise RuntimeError("no-output")
        path = max(files, key=os.path.getsize)
        if os.path.getsize(path) > MAX_FILESIZE:
            raise RuntimeError("no-output")

        job["file"] = path
        job["name"] = safe_name((info or {}).get("title"), os.path.splitext(path)[1] or ext)
        job["progress"] = 100
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("Job %s failed: %s", job["id"], exc)
        if str(exc) == "no-output":
            job["error"] = (
                f"This video is too long or too large to download here "
                f"(limit {MAX_DURATION // 60} min / {MAX_FILESIZE // 1024 // 1024} MB)."
            )
        else:
            job["error"] = friendly_error(exc)
        job["status"] = "error"
    finally:
        SLOTS.release()


def sweeper():
    while True:
        time.sleep(60)
        now = time.time()
        with JOBS_LOCK:
            stale = [
                jid
                for jid, j in JOBS.items()
                if now - j["created"] > JOB_TTL and j["status"] != "working"
                or now - j["created"] > JOB_TTL * 4
            ]
            for jid in stale:
                job = JOBS.pop(jid)
                shutil.rmtree(job["dir"], ignore_errors=True)
        with _hits_lock:
            for key in list(_hits):
                q = _hits[key]
                while q and now - q[0] > 3600:
                    q.popleft()
                if not q:
                    del _hits[key]


shutil.rmtree(WORK_ROOT, ignore_errors=True)
os.makedirs(WORK_ROOT, exist_ok=True)
threading.Thread(target=sweeper, daemon=True).start()


# ---------------------------------------------------------------- routes ---
@app.after_request
def add_headers(resp):
    origin = request.headers.get("Origin", "").rstrip("/")
    if origin and (origin in ALLOWED_ORIGINS or "*" in ALLOWED_ORIGINS):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "86400"
        resp.headers.add("Vary", "Origin")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    return resp


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.post("/api/info")
@rate_limit("info", INFO_LIMIT)
def api_info():
    data = request.get_json(silent=True) or {}
    url = clean_url(data.get("url"))
    if not url:
        return jsonify(error="Please paste a valid YouTube or Facebook video link."), 400
    try:
        with yt_dlp.YoutubeDL({**base_opts(), "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("Info failed for %s: %s", url, exc)
        return jsonify(error=friendly_error(exc)), 422
    problem = check_info(info)
    if problem:
        return jsonify(error=problem), 422
    return jsonify(video_payload(info))


@app.post("/api/prepare")
@rate_limit("prepare", DOWNLOAD_LIMIT)
def api_prepare():
    data = request.get_json(silent=True) or {}
    url = clean_url(data.get("url"))
    fmt = str(data.get("format", ""))
    if not url or not FORMAT_RE.match(fmt):
        return jsonify(error="Invalid request. Please fetch the video again."), 400
    if not SLOTS.acquire(blocking=False):
        return jsonify(error="We're busy right now. Please try again in a minute."), 503

    job_id = uuid.uuid4().hex
    workdir = os.path.join(WORK_ROOT, job_id)
    os.makedirs(workdir, exist_ok=True)
    job = {
        "id": job_id,
        "dir": workdir,
        "status": "working",
        "progress": 0,
        "error": None,
        "file": None,
        "name": None,
        "created": time.time(),
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    threading.Thread(target=run_job, args=(job, url, fmt), daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/api/status/<job_id>")
def api_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="This download has expired. Please start again."), 404
    return jsonify(status=job["status"], progress=job["progress"], error=job["error"])


@app.get("/api/file/<job_id>")
def api_file(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done" or not job["file"] or not os.path.isfile(job["file"]):
        return jsonify(error="This file has expired. Please start the download again."), 404
    return send_file(job["file"], as_attachment=True, download_name=job["name"])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
