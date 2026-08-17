# kizoxy-scraper

Standalone Python microservice (FastAPI + Camoufox browser pool + curl_cffi + yt-dlp) for YouTube and TikTok data extraction.
Designed to be consumed internally by the Kizoxy Node.js bot over HTTP with a shared secret.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  FastAPI (uvicorn)                   │
│                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────┐  │
│  │ /youtube/*   │  │ /tiktok/*    │  │ /health   │  │
│  │  (yt-dlp)    │  │  (curl_cffi  │  │           │  │
│  │              │  │   + browser) │  │           │  │
│  └──────┬───────┘  └──────┬───────┘  └───────────┘  │
│         │                 │                         │
│  ┌──────┴─────────────────┴──────────────────────┐  │
│  │         Shared: config, auth, errors,         │  │
│  │         http_client, browser pool             │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

### Dual-Path Strategy (TikTok)

1. **Fast path** (`curl_cffi`): TLS-impersonated HTTP request → parse `__UNIVERSAL_DATA_FOR_REHYDRATION__` JSON from HTML. Fastest, but TikTok may block.
2. **Browser fallback** (`Camoufox`): Full headless Firefox with anti-fingerprint. Slower but reliable. Auto-fallback when fast path fails.

YouTube uses `yt-dlp` natively (no browser needed).

## Project Structure

```
app/
├── __init__.py
├── main.py            # FastAPI app, lifespan, middleware
├── config.py          # Pydantic settings (env vars)
├── auth.py            # X-Api-Key header validation
├── errors.py          # Error codes, exceptions hierarchy
├── http_client.py     # curl_cffi async wrapper with retry
├── models.py          # Pydantic response/data models
├── browser/
│   ├── pool.py        # Camoufox browser pool (asyncio.Queue)
│   └── health.py      # Pool health check helper
├── tiktok/
│   ├── extractor.py   # TikTok data extraction (fast + browser)
│   └── router.py      # TikTok API endpoints
└── youtube/
    ├── extractor.py   # YouTube data extraction (yt-dlp)
    └── router.py      # YouTube API endpoints
```

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | No | Service & browser pool health |
| `GET` | `/youtube/channel/{channel_id}/latest?limit=5` | Yes | Latest channel videos |
| `GET` | `/youtube/channel/{channel_id}/live` | Yes | Channel live status |
| `GET` | `/tiktok/user/{username}/posts` | Yes | User posts (fast→browser fallback) |
| `GET` | `/tiktok/user/{username}/live` | Yes | User live status |

### Response Format

All authenticated endpoints return:
```json
{
  "success": true,
  "data": { ... },
  "source": "fast" | "browser",
  "error": null
}
```

On error:
```json
{
  "success": false,
  "data": null,
  "source": null,
  "error": { "code": "BLOCKED", "message": "..." }
}
```

### Error Codes

| Code | Description |
|------|-------------|
| `BLOCKED` | Target site blocked the request (403) |
| `NOT_FOUND` | Resource not found (404, invalid channel/user) |
| `TIMEOUT` | Request timed out |
| `RATE_LIMITED` | Rate limited by target site (429) |
| `INTERNAL` | Internal scraper error |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | `change-me-to-a-secure-random-secret` | `X-Api-Key` header value |
| `BIND_HOST` | `127.0.0.1` | Bind address |
| `BIND_PORT` | `8100` | Bind port |
| `BROWSER_POOL_SIZE` | `1` | Number of Camoufox instances. Keep `1` for TikTok stability; concurrent browsers can make `/api/post/item_list/` return empty/non-JSON responses. |
| `BROWSER_MAX_AGE_SECONDS` | `1800` | Max browser instance age before recycle |
| `BROWSER_MAX_USES` | `1` | Max uses per browser instance. Keep `1` so empty TikTok attempts retry with a fresh fingerprint. |
| `REQUEST_TIMEOUT` | `10` | HTTP request timeout (seconds) |
| `MAX_RETRIES` | `2` | Retry count for failed fast-path requests |
| `PROXY_URL` | *(none)* | Optional proxy (`http://user:pass@host:port`) |
| `TIKTOK_SESSION_ID` | *(none)* | Optional TikTok `sessionid` cookie for restricted accounts returning SSR status `209002` |
| `TIKTOK_COOKIE` | *(none)* | Optional fallback TikTok session cookie value |

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
python -m camoufox fetch
uvicorn app.main:app --port 8100
```

## Testing

```bash
# Unit tests only
pytest tests/ -m "not live" -v

# Include live integration tests (hits real APIs)
pytest tests/ -v
```

## Docker Deployment

```bash
docker compose up -d --build
```
