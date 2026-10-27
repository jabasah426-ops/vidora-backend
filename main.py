import ipaddress
import os
import re
import socket
import tempfile
import shutil
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, HttpUrl

app = FastAPI(title="Vidora Backend", version="1.0.0")
# Public frontend only. The API has no user accounts or private data.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Range"],
    expose_headers=["Content-Length", "Content-Disposition"],
)

class InfoRequest(BaseModel):
    url: HttpUrl


def checked_url(raw: str) -> str:
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Please provide a valid public http/https video URL.")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        raise HTTPException(status_code=400, detail="Local or private network URLs are not supported.")
    try:
        records = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        addresses = {ipaddress.ip_address(row[4][0].split("%", 1)[0]) for row in records}
    except (socket.gaierror, ValueError, OSError):
        raise HTTPException(status_code=400, detail="The link host could not be resolved.")
    if not addresses or any(not ip.is_global for ip in addresses):
        raise HTTPException(status_code=400, detail="Private or non-public network addresses are not supported.")
    return raw


def ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 1,
        "fragment_retries": 1,
        "ignoreerrors": False,
        "nocheckcertificate": False,
        "geo_bypass": False,
    }
    if extra:
        opts.update(extra)
    return opts


def friendly_error(exc: Exception) -> str:
    message = str(exc)
    if "Unsupported URL" in message:
        return "This website or link type is not supported by the downloader."
    if any(word in message.lower() for word in ("private video", "login", "sign in", "authentication", "members-only")):
        return "This video requires login or special access. Vidora only supports publicly accessible content."
    if "DRM" in message.upper():
        return "This video appears to use DRM protection and cannot be downloaded by Vidora."
    if "HTTP Error 404" in message:
        return "The video was not found. Check that the link is correct and still available."
    if "HTTP Error 403" in message or "Forbidden" in message:
        return "The website refused the request. This platform may block automated downloads."
    return "Could not process this link. It may be private, unsupported, restricted, or temporarily unavailable."


@app.get("/")
def root():
    return {"service": "Vidora Backend", "status": "running", "health": "/api/health"}

@app.get("/api/health")
def health():
    return {"ok": True, "service": "Vidora Backend", "version": "1.0.0"}

@app.post("/api/info")
def info(body: InfoRequest):
    url = checked_url(str(body.url))
    try:
        with yt_dlp.YoutubeDL(ydl_opts({"skip_download": True, "extract_flat": False})) as ydl:
            data = ydl.extract_info(url, download=False)
        if not data:
            raise ValueError("No video information returned")
        title = data.get("title") or "Video"
        duration = data.get("duration")
        duration_text = f"{int(duration)//60}:{int(duration)%60:02d}" if duration else ""
        formats = [
            {"id": "best", "label": "Best quality", "ext": "mp4", "size": ""},
            {"id": "720", "label": "HD 720p", "ext": "mp4", "size": ""},
            {"id": "480", "label": "480p", "ext": "mp4", "size": ""},
            {"id": "360", "label": "360p", "ext": "mp4", "size": ""},
            {"id": "mp3", "label": "Audio only", "ext": "mp3", "size": ""},
        ]
        return {
            "title": title,
            "platform": data.get("extractor_key") or data.get("extractor") or "Video",
            "duration": duration_text,
            "thumbnail": data.get("thumbnail") or "",
            "preview_url": "",
            "formats": formats,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=friendly_error(exc))

@app.get("/api/download")
def download(url: str = Query(...), q: str = Query("best"), title: str = Query("video")):
    url = checked_url(url)
    if q not in {"best", "720", "480", "360", "mp3"}:
        raise HTTPException(status_code=400, detail="Unsupported format selection.")
    safe_title = re.sub(r"[^A-Za-z0-9 _-]", "", title).strip()[:70] or "video"
    temp_dir = tempfile.mkdtemp(prefix="vidora-")
    output_template = str(Path(temp_dir) / "%(title).70s.%(ext)s")
    if q == "mp3":
        fmt = "bestaudio/best"
        opts = {"format": fmt, "outtmpl": output_template, "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]}
        expected_ext = ".mp3"
        media_type = "audio/mpeg"
    else:
        fmt = "bestvideo[height<={0}]+bestaudio/best[height<={0}]/best[height<={0}]/best".format(q) if q != "best" else "bestvideo+bestaudio/best"
        opts = {"format": fmt, "outtmpl": output_template, "merge_output_format": "mp4"}
        expected_ext = ".mp4"
        media_type = "video/mp4"
    try:
        with yt_dlp.YoutubeDL(ydl_opts(opts)) as ydl:
            ydl.download([url])
        files = [p for p in Path(temp_dir).iterdir() if p.is_file() and p.suffix.lower() in (".mp4", ".m4v", ".webm", ".mkv", ".mp3", ".m4a", ".opus")]
        if not files:
            raise RuntimeError("The server did not produce a downloadable file.")
        # Prefer requested extension, but allow the actual output if a source only supplies another container.
        file_path = next((p for p in files if p.suffix.lower() == expected_ext), files[0])
        media_type = "audio/mpeg" if file_path.suffix.lower() == ".mp3" else "video/mp4" if file_path.suffix.lower() in (".mp4", ".m4v") else "application/octet-stream"
        return FileResponse(
            path=str(file_path),
            media_type=media_type,
            filename=f"{safe_title}{file_path.suffix.lower()}",
            background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
        )
    except HTTPException:
        raise
    except Exception as exc:
        # Return a concise error to the frontend; don't expose stack traces.
        raise HTTPException(status_code=422, detail=friendly_error(exc))

@app.exception_handler(HTTPException)
async def http_error_handler(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
