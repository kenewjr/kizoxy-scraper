import asyncio
import sys
import time
from pathlib import Path

# Ensure root workspace directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from structlog import get_logger

from app.browser.pool import BrowserPool
from app.config import settings
from app.http_client import fetch_fast
from app.tiktok.extractor import (
    _extract_rehydration_json,
    _parse_posts_from_rehydration,
    get_user_posts,
)

logger = get_logger()


async def diagnose_fast_path(username: str) -> bool:
    print(f"\n[1/3] Testing Fast Path (curl_cffi TLS impersonation) for @{username}...")
    start = time.monotonic()
    try:
        url = f"https://www.tiktok.com/@{username}"
        resp = await fetch_fast(url, impersonate="chrome124")
        duration = round((time.monotonic() - start) * 1000)
        print(f"      Status: {resp.status_code} ({duration}ms)")

        if resp.status_code != 200:
            print("      [FAIL] Fast path HTTP status non-200")
            return False

        html = resp.text
        html_len = len(html)
        has_rehydration = "__UNIVERSAL_DATA_FOR_REHYDRATION__" in html
        print(
            f"      HTML size: {html_len} bytes, Rehydration tag found: {has_rehydration}"
        )

        if not has_rehydration or html_len < 50_000:
            print("      [FAIL] Fast path response blocked or truncated by TikTok")
            return False

        data = _extract_rehydration_json(html)
        if data is None:
            print("      [FAIL] Failed to parse rehydration JSON")
            return False

        posts = _parse_posts_from_rehydration(data)
        print(f"      [SUCCESS] Extracted {len(posts)} posts via fast path")
        return True

    except Exception as e:
        print(f"      [FAIL] Fast path exception: {e}")
        return False


async def diagnose_browser_path(username: str) -> bool:
    print(f"\n[2/3] Testing Browser Path (Camoufox pool) for @{username}...")
    start = time.monotonic()
    pool = BrowserPool(pool_size=1)
    try:
        await pool.start()
        print("      Browser pool started successfully")

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

            nav_start = time.monotonic()
            await page.goto(f"https://www.tiktok.com/@{username}", timeout=30_000)
            nav_duration = round((time.monotonic() - nav_start) * 1000)

            try:
                await page.wait_for_selector(
                    '[data-e2e="user-post-item"]', timeout=15_000
                )
                print(f"      Selector found in {nav_duration}ms")
            except Exception:
                print(
                    f"      Selector wait timed out after {nav_duration}ms (continuing)"
                )

            html = await page.content()
            data = _extract_rehydration_json(html)
            duration = round((time.monotonic() - start) * 1000)

            if data is None:
                print(
                    f"      [FAIL] Browser path failed to extract rehydration JSON ({duration}ms)"
                )
                await pool.shutdown()
                return False

            posts = _parse_posts_from_rehydration(data)
            print(
                f"      [SUCCESS] Extracted {len(posts)} posts via browser path ({duration}ms)"
            )
            await pool.shutdown()
            return True

    except Exception as e:
        print(f"      [FAIL] Browser path exception: {e}")
        try:
            await pool.shutdown()
        except Exception:
            pass
        return False


async def diagnose_full_pipeline(username: str) -> None:
    print(f"\n[3/3] Testing Full Extraction Pipeline for @{username}...")
    start = time.monotonic()
    pool = BrowserPool(pool_size=1)
    try:
        await pool.start()
        posts, source, diagnostic = await get_user_posts(pool, username)
        duration = round((time.monotonic() - start) * 1000)
        print(
            f"      [SUCCESS] Pipeline returned {len(posts)} posts from source '{source}' in {duration}ms"
        )
        if diagnostic:
            print(f"      Diagnostic: {diagnostic}")
        await pool.shutdown()
    except Exception as e:
        print(f"      [FAIL] Pipeline exception: {e}")
        try:
            await pool.shutdown()
        except Exception:
            pass


import argparse


async def main():
    parser = argparse.ArgumentParser(description="kizoxy-scraper diagnostic tool")
    parser.add_argument("--platform", choices=["tiktok", "youtube"], default="tiktok")
    parser.add_argument("--username", help="TikTok username or YouTube channel ID")
    parser.add_argument("positional_user", nargs="?", help="Positional target username")

    args = parser.parse_args()
    target_username = args.username or args.positional_user or "tiktok"

    print("============================================================")
    print(f"  kizoxy-scraper {args.platform.capitalize()} Diagnostic Tool")
    print(f"  Target User/Channel: @{target_username}")
    print(f"  Proxy Configured   : {settings.proxy_url or 'None (Direct)'}")
    print("============================================================")

    if args.platform == "tiktok":
        fast_ok = await diagnose_fast_path(target_username)
        browser_ok = await diagnose_browser_path(target_username)
        await diagnose_full_pipeline(target_username)

        print("\n============================================================")
        print("  DIAGNOSTIC SUMMARY")
        print(f"  Fast Path    : {'PASS' if fast_ok else 'BLOCKED/FAIL'}")
        print(f"  Browser Path : {'PASS' if browser_ok else 'FAIL'}")
        print("============================================================\n")
    else:
        print("  YouTube native yt-dlp diagnostic...")
        from app.youtube.extractor import get_channel_videos

        start = time.monotonic()
        try:
            vids = await get_channel_videos(target_username, limit=3)
            dur = round((time.monotonic() - start) * 1000)
            print(f"  [SUCCESS] YouTube extracted {len(vids)} videos in {dur}ms")
        except Exception as e:
            print(f"  [FAIL] YouTube exception: {e}")


if __name__ == "__main__":
    asyncio.run(main())
