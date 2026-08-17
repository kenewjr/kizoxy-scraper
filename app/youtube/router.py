import asyncio

from fastapi import APIRouter, Depends, Query

from app.auth import verify_shared_secret
from app.errors import ScraperException
from app.models import APIResponse, LiveStatus, VideoEntry
from app.youtube import extractor

router = APIRouter(prefix="/youtube", dependencies=[Depends(verify_shared_secret)])


@router.get(
    "/channel/{channel_id}/latest", response_model=APIResponse[list[VideoEntry]]
)
async def get_latest_videos(
    channel_id: str, limit: int = Query(default=5, ge=1, le=50)
):
    try:
        raw_videos = await asyncio.to_thread(
            extractor.get_channel_latest_videos, channel_id, limit
        )
        videos = [VideoEntry(**v) for v in raw_videos]
        return APIResponse.ok(videos, source="fast")
    except ScraperException as e:
        return APIResponse.fail(e.code.value, e.message)
    except Exception as e:
        return APIResponse.fail(
            "INTERNAL", f"Failed to fetch YouTube latest videos: {e!s}"
        )


@router.get("/channel/{channel_id}/live", response_model=APIResponse[LiveStatus])
async def get_live_status(channel_id: str):
    try:
        raw_live = await asyncio.to_thread(extractor.check_channel_live, channel_id)
        status = LiveStatus(**raw_live)
        return APIResponse.ok(status, source="fast")
    except ScraperException as e:
        return APIResponse.fail(e.code.value, e.message)
    except Exception as e:
        return APIResponse.fail(
            "INTERNAL", f"Failed to fetch YouTube live status: {e!s}"
        )
