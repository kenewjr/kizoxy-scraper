import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.browser.pool import BrowserPool
from app.errors import BrowserPoolExhaustedError


@pytest.mark.asyncio
async def test_browser_pool_lifecycle():
    mock_camoufox = MagicMock()
    mock_browser = AsyncMock()
    mock_page = AsyncMock()
    mock_browser.new_page = AsyncMock(return_value=mock_page)
    mock_camoufox.__aenter__ = AsyncMock(return_value=mock_browser)
    mock_camoufox.__aexit__ = AsyncMock(return_value=None)

    with patch("app.browser.pool.AsyncCamoufox", return_value=mock_camoufox):
        pool = BrowserPool(pool_size=2, max_age_seconds=10)
        await pool.start()

        stats = pool.get_stats()
        assert stats["total_instances"] == 2
        assert stats["available_instances"] == 2

        async with pool.acquire() as page:
            assert page == mock_page
            assert pool.get_stats()["available_instances"] == 1

        assert pool.get_stats()["available_instances"] == 2
        await pool.shutdown()
        assert pool.get_stats()["started"] is False


@pytest.mark.asyncio
async def test_browser_pool_recycle_on_uses():
    mock_camoufox = MagicMock()
    mock_browser = AsyncMock()
    mock_page = AsyncMock()
    mock_browser.new_page = AsyncMock(return_value=mock_page)
    mock_camoufox.__aenter__ = AsyncMock(return_value=mock_browser)
    mock_camoufox.__aexit__ = AsyncMock(return_value=None)

    with patch("app.browser.pool.AsyncCamoufox", return_value=mock_camoufox):
        pool = BrowserPool(pool_size=1, max_age_seconds=1000, max_uses=100)
        await pool.start()

        # Simulate max uses reach
        instance = pool._all_instances[0]
        instance.uses = 100

        async with pool.acquire() as page:
            assert page == mock_page

        await pool.shutdown()


@pytest.mark.asyncio
async def test_recycle_failure_does_not_requeue_stale_instance():
    mock_camoufox = MagicMock()
    mock_browser = AsyncMock()
    mock_page = AsyncMock()
    mock_browser.new_page = AsyncMock(return_value=mock_page)
    mock_camoufox.__aenter__ = AsyncMock(return_value=mock_browser)
    mock_camoufox.__aexit__ = AsyncMock(return_value=None)

    with patch("app.browser.pool.AsyncCamoufox", return_value=mock_camoufox):
        pool = BrowserPool(pool_size=1, max_age_seconds=1000, max_uses=100)
        await pool.start()

        stale_instance = pool._all_instances[0]
        stale_instance.uses = 100

        # Mock _spawn_instance to fail on first call during recycle
        original_spawn = pool._spawn_instance
        spawn_calls = 0

        async def failing_spawn():
            nonlocal spawn_calls
            spawn_calls += 1
            if spawn_calls == 1:
                raise RuntimeError("Recycle spawn failure")
            return await original_spawn()

        pool._spawn_instance = failing_spawn

        with pytest.raises(RuntimeError, match="Recycle spawn failure"):
            async with pool.acquire():
                pass

        # Assert stale instance is closed/removed and not in available queue
        assert stale_instance not in pool._all_instances
        # Queue contains the replacement instance, NOT stale_instance
        assert pool._available.qsize() == 1
        fresh_instance = pool._available.get_nowait()
        await pool._available.put(fresh_instance)
        assert fresh_instance is not stale_instance
        assert fresh_instance.uses == 0

        # Subsequent acquire should get fresh instance
        async with pool.acquire() as page:
            assert page == mock_page
            assert pool.get_stats()["total_instances"] == 1

        await pool.shutdown()


@pytest.mark.asyncio
async def test_recycle_and_replacement_both_fail_shrinks_pool():
    mock_camoufox = MagicMock()
    mock_browser = AsyncMock()
    mock_page = AsyncMock()
    mock_browser.new_page = AsyncMock(return_value=mock_page)
    mock_camoufox.__aenter__ = AsyncMock(return_value=mock_browser)
    mock_camoufox.__aexit__ = AsyncMock(return_value=None)

    with patch("app.browser.pool.AsyncCamoufox", return_value=mock_camoufox):
        pool = BrowserPool(pool_size=1, max_age_seconds=1000, max_uses=100)
        await pool.start()

        stale_instance = pool._all_instances[0]
        stale_instance.uses = 100

        async def always_failing_spawn():
            raise RuntimeError("Total spawn failure")

        pool._spawn_instance = always_failing_spawn

        with pytest.raises(RuntimeError, match="Total spawn failure"):
            async with pool.acquire():
                pass

        # Stale instance removed, replacement failed, pool shrunk to 0
        assert stale_instance not in pool._all_instances
        assert pool._available.qsize() == 0

        await pool.shutdown()


@pytest.mark.asyncio
async def test_health_tracking_and_recycle_on_threshold():
    mock_camoufox = MagicMock()
    mock_browser = AsyncMock()
    mock_page = AsyncMock()
    mock_browser.new_page = AsyncMock(return_value=mock_page)
    mock_camoufox.__aenter__ = AsyncMock(return_value=mock_browser)
    mock_camoufox.__aexit__ = AsyncMock(return_value=None)

    with patch("app.browser.pool.AsyncCamoufox", return_value=mock_camoufox):
        pool = BrowserPool(pool_size=1, max_age_seconds=10000, max_uses=10000)
        await pool.start()

        first_instance = pool._all_instances[0]

        # Trigger 3 consecutive page errors
        for _ in range(3):
            with pytest.raises(ValueError, match="Page navigation crash"):
                async with pool.acquire():
                    raise ValueError("Page navigation crash")

        assert first_instance.consecutive_errors == 3
        stats = pool.get_stats()
        assert stats["unhealthy_instances"] == 1
        assert stats["total_consecutive_errors"] == 3

        # Fourth acquire should trigger recycle because consecutive_errors >= HEALTH_ERROR_THRESHOLD
        async with pool.acquire() as page:
            assert page == mock_page

        new_instance = pool._all_instances[0]
        assert new_instance is not first_instance
        assert new_instance.consecutive_errors == 0

        await pool.shutdown()


@pytest.mark.asyncio
async def test_recycle_does_not_double_issue_instance_to_concurrent_acquirer():
    """Regression test for the acquire/recycle race fixed 2026-08-15.

    Previously, _spawn_instance() enqueued its newly-created instance into
    self._available as a side effect, and _recycle() then called
    get_nowait() to reclaim it for its own caller's exclusive use. Between
    those two steps there was no lock protecting the queue, so a second,
    concurrently-waiting acquire() (a plain self._available.get(), never
    guarded by self._lock) could steal the freshly-queued instance first.
    Both callers would then hold the same BrowserInstance/page at once.

    _spawn_instance() no longer auto-enqueues (see pool.py), so the newly
    recycled instance must never appear in the available queue while it is
    still checked out by the acquire() call that triggered the recycle.
    """
    mock_camoufox = MagicMock()
    mock_browser = AsyncMock()
    mock_page = AsyncMock()
    mock_browser.new_page = AsyncMock(return_value=mock_page)
    mock_camoufox.__aenter__ = AsyncMock(return_value=mock_browser)
    mock_camoufox.__aexit__ = AsyncMock(return_value=None)

    with patch("app.browser.pool.AsyncCamoufox", return_value=mock_camoufox):
        pool = BrowserPool(pool_size=1, max_age_seconds=1000, max_uses=1)
        await pool.start()

        # Force the single instance to need recycling on the next acquire.
        pool._all_instances[0].uses = 1

        release_event = asyncio.Event()

        async def hold_instance():
            async with pool.acquire():
                await release_event.wait()

        holder = asyncio.create_task(hold_instance())
        # Give the holder task a chance to run through recycle + acquire.
        for _ in range(50):
            await asyncio.sleep(0)
            if pool.get_stats()["total_instances"] == 1:
                break

        # While the recycled instance is checked out by `holder`, it must
        # NOT be sitting in the available queue where a second acquirer
        # could grab it too.
        assert pool._available.qsize() == 0

        release_event.set()
        await holder

        # Once released, exactly one instance is available again — and it
        # was never duplicated or lost.
        assert pool._available.qsize() == 1
        assert pool.get_stats()["total_instances"] == 1

        await pool.shutdown()


@pytest.mark.asyncio
async def test_acquire_timeout_raises_exhausted_error():
    mock_camoufox = MagicMock()
    mock_browser = AsyncMock()
    mock_page = AsyncMock()
    mock_browser.new_page = AsyncMock(return_value=mock_page)
    mock_camoufox.__aenter__ = AsyncMock(return_value=mock_browser)
    mock_camoufox.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.browser.pool.AsyncCamoufox", return_value=mock_camoufox),
        patch("app.browser.pool.ACQUIRE_TIMEOUT_SECONDS", 0.05),
    ):
        pool = BrowserPool(pool_size=1)
        await pool.start()

        # Acquire the only available instance
        async with pool.acquire():
            # Try to acquire another while busy
            with pytest.raises(
                BrowserPoolExhaustedError,
                match="No browser instance available within",
            ):
                async with pool.acquire():
                    pass

        await pool.shutdown()
