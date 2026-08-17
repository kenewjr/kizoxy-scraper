import asyncio
import sys
import time
from pathlib import Path

# Ensure root workspace directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from structlog import get_logger

from app.browser.pool import BrowserPool
from app.config import settings

logger = get_logger()


async def warmup():
    pool_size = int(sys.argv[1]) if len(sys.argv) > 1 else settings.browser_pool_size
    print("============================================================")
    print("  kizoxy-scraper Browser Pool Warmup & Debug Tool")
    print(f"  Target Pool Size : {pool_size}")
    print(f"  Max Age (s)     : {settings.browser_max_age_seconds}")
    print(f"  Max Uses        : {settings.browser_max_uses}")
    print(f"  Proxy           : {settings.proxy_url or 'None (Direct)'}")
    print("============================================================")

    pool = BrowserPool(pool_size=pool_size)

    print("\n[1/4] Starting browser pool...")
    start_time = time.monotonic()
    await pool.start()
    init_duration = round((time.monotonic() - start_time) * 1000)
    stats = pool.get_stats()
    print(
        f"      Pool initialized in {init_duration}ms. "
        f"Instances: {stats['total_instances']}/{pool_size}, "
        f"Available: {stats['available_instances']}"
    )

    print("\n[2/4] Verifying page acquisition across pool instances...")
    acquire_success = 0
    try:
        for i in range(pool_size):
            async with pool.acquire() as page:
                title = await page.title()
                acquire_success += 1
                print(f"      Instance #{i+1} acquired OK. Blank page title: '{title}'")
    except Exception as e:
        print(f"      [ERROR] Acquisition failed: {e}")

    print(f"      Acquired {acquire_success}/{pool_size} instances successfully.")

    print("\n[3/4] Testing pool stats & health tracking...")
    stats = pool.get_stats()
    print(f"      Stats: {stats}")

    print("\n[4/4] Shutting down browser pool...")
    shutdown_start = time.monotonic()
    await pool.shutdown()
    shutdown_duration = round((time.monotonic() - shutdown_start) * 1000)
    print(f"      Pool shut down cleanly in {shutdown_duration}ms.")

    print("\n============================================================")
    print("  WARMUP COMPLETE — Browser pool is operational.")
    print("============================================================\n")


if __name__ == "__main__":
    asyncio.run(warmup())
