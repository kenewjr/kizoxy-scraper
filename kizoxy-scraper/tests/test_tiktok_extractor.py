from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.tiktok.extractor import (
    TERMINAL_SSR_STATUSES,
    BrowserPostsResult,
    EmptyResultEvidence,
    YtdlpPostsResult,
    _extract_rehydration_json,
    get_user_posts,
    get_user_posts_browser,
    get_user_posts_fast,
    get_user_posts_ytdlp,
)


class FakePool:
    def __init__(self, page):
        self.page = page

    @asynccontextmanager
    async def acquire(self):
        yield self.page


def make_browser_page(response=None, ssr_status=None):
    page = MagicMock()
    response_handler = None

    def register_handler(event, handler):
        nonlocal response_handler
        if event == "response":
            response_handler = handler

    async def goto(*args, **kwargs):
        if response is not None:
            assert response_handler is not None
            await response_handler(response)

    data = None
    if ssr_status is not None:
        data = {
            "__DEFAULT_SCOPE__": {
                "webapp.user-detail": {
                    "statusCode": ssr_status,
                    "itemList": [],
                }
            }
        }
    html = "<html></html>"
    if data is not None:
        import json

        html = (
            '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
            f"{json.dumps(data)}</script>"
        )

    page.on.side_effect = register_handler
    page.route = AsyncMock()
    page.goto = AsyncMock(side_effect=goto)
    page.wait_for_selector = AsyncMock(side_effect=TimeoutError)
    page.content = AsyncMock(return_value=html)
    page.mouse.wheel = AsyncMock()
    page.evaluate = AsyncMock()
    return page


def test_extract_rehydration_json():
    html = """
    <html>
      <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">
        {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"itemList": [{"id": "789", "desc": "Hello TikTok"}]}}}
      </script>
    </html>
    """
    data = _extract_rehydration_json(html)
    assert data is not None
    assert "__DEFAULT_SCOPE__" in data


@pytest.mark.asyncio
async def test_get_user_posts_fast_blocked_or_short():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html>Short blocked page</html>"

    with patch(
        "app.tiktok.extractor.fetch_fast",
        new_callable=AsyncMock,
        return_value=mock_resp,
    ):
        res = await get_user_posts_fast("testuser")
        assert res is None


@pytest.mark.asyncio
async def test_get_user_posts_fast_valid():
    valid_json = '{"__DEFAULT_SCOPE__": {"webapp.user-detail": {"itemList": [{"id": "111", "desc": "Post 1"}]}}}'
    valid_html = (
        '<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        + valid_json
        + "</script>"
        + ("x" * 50_000)
        + "</html>"
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = valid_html

    with patch(
        "app.tiktok.extractor.fetch_fast",
        new_callable=AsyncMock,
        return_value=mock_resp,
    ):
        res = await get_user_posts_fast("testuser")
        assert res is not None
        assert len(res) == 1
        assert res[0]["id"] == "111"


def test_get_user_posts_ytdlp_logs_captured_messages():
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def extract_info(self, url, download=False):
            self.options["logger"].warning("Private account: login required")
            return {"entries": []}

    with (
        patch("app.tiktok.extractor.yt_dlp.YoutubeDL", FakeYoutubeDL),
        patch("app.tiktok.extractor.logger.info") as info_log,
    ):
        videos = get_user_posts_ytdlp("private-user")

    assert videos == []
    assert info_log.call_args.args == ("TikTok yt-dlp path returned 0 posts",)
    assert info_log.call_args.kwargs["username"] == "private-user"
    assert info_log.call_args.kwargs["ytdlp_messages"] == [
        "Private account: login required"
    ]


def test_empty_result_diagnostic_prefers_account_condition_and_is_short():
    diagnostic = EmptyResultEvidence(
        ytdlp_messages=[
            "The extractor is attempting impersonation, but no target is available",
            "This user's account is either private or has embedding disabled",
            "x" * 500,
        ]
    ).diagnostic()

    assert diagnostic == (
        "yt-dlp: This user's account is either private or has embedding disabled"
    )
    assert "\n" not in diagnostic
    assert len(diagnostic) <= 300


@pytest.mark.asyncio
async def test_get_user_posts_browser_logs_parsed_empty_item_list():
    response = MagicMock()
    response.url = "https://www.tiktok.com/api/post/item_list/?aid=1988"
    response.json = AsyncMock(
        return_value={"statusCode": 10221, "itemList": [], "statusMsg": ""}
    )
    page = make_browser_page(response=response)

    with patch("app.tiktok.extractor.logger.debug") as debug_log:
        result = await get_user_posts_browser(FakePool(page), "restricted-user")

    assert result.posts == []
    assert result.ssr_status is None
    assert result.post_api_responses == 1
    assert result.post_api_items == 0
    debug_log.assert_called_once_with(
        "TikTok post API returned empty itemList",
        username="restricted-user",
        url=response.url,
        status_code=10221,
        response_keys=["statusCode", "itemList", "statusMsg"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", sorted(TERMINAL_SSR_STATUSES))
async def test_get_user_posts_skips_retry_for_terminal_status(terminal_status):
    with (
        patch(
            "app.tiktok.extractor.get_user_posts_fast",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.tiktok.extractor._get_user_posts_ytdlp_result",
            return_value=YtdlpPostsResult(
                posts=[], messages=["ignored for terminal status"]
            ),
        ),
        patch(
            "app.tiktok.extractor.get_user_posts_browser",
            new_callable=AsyncMock,
            return_value=BrowserPostsResult([], terminal_status, 0, 0),
        ) as browser_path,
    ):
        posts, source, diagnostic = await get_user_posts(MagicMock(), "testuser")

    assert posts == []
    assert source == "browser"
    assert str(terminal_status) in diagnostic
    browser_path.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, 0])
async def test_get_user_posts_retries_non_terminal_status(status):
    with (
        patch(
            "app.tiktok.extractor.get_user_posts_fast",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.tiktok.extractor._get_user_posts_ytdlp_result",
            return_value=YtdlpPostsResult(posts=[], messages=[]),
        ),
        patch(
            "app.tiktok.extractor.get_user_posts_browser",
            new_callable=AsyncMock,
            return_value=BrowserPostsResult([], status, 2, 0),
        ) as browser_path,
    ):
        posts, source, diagnostic = await get_user_posts(MagicMock(), "testuser")

    assert posts == []
    assert source == "browser"
    assert diagnostic == "browser captured 0 items across 2 API responses"
    assert browser_path.await_count == 2


@pytest.mark.asyncio
async def test_get_user_posts_drops_diagnostic_when_browser_finds_posts():
    post = {"id": "123456789012345", "desc": "found"}
    with (
        patch(
            "app.tiktok.extractor.get_user_posts_fast",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.tiktok.extractor._get_user_posts_ytdlp_result",
            return_value=YtdlpPostsResult(
                posts=[], messages=["prior warning"]
            ),
        ),
        patch(
            "app.tiktok.extractor.get_user_posts_browser",
            new_callable=AsyncMock,
            return_value=BrowserPostsResult([post], None, 1, 1),
        ),
    ):
        posts, source, diagnostic = await get_user_posts(MagicMock(), "testuser")

    assert posts == [post]
    assert source == "browser"
    assert diagnostic is None


@pytest.mark.parametrize(
    ("posts", "extractor_diagnostic", "expected_diagnostic"),
    [
        (
            [],
            "TikTok status 10221 (likely banned/restricted)",
            "TikTok status 10221 (likely banned/restricted)",
        ),
        (
            [{"id": "123456789012345", "desc": "found"}],
            "yt-dlp: stale warning",
            None,
        ),
    ],
)
def test_posts_route_returns_diagnostic_only_for_empty_data(
    posts, extractor_diagnostic, expected_diagnostic
):
    client = TestClient(app)
    with (
        patch.object(settings, "api_key", "test-secret-123"),
        patch(
            "app.tiktok.extractor.get_user_posts",
            new_callable=AsyncMock,
            return_value=(posts, "browser", extractor_diagnostic),
        ),
    ):
        response = client.get(
            "/tiktok/user/testuser/posts",
            headers={"X-Api-Key": "test-secret-123"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert [post["id"] for post in body["data"]] == [post["id"] for post in posts]
    assert body["diagnostic"] == expected_diagnostic


@pytest.mark.live
@pytest.mark.asyncio
async def test_tiktok_live_integration():
    # Test against a known public TikTok username
    res = await get_user_posts_fast("tiktok")
    # Even if fast path is blocked, it should return None or list
    assert res is None or isinstance(res, list)
