from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    api_key: str = "change-me-to-a-secure-random-secret"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8100
    browser_pool_size: int = 3
    browser_max_age_seconds: int = 1800
    # NOTE: was 1, meaning every request after the very first triggered a
    # full browser respawn (_recycle). Combined with the acquire/recycle
    # race that has since been fixed in app/browser/pool.py, this made
    # concurrent requests (Kizoxy fetches TikTok posts + live status in
    # parallel per account) fail intermittently. 5 keeps periodic
    # fingerprint rotation for anti-bot purposes without recycling on
    # almost every call.
    browser_max_uses: int = 5
    request_timeout: int = 10
    max_retries: int = 2
    proxy_url: str | None = None
    tiktok_session_id: str | None = None
    tiktok_cookie: str | None = None

    @field_validator("proxy_url", "tiktok_session_id", "tiktok_cookie", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v: Any) -> Any:
        if isinstance(v, str):
            v_str = v.strip()
            if not v_str:
                return None
            if "api.proxyscrape.com" in v_str or "get?request=" in v_str:
                return None
        return v

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()
