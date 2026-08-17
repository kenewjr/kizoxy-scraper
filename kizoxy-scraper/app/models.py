from typing import Generic, Literal, TypeVar

from pydantic import BaseModel

from app.errors import ErrorDetail

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    success: bool
    data: T | None = None
    source: Literal["fast", "browser", "ytdlp"] | None = None
    error: ErrorDetail | None = None
    diagnostic: str | None = None

    @classmethod
    def ok(
        cls,
        data: T,
        source: Literal["fast", "browser", "ytdlp"] = "fast",
        diagnostic: str | None = None,
    ) -> "APIResponse[T]":
        return cls(
            success=True,
            data=data,
            source=source,
            error=None,
            diagnostic=diagnostic,
        )

    @classmethod
    def fail(cls, code: str, message: str) -> "APIResponse[T]":
        return cls(
            success=False,
            data=None,
            source=None,
            error=ErrorDetail(code=code, message=message),
            diagnostic=None,
        )


class VideoEntry(BaseModel):
    id: str
    title: str | None = None
    upload_date: str | None = None
    url: str | None = None


class LiveStatus(BaseModel):
    is_live: bool
    video_id: str | None = None
    title: str | None = None


class TikTokPost(BaseModel):
    id: str
    desc: str | None = None
    create_time: int | None = None
    video_url: str | None = None
    cover_url: str | None = None
