import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import yt_dlp
from structlog import get_logger

from app.browser.pool import BrowserPool
from app.config import settings
from app.errors import InternalException
from app.http_client import fetch_fast

logger = get_logger()

TERMINAL_SSR_STATUSES = {10202, 10221, 10222}


@dataclass
class BrowserPostsResult:
    posts: list[dict[str, Any]]
    ssr_status: int | None
    post_api_responses: int
    post_api_items: int


@dataclass
class YtdlpPostsResult:
    posts: list[dict[str, Any]]
    messages: list[str]


@dataclass
class EmptyResultEvidence:
    ytdlp_messages: list[str]
    browser_api_responses: int = 0
    browser_api_items: int = 0

    def diagnostic(self) -> str:
        if self.ytdlp_messages:
            message = next(
                (
                    item
                    for item in self.ytdlp_messages
                    if "private" in item.lower()
                    or "embedding disabled" in item.lower()
                    or "login" in item.lower()
                    or "unable to extract" in item.lower()
                    or item.startswith("ERROR:")
                ),
                self.ytdlp_messages[0],
            )
            return f"yt-dlp: {' '.join(message.split())}"[:300]
        return (
            f"browser captured {self.browser_api_items} items across "
            f"{self.browser_api_responses} API responses"
        )


def _terminal_status_diagnostic(status: int) -> str:
    labels = {
        10202: "not found",
        10221: "likely banned/restricted",
        10222: "private",
    }
    return f"TikTok status {status} ({labels[status]})"[:300]


def _extract_rehydration_json(html: str) -> dict[str, Any] | None:
    try:
        match = re.search(
            r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not match:
            # Fallback regex if id order is different
            match = re.search(
                r'__UNIVERSAL_DATA_FOR_REHYDRATION__"\s*:\s*({.*?})\s*</script>',
                html,
                re.DOTALL,
            )
        if match:
            content = match.group(1).strip()
            return json.loads(content)

        # Fallback: brace-matching parser for __DEFAULT_SCOPE__
        idx = html.find('"__DEFAULT_SCOPE__":')
        if idx != -1:
            start_idx = html.rfind("{", 0, idx)
            if start_idx != -1:
                depth = 0
                in_string = False
                escape = False
                for i in range(start_idx, len(html)):
                    char = html[i]
                    if escape:
                        escape = False
                        continue
                    if char == "\\":
                        escape = True
                        continue
                    if char == '"':
                        in_string = not in_string
                        continue
                    if not in_string:
                        if char == "{":
                            depth += 1
                        elif char == "}":
                            depth -= 1
                            if depth == 0:
                                return json.loads(html[start_idx : i + 1])
    except Exception as e:
        logger.warning("Failed to parse rehydration JSON", error=str(e))
    return None


def _parse_posts_from_rehydration(
    data: dict[str, Any], target_username: str | None = None
) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    try:
        scope = data.get("__DEFAULT_SCOPE__", {})
        user_detail = scope.get("webapp.user-detail", {})
        item_list = user_detail.get("itemList") or []

        for item in item_list:
            post_id = str(item.get("id", ""))
            if not post_id:
                continue

            # Strict author filter: skip reposts from other creators
            if target_username:
                author = item.get("author", {})
                author_name = (
                    author.get("uniqueId") or author.get("unique_id") or ""
                ).lower()
                if author_name and author_name != target_username.lower():
                    continue

            desc = item.get("desc", "")
            create_time = item.get("createTime")
            video = item.get("video", {})
            video_url = video.get("playAddr") or video.get("downloadAddr")
            cover_url = video.get("cover") or video.get("originCover")

            posts.append(
                {
                    "id": post_id,
                    "desc": desc,
                    "create_time": create_time,
                    "video_url": video_url,
                    "cover_url": cover_url,
                }
            )
    except Exception as e:
        logger.warning("Error extracting posts from data scope", error=str(e))
    return posts


def _parse_live_status_from_rehydration(data: dict[str, Any]) -> dict[str, Any]:
    try:
        scope = data.get("__DEFAULT_SCOPE__", {})
        user_detail = scope.get("webapp.user-detail", {})
        userInfo = user_detail.get("userInfo", {})
        user = userInfo.get("user", {})

        is_live = (
            bool(user.get("roomId")) or user.get("liveRoom", {}).get("status") == 2
        )
        room_id = str(user.get("roomId")) if user.get("roomId") else None
        title = user.get("liveRoom", {}).get("title")

        return {"is_live": is_live, "video_id": room_id, "title": title}
    except Exception:
        return {"is_live": False, "video_id": None, "title": None}


async def get_user_posts_fast(username: str) -> list[dict[str, Any]] | None:
    """Fast path using curl_cffi with TLS impersonation.

    NOTE: As of 2026-08, TikTok's SlardarWAF blocks all curl_cffi requests
    returning a 1.4KB challenge page. This path will return None and fall
    through to the browser path. Kept for future use if WAF bypass improves.
    """
    start = time.monotonic()
    try:
        resp = await fetch_fast(
            f"https://www.tiktok.com/@{username}",
            impersonate="chrome124",
        )
        if resp.status_code != 200:
            return None
        html = resp.text
        # SlardarWAF challenge pages are ~1.4KB
        if len(html) < 50_000:
            logger.debug(
                "TikTok fast-path WAF blocked",
                username=username,
                html_len=len(html),
                duration_ms=round((time.monotonic() - start) * 1000),
            )
            return None
        if "__UNIVERSAL_DATA_FOR_REHYDRATION__" not in html and "__DEFAULT_SCOPE__" not in html:
            return None
        data = _extract_rehydration_json(html)
        if data is None:
            return None
        posts = _parse_posts_from_rehydration(data, target_username=username)
        if not posts:
            return None
        logger.info(
            "TikTok fast-path success",
            username=username,
            posts=len(posts),
            duration_ms=round((time.monotonic() - start) * 1000),
        )
        return posts
    except Exception as e:
        logger.debug(
            "TikTok fast-path failed",
            username=username,
            error=str(e),
            duration_ms=round((time.monotonic() - start) * 1000),
        )
        return None


async def get_user_posts_browser(
    pool: BrowserPool, username: str, session_id: str | None = None
) -> BrowserPostsResult:
    """Browser path using Camoufox pool with API interception + scroll.

    TikTok SSR returns empty itemList (status 209002 for restricted accounts,
    or just empty for all guest views). Real video data comes from XHR calls
    to /api/post/item_list/ which fire when the page loads or on scroll.
    Returns posts plus SSR/API evidence needed by retry and diagnostics.
    """
    start = time.monotonic()
    async with pool.acquire() as page:
        # Inject TikTok session cookie if configured
        session_val = session_id or settings.tiktok_session_id or settings.tiktok_cookie
        if session_val:
            try:
                await page.context.add_cookies(
                    [
                        {
                            "name": "sessionid",
                            "value": session_val,
                            "domain": ".tiktok.com",
                            "path": "/",
                        },
                        {
                            "name": "sessionid_ss",
                            "value": session_val,
                            "domain": ".tiktok.com",
                            "path": "/",
                        },
                        {
                            "name": "sid_tt",
                            "value": session_val,
                            "domain": ".tiktok.com",
                            "path": "/",
                        },
                    ]
                )
            except Exception as e:
                logger.warning("Failed to inject TikTok session cookie", error=str(e))

        captured_posts: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        posts_found = asyncio.Event()
        stats = {
            "post_api_responses": 0,
            "post_api_items": 0,
            "filtered_reposts": 0,
            "duplicate_posts": 0,
        }

        async def handle_response(response):
            url = response.url
            # Intercept user's own post list endpoints only
            if "/api/post/item_list/" not in url and "/api/user/post/" not in url:
                return
            try:
                stats["post_api_responses"] += 1
                res_data = None
                try:
                    res_data = await response.json()
                except Exception:
                    body_bytes = await response.body()
                    if body_bytes:
                        res_data = json.loads(body_bytes.decode("utf-8", errors="ignore"))

                if not res_data or not isinstance(res_data, dict):
                    return

                item_list = (
                    res_data.get("itemList")
                    or res_data.get("itemModule")
                    or []
                )
                if isinstance(item_list, dict):
                    item_list = list(item_list.values())
                stats["post_api_items"] += len(item_list)

                if not item_list:
                    logger.debug(
                        "TikTok post API returned empty itemList",
                        username=username,
                        url=url[:160],
                        status_code=res_data.get("statusCode"),
                        response_keys=list(res_data.keys())[:20],
                    )

                added = 0
                for item in item_list:
                    post_id = str(item.get("id", ""))
                    if not post_id:
                        continue
                    author = item.get("author", {})
                    author_name = (
                        author.get("uniqueId") or author.get("unique_id") or ""
                    ).lower()
                    if author_name and author_name != username.lower():
                        stats["filtered_reposts"] += 1
                        continue
                    if post_id in seen_ids:
                        stats["duplicate_posts"] += 1
                        continue

                    video = item.get("video", {})
                    captured_posts.append(
                        {
                            "id": post_id,
                            "desc": item.get("desc", ""),
                            "create_time": item.get("createTime"),
                            "video_url": video.get("playAddr")
                            or video.get("downloadAddr"),
                            "cover_url": video.get("cover")
                            or video.get("originCover"),
                        }
                    )
                    seen_ids.add(post_id)
                    added += 1
                if added:
                    posts_found.set()
            except Exception as parse_err:
                status_code = getattr(response, "status", None)
                body_sample = ""
                try:
                    body_bytes = await response.body()
                    body_sample = body_bytes.decode("utf-8", errors="ignore")[:200]
                except Exception:
                    pass
                logger.warning(
                    "TikTok post API parse failed",
                    username=username,
                    url=url[:160],
                    status=status_code,
                    body_sample=body_sample,
                    error=str(parse_err),
                )

        page.on("response", handle_response)

        # Block heavy resources to speed up loading
        await page.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in ("image", "media", "font")
                else route.continue_()
            ),
        )

        await page.goto(f"https://www.tiktok.com/@{username}", timeout=25_000)

        # Wait for grid or first real post API response.
        try:
            await page.wait_for_selector(
                '[data-e2e="user-post-item"]', timeout=5_000
            )
        except Exception:
            pass
        try:
            await asyncio.wait_for(posts_found.wait(), timeout=3.0)
        except TimeoutError:
            pass
        posts_found.clear()

        # Instant break if posts already captured on initial load
        if captured_posts:
            logger.info("TikTok posts captured on initial page load", username=username, posts=len(captured_posts))

        # Scroll repeatedly to trigger TikTok pagination. Stop as soon as data is captured.
        else:
            idle_rounds = 0
            last_count = len(captured_posts)
            for scroll_index in range(8):
                try:
                    await page.mouse.wheel(0, 2500)
                    await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(posts_found.wait(), timeout=1.5)
                except TimeoutError:
                    pass
                posts_found.clear()

                if captured_posts:
                    break

                if len(captured_posts) == last_count:
                    idle_rounds += 1
                else:
                    idle_rounds = 0
                last_count = len(captured_posts)

                if idle_rounds >= 2:
                    break

        # Also try SSR data as supplementary source
        html = await page.content()
        data = _extract_rehydration_json(html)
        ssr_posts = _parse_posts_from_rehydration(data, target_username=username) if data else []

        # Merge SSR supplements
        all_posts = list(captured_posts)
        for sp in ssr_posts:
            if sp["id"] not in seen_ids:
                all_posts.append(sp)
                seen_ids.add(sp["id"])

        # Extract SSR status for logging
        ssr_status = None
        if data:
            scope = data.get("__DEFAULT_SCOPE__", {})
            ud = scope.get("webapp.user-detail", {})
            ssr_status = ud.get("statusCode")

        duration = round((time.monotonic() - start) * 1000)
        logger.info(
            "TikTok browser-path done",
            username=username,
            posts=len(all_posts),
            api_captured=len(captured_posts),
            ssr_posts=len(ssr_posts),
            ssr_status=ssr_status,
            post_api_responses=stats["post_api_responses"],
            post_api_items=stats["post_api_items"],
            filtered_reposts=stats["filtered_reposts"],
            duplicate_posts=stats["duplicate_posts"],
            scrolls=scroll_index + 1,
            duration_ms=duration,
        )
        return BrowserPostsResult(
            posts=all_posts,
            ssr_status=ssr_status,
            post_api_responses=stats["post_api_responses"],
            post_api_items=stats["post_api_items"],
        )


class CapturingYtdlpLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        self.messages.append(str(msg))

    def error(self, msg: str) -> None:
        self.messages.append(str(msg))


def _get_user_posts_ytdlp_result(
    username: str, session_id: str | None = None
) -> YtdlpPostsResult:
    """Native yt-dlp flat extraction for TikTok user profiles."""
    start = time.monotonic()
    session_val = session_id or settings.tiktok_session_id or settings.tiktok_cookie
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    if session_val:
        headers["Cookie"] = f"sessionid={session_val}; sessionid_ss={session_val}; sid_tt={session_val}"

    ytdlp_logger = CapturingYtdlpLogger()
    ydl_opts = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "logger": ytdlp_logger,
        "headers": headers,
    }
    if settings.proxy_url:
        ydl_opts["proxy"] = settings.proxy_url
    videos: list[dict[str, Any]] = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        url = f"https://www.tiktok.com/@{username}"
        try:
            info = ydl.extract_info(url, download=False)
            if info:
                entries = info.get("entries") or []
                for entry in entries:
                    if entry and isinstance(entry, dict):
                        video_id = entry.get("id") or entry.get("url", "").split("/")[-1]
                        if video_id and len(video_id) >= 15:
                            videos.append(
                                {
                                    "id": str(video_id),
                                    "desc": entry.get("title")
                                    or entry.get("description")
                                    or "",
                                    "create_time": None,
                                    "video_url": entry.get("url")
                                    or f"https://www.tiktok.com/@{username}/video/{video_id}",
                                    "cover_url": entry.get("thumbnail") or None,
                                }
                            )
        except Exception as err:
            logger.debug(
                "yt-dlp TikTok extraction error", username=username, error=str(err)
            )

    duration = round((time.monotonic() - start) * 1000)
    if videos:
        logger.info(
            "TikTok yt-dlp path success",
            username=username,
            posts=len(videos),
            duration_ms=duration,
        )
        return YtdlpPostsResult(posts=videos, messages=ytdlp_logger.messages)

    logger.info(
        "TikTok yt-dlp path returned 0 posts",
        username=username,
        duration_ms=duration,
        ytdlp_messages=ytdlp_logger.messages[:5],
    )
    return YtdlpPostsResult(posts=videos, messages=ytdlp_logger.messages)


def get_user_posts_ytdlp(
    username: str, session_id: str | None = None
) -> list[dict[str, Any]]:
    return _get_user_posts_ytdlp_result(username, session_id=session_id).posts


async def get_user_posts(
    pool: BrowserPool | None, username: str, session_id: str | None = None
) -> tuple[list[dict[str, Any]], str, str | None]:
    fast_result = await get_user_posts_fast(username)
    if fast_result is not None:
        return fast_result, "fast", None

    ytdlp_result = await asyncio.to_thread(
        _get_user_posts_ytdlp_result, username, session_id=session_id
    )
    if ytdlp_result.posts:
        return ytdlp_result.posts, "ytdlp", None

    if pool is None:
        raise InternalException("Browser pool unavailable and fast-path failed")

    last_result: list[dict[str, Any]] = []
    evidence = EmptyResultEvidence(ytdlp_messages=ytdlp_result.messages)
    for attempt in range(2):
        browser_result = await get_user_posts_browser(
            pool, username, session_id=session_id
        )
        if browser_result.posts:
            if attempt:
                logger.info(
                    "TikTok browser retry recovered posts",
                    username=username,
                    attempt=attempt + 1,
                    posts=len(browser_result.posts),
                )
            return browser_result.posts, "browser", None
        last_result = browser_result.posts
        evidence.browser_api_responses = browser_result.post_api_responses
        evidence.browser_api_items = browser_result.post_api_items
        logger.warning(
            "TikTok browser attempt returned 0 posts",
            username=username,
            attempt=attempt + 1,
            max_attempts=2,
            ssr_status=browser_result.ssr_status,
        )
        if browser_result.ssr_status in TERMINAL_SSR_STATUSES:
            logger.info(
                "TikTok account appears banned/private/not-found — skipping retry",
                username=username,
                ssr_status=browser_result.ssr_status,
            )
            return (
                last_result,
                "browser",
                _terminal_status_diagnostic(browser_result.ssr_status),
            )

    return last_result, "browser", evidence.diagnostic()


async def get_user_live_fast(username: str) -> dict[str, Any] | None:
    start = time.monotonic()
    try:
        resp = await fetch_fast(
            f"https://www.tiktok.com/@{username}/live",
            impersonate="chrome124",
        )
        if resp.status_code != 200:
            return None
        html = resp.text
        if "__UNIVERSAL_DATA_FOR_REHYDRATION__" not in html or len(html) < 50_000:
            return None
        data = _extract_rehydration_json(html)
        if data is None:
            return None
        result = _parse_live_status_from_rehydration(data)
        logger.info(
            "TikTok live fast-path success",
            username=username,
            is_live=result["is_live"],
            duration_ms=round((time.monotonic() - start) * 1000),
        )
        return result
    except Exception as e:
        logger.warning(
            "TikTok fast-path live check failed",
            username=username,
            error=str(e),
            duration_ms=round((time.monotonic() - start) * 1000),
        )
        return None


async def get_user_live_browser(pool: BrowserPool, username: str) -> dict[str, Any]:
    async with pool.acquire() as page:
        await page.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type
                in ("image", "media", "font", "stylesheet")
                else route.continue_()
            ),
        )
        await page.goto(f"https://www.tiktok.com/@{username}/live", timeout=30_000)
        html = await page.content()
        data = _extract_rehydration_json(html)
        if data is None:
            return {"is_live": False, "video_id": None, "title": None}
        return _parse_live_status_from_rehydration(data)


async def get_user_live(
    pool: BrowserPool | None, username: str
) -> tuple[dict[str, Any], str]:
    fast_result = await get_user_live_fast(username)
    if fast_result is not None:
        return fast_result, "fast"
    if pool is None:
        raise InternalException("Browser pool unavailable and fast-path failed")
    browser_result = await get_user_live_browser(pool, username)
    return browser_result, "browser"
