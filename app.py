
import json
import os
import re
import sys
import tempfile
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.background import BackgroundTask
from pydantic import BaseModel, Field

app = FastAPI(title="Video Downloader Backend")

MAX_DURATION = 900
MAX_FILE_SIZE = 500 * 1024 * 1024
TIMEOUT = 240

PLATFORMS = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "youtube-nocookie.com": "YouTube",
    "facebook.com": "Facebook",
    "fb.com": "Facebook",
    "fb.watch": "Facebook",
    "tiktok.com": "TikTok",
}


def identify_platform(value):
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")

        if parsed.scheme != "https":
            return None

        if parsed.username or parsed.password:
            return None

        if not host or re.fullmatch(
            r"\d{1,3}(?:\.\d{1,3}){3}", host
        ):
            return None

        for domain, name in PLATFORMS.items():
            if host == domain or host.endswith("." + domain):
                return name

        return None
    except Exception:
        return None


def run_ytdlp(args, timeout=TIMEOUT):
    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="Proses terlalu lama. Coba lagi nanti."
        )

    if result.returncode != 0:
        # Jangan tampilkan detail internal server kepada pengguna.
        raise HTTPException(
            status_code=422,
            detail="Video tidak bisa diproses. Periksa tautan atau coba lagi."
        )

    return result.stdout


def inspect_video(url):
    run_ytdlp([
        "--no-warnings",
        "--no-playlist",
        "--skip-download",
        "--dump-single-json",
        "--socket-timeout", "20",
        "--extractor-retries", "1",
        url,
    ])

    # Ambil metadata dengan satu proses JSON yang dapat diperiksa.
    raw = run_ytdlp([
        "--no-warnings",
        "--no-playlist",
        "--skip-download",
        "--dump-single-json",
        "--socket-timeout", "20",
        "--extractor-retries", "1",
        url,
    ])

    try:
        info = json.loads(raw)
    except Exception:
        raise HTTPException(
            status_code=422,
            detail="Metadata video tidak dapat dibaca."
        )

    duration = info.get("duration")

    if duration is not None and duration > MAX_DURATION:
        raise HTTPException(
            status_code=413,
            detail="Durasi video dibatasi maksimal 15 menit."
        )

    return info


class InspectRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "service": "Video Downloader Backend",
        "status": "online",
    }


@app.post("/api/inspect")
def inspect(request: InspectRequest):
    platform = identify_platform(request.url)

    if not platform:
        raise HTTPException(
            status_code=400,
            detail="Platform tidak didukung atau URL tidak valid."
        )

    info = inspect_video(request.url)

    return {
        "ok": True,
        "platform": platform,
        "title": info.get("title", "Video"),
        "duration": info.get("duration"),
        "downloadReady": True,
        "message": "Video berhasil diperiksa."
    }


@app.get("/api/download")
def download(url: str = Query(min_length=8, max_length=2048)):
    platform = identify_platform(url)

    if not platform:
        raise HTTPException(
            status_code=400,
            detail="Platform tidak didukung atau URL tidak valid."
        )

    # Periksa metadata sebelum mengunduh.
    info = inspect_video(url)
    duration = info.get("duration")

    if duration is not None and duration > MAX_DURATION:
        raise HTTPException(
            status_code=413,
            detail="Durasi video dibatasi maksimal 15 menit."
        )

    with tempfile.TemporaryDirectory(prefix="video-dl-") as temp:
        output = str(Path(temp) / "video.%(ext)s")

        run_ytdlp([
            "--no-warnings",
            "--no-playlist",
            "--no-part",
            "--max-filesize", str(MAX_FILE_SIZE),
            "--socket-timeout", "20",
            "--retries", "1",
            "--extractor-retries", "1",
            "-f", "best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", output,
            url,
        ], timeout=TIMEOUT)

        files = [
            p for p in Path(temp).glob("video.*")
            if p.is_file()
        ]

        if not files:
            raise HTTPException(
                status_code=500,
                detail="File hasil unduhan tidak ditemukan."
            )

        file_path = files[0]

        if file_path.stat().st_size > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail="Ukuran video melebihi batas 500 MB."
            )

        # FileResponse perlu file tetap ada sampai transfer selesai.
        # Pindahkan file ke direktori sementara di luar blok ini.
        persistent_temp = tempfile.mkdtemp(prefix="video-result-")
        final_path = Path(persistent_temp) / file_path.name
        final_path.write_bytes(file_path.read_bytes())

    def cleanup():
        try:
            final_path.unlink(missing_ok=True)
            final_path.parent.rmdir()
        except OSError:
            pass

    safe_name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        info.get("title") or "video"
    )[:100]

    suffix = final_path.suffix or ".mp4"

    return FileResponse(
        path=str(final_path),
        filename=safe_name + suffix,
        media_type="application/octet-stream",
        background=BackgroundTask(cleanup),
    )
