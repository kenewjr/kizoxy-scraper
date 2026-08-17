from fastapi import APIRouter, Depends, Request

from app.auth import verify_shared_secret
from app.errors import ScraperException
from app.models import APIResponse, LiveStatus, TikTokPost
from app.tiktok import extractor

router = APIRouter(prefix="/tiktok", dependencies=[Depends(verify_shared_secret)])


@router.get("/user/{username}/posts", response_model=APIResponse[list[TikTokPost]])
async def get_user_posts(
    username: str,
    request: Request,
    session_id: str | None = None,
):
    try:
        pool = getattr(request.app.state, "browser_pool", None)
        raw_posts, source, diagnostic = await extractor.get_user_posts(
            pool, username, session_id=session_id
        )
        posts = [TikTokPost(**p) for p in raw_posts]
        return APIResponse.ok(
            posts,
            source=source,  # type: ignore[arg-type]
            diagnostic=diagnostic if not posts else None,
        )
    except ScraperException as e:
        return APIResponse.fail(e.code.value, e.message)
    except Exception as e:
        return APIResponse.fail("INTERNAL", f"Failed to fetch TikTok posts: {e!s}")


@router.get("/user/{username}/live", response_model=APIResponse[LiveStatus])
async def get_user_live(username: str, request: Request):
    try:
        pool = getattr(request.app.state, "browser_pool", None)
        raw_live, source = await extractor.get_user_live(pool, username)
        status = LiveStatus(**raw_live)
        return APIResponse.ok(status, source=source)  # type: ignore[arg-type]
    except ScraperException as e:
        return APIResponse.fail(e.code.value, e.message)
    except Exception as e:
        return APIResponse.fail(
            "INTERNAL", f"Failed to fetch TikTok live status: {e!s}"
        )
