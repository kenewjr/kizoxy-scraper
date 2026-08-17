import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.browser.health import get_pool_health
from app.browser.pool import BrowserPool
from app.config import settings
from app.errors import ScraperException
from app.models import APIResponse
from app.tiktok.router import router as tiktok_router
from app.youtube.router import router as youtube_router

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing kizoxy-scraper service")
    pool = BrowserPool(pool_size=settings.browser_pool_size)
    try:
        await pool.start()
    except Exception as e:
        logger.warning("Browser pool failed to start initially", error=str(e))
    app.state.browser_pool = pool
    yield
    logger.info("Shutting down kizoxy-scraper service")
    await pool.shutdown()


app = FastAPI(
    title="kizoxy-scraper",
    description="Internal microservice for YouTube & TikTok data extraction",
    version="0.2.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    structlog.contextvars.clear_contextvars()
    return response


@app.exception_handler(ScraperException)
async def scraper_exception_handler(request: Request, exc: ScraperException):
    resp = APIResponse.fail(exc.code.value, exc.message)
    return JSONResponse(status_code=200, content=resp.model_dump())


app.include_router(youtube_router)
app.include_router(tiktok_router)


@app.get("/health")
async def health_check():
    pool = getattr(app.state, "browser_pool", None)
    pool_health = get_pool_health(pool)
    return {
        "status": "ok",
        "service": "kizoxy-scraper",
        "version": "0.2.0",
        "browser_pool": pool_health,
    }
