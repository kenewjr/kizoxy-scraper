import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from camoufox.async_api import AsyncCamoufox
from structlog import get_logger

from app.config import settings
from app.errors import BrowserPoolExhaustedError

logger = get_logger()

HEALTH_ERROR_THRESHOLD = 3
# NOTE: must stay comfortably below the Node client's request timeout
# (90s, see src/integrations/scraperService/client.js) so a saturated pool
# fails fast with a clear POOL_EXHAUSTED error instead of hanging until the
# Node side aborts the connection — that abort is what produced the
# "TikTok notifications sometimes work, sometimes don't" symptom: Node gave
# up on a still-in-progress request, logged the service as offline, and the
# poll cycle silently returned zero videos. Was accidentally 300 (5 min).
ACQUIRE_TIMEOUT_SECONDS = 25


@dataclass
class BrowserInstance:
    cm: Any
    browser: Any
    spawned_at: float = field(default_factory=time.time)
    uses: int = 0
    consecutive_errors: int = 0


class BrowserPool:
    def __init__(
        self,
        pool_size: int = 3,
        max_age_seconds: int | None = None,
        max_uses: int | None = None,
    ):
        self._pool_size = pool_size
        self._max_age_seconds = max_age_seconds or settings.browser_max_age_seconds
        self._max_uses = max_uses or settings.browser_max_uses
        self._available: asyncio.Queue[BrowserInstance] = asyncio.Queue()
        self._all_instances: list[BrowserInstance] = []
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self):
        if self._started:
            return
        logger.info("Starting browser pool", pool_size=self._pool_size)
        for i in range(self._pool_size):
            try:
                instance = await self._spawn_instance()
                await self._available.put(instance)
            except Exception as e:
                logger.warning(
                    "Failed to spawn browser instance",
                    index=i,
                    error=str(e),
                )
        self._started = True

    async def _spawn_instance(self) -> BrowserInstance:
        """Launch a new browser instance and register it in the pool.

        Deliberately does NOT enqueue the instance into ``self._available``.
        Callers decide when (and whether) an instance becomes visible to
        other consumers — see the ``_recycle()`` docstring for why this
        matters.
        """
        kwargs: dict[str, Any] = {
            "headless": True,
            "geoip": False,
            "os": "windows",
            "locale": "id-ID",
        }
        if settings.proxy_url:
            kwargs["proxy"] = {"server": settings.proxy_url}

        camoufox_cm = AsyncCamoufox(**kwargs)
        browser = await camoufox_cm.__aenter__()

        instance = BrowserInstance(cm=camoufox_cm, browser=browser)
        self._all_instances.append(instance)
        return instance

    def _needs_recycle(self, instance: BrowserInstance) -> bool:
        age = time.time() - instance.spawned_at
        return (
            age > self._max_age_seconds
            or instance.uses >= self._max_uses
            or instance.consecutive_errors >= HEALTH_ERROR_THRESHOLD
        )

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Any, None]:
        try:
            instance = await asyncio.wait_for(
                self._available.get(), timeout=ACQUIRE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            raise BrowserPoolExhaustedError(
                f"No browser instance available within {ACQUIRE_TIMEOUT_SECONDS}s"
            )

        recycling = False
        page_error_occurred = False
        try:
            if self._needs_recycle(instance):
                recycling = True
                instance = await self._recycle(instance)
                recycling = False

            instance.uses += 1
            try:
                page = await instance.browser.new_page()
                try:
                    yield page
                finally:
                    try:
                        await page.close()
                    except Exception as e:
                        logger.warning("Error closing page", error=str(e))
            except Exception:
                page_error_occurred = True
                raise
        except Exception:
            if recycling:
                try:
                    replacement = await self._spawn_instance()
                    await self._available.put(replacement)
                except Exception as spawn_err:
                    logger.error(
                        "Failed to replace instance after error",
                        error=str(spawn_err),
                    )
            else:
                if page_error_occurred:
                    instance.consecutive_errors += 1
                await self._available.put(instance)
            raise
        else:
            instance.consecutive_errors = 0
            await self._available.put(instance)

    async def _recycle(self, old_instance: BrowserInstance) -> BrowserInstance:
        """Close old instance and spawn a new one for the caller's exclusive,
        immediate use — never via the shared queue.

        BUG FIX (previously): ``_spawn_instance()`` used to enqueue the new
        instance into ``self._available`` as a side effect, and this method
        then called ``get_nowait()`` to pull it back out for itself. Between
        those two steps there was no lock protecting the queue, so a second,
        concurrent ``acquire()`` call (which only does a plain
        ``self._available.get()``, unguarded by ``self._lock``) could steal
        the freshly-queued instance first. When that happened, two callers
        ended up holding the same ``BrowserInstance``/pages concurrently,
        producing intermittent, hard-to-reproduce failures — most visible
        here as TikTok notifications that "sometimes work, sometimes don't",
        since Kizoxy's Node client fetches posts and live-status for the
        same account concurrently (Promise.allSettled) and this pool
        recycled on nearly every request when BROWSER_MAX_USES was low.
        ``_spawn_instance()`` no longer auto-enqueues, so this method simply
        uses the instance it just created without ever exposing it to other
        consumers.
        """
        async with self._lock:
            logger.info("Recycling browser instance", uses=old_instance.uses)
            if old_instance in self._all_instances:
                self._all_instances.remove(old_instance)
            try:
                await old_instance.cm.__aexit__(None, None, None)
            except Exception as e:
                logger.warning("Error closing recycled browser", error=str(e))

            return await self._spawn_instance()

    async def shutdown(self):
        logger.info("Shutting down browser pool")
        async with self._lock:
            for instance in list(self._all_instances):
                try:
                    await instance.cm.__aexit__(None, None, None)
                except Exception as e:
                    logger.warning(
                        "Error closing browser during shutdown", error=str(e)
                    )
            self._all_instances.clear()
            self._started = False

    def get_stats(self) -> dict[str, Any]:
        unhealthy = sum(
            1
            for inst in self._all_instances
            if inst.consecutive_errors >= HEALTH_ERROR_THRESHOLD
        )
        total_errors = sum(inst.consecutive_errors for inst in self._all_instances)
        return {
            "total_instances": len(self._all_instances),
            "available_instances": self._available.qsize(),
            "unhealthy_instances": unhealthy,
            "total_consecutive_errors": total_errors,
            "started": self._started,
        }
