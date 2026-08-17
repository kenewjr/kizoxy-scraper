import hmac

import structlog
from fastapi import Header, HTTPException

from app.config import settings

logger = structlog.get_logger()


async def verify_shared_secret(x_api_key: str = Header(...)):
    if not settings.api_key:
        return
    client_key = x_api_key.strip()
    server_key = settings.api_key.strip()
    if hmac.compare_digest(client_key, server_key):
        return
    hint = ""
    if "," in client_key:
        parts = [p.strip() for p in client_key.split(",")]
        if len(parts) > 1 and all(p == parts[0] for p in parts):
            hint = (
                " — looks like the client sent the same API key header "
                "multiple times under different casing (e.g. both "
                "'x-api-key' and 'X-Api-Key' in the same headers object); "
                "HTTP combines repeated headers with ', ' on the wire. "
                "Check the client's request headers construction, not "
                "the key value itself."
            )

    logger.warning(
        "API key verification failed" + hint,
        provided=repr(client_key),
        expected=repr(server_key),
    )
    raise HTTPException(status_code=403, detail="Forbidden")

