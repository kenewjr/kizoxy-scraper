from typing import Any

from app.browser.pool import BrowserPool


def get_pool_health(pool: BrowserPool | None) -> dict[str, Any]:
    if pool is None:
        return {"status": "uninitialized", "pool_size": 0, "available": 0}
    stats = pool.get_stats()
    return {
        "status": "healthy" if stats["started"] else "stopped",
        "total_instances": stats["total_instances"],
        "available_instances": stats["available_instances"],
        "unhealthy_instances": stats.get("unhealthy_instances", 0),
        "total_consecutive_errors": stats.get("total_consecutive_errors", 0),
    }
