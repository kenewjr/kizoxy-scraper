from typing import Any

import yt_dlp

from app.config import settings
from app.errors import (
    BlockedException,
    InternalException,
    NotFoundException,
    TimeoutException,
)


def _get_ydl_opts(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
    }
    if settings.proxy_url:
        opts["proxy"] = settings.proxy_url
    if extra:
        opts.update(extra)
    return opts


def _build_youtube_url(target: str, path_suffix: str = "videos") -> str:
    target = target.strip()
    if target.startswith(("http://", "https://")):
        return target
    if target.startswith("@"):
        return f"https://www.youtube.com/{target}/{path_suffix}"
    if target.startswith("UC"):
        return f"https://www.youtube.com/channel/{target}/{path_suffix}"
    return f"https://www.youtube.com/{target}/{path_suffix}"


def _wrap_yt_dlp_error(e: yt_dlp.utils.DownloadError) -> Exception:
    """Map yt-dlp errors to typed ScraperExceptions."""
    msg = str(e).lower()
    if "404" in msg or "not found" in msg or "does not exist" in msg:
        return NotFoundException(str(e))
    if "403" in msg or "blocked" in msg or "sign in" in msg:
        return BlockedException(str(e))
    if "timed out" in msg or "timeout" in msg:
        return TimeoutException(str(e))
    return InternalException(str(e))


def get_channel_latest_videos(channel_id: str, limit: int = 5) -> list[dict[str, Any]]:
    ydl_opts = _get_ydl_opts(
        {
            "extract_flat": "in_playlist",
            "playlistend": limit,
        }
    )
    url = _build_youtube_url(channel_id, "videos")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get("entries", []) if info else []
            return [
                {
                    "id": str(entry.get("id", "")),
                    "title": entry.get("title"),
                    "upload_date": entry.get("upload_date"),
                    "url": entry.get("url")
                    or (
                        f"https://www.youtube.com/watch?v={entry.get('id')}"
                        if entry.get("id")
                        else None
                    ),
                }
                for entry in entries
                if entry
            ]
    except yt_dlp.utils.DownloadError as e:
        raise _wrap_yt_dlp_error(e) from e


def check_channel_live(channel_id: str) -> dict[str, Any]:
    url = _build_youtube_url(channel_id, "live")

    ydl_opts = _get_ydl_opts({"skip_download": True})
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return {"is_live": False, "video_id": None, "title": None}
            return {
                "is_live": bool(info.get("is_live")),
                "video_id": info.get("id"),
                "title": info.get("title"),
            }
    except yt_dlp.utils.DownloadError as e:
        raise _wrap_yt_dlp_error(e) from e
