"""Server-side ID generator.

Format: `{last 8 digits of current epoch-ms}-{counter}` (e.g. "23456789-7").
Articles for one invoice share a single base; counters run 1..N. Invoice IDs
use the same shape but with their own per-base counter.

The counter is kept in an in-process cache. On a cold cache (process restart,
unseen base) we query the DB for `MAX(SPLIT_PART(id, '-', 2)::int) WHERE id
LIKE '{base}-%'` so we never collide with existing rows.
"""

from __future__ import annotations

import asyncio
import time


_counter_cache: dict[str, int] = {}
_lock = asyncio.Lock()


def _current_base() -> str:
    """Last 8 digits of current epoch-ms."""
    return str(int(time.time() * 1000))[-8:]


async def _reserve_invoice_counter(conn) -> tuple[str, int]:
    async with _lock:
        base = _current_base()
        if base not in _counter_cache:
            db_max = await conn.fetchval(
                "SELECT COALESCE(MAX(SPLIT_PART(id, '-', 2)::int), 0) "
                "FROM billed_details WHERE id LIKE $1",
                f"{base}-%",
            )
            _counter_cache[base] = int(db_max or 0)
        _counter_cache[base] += 1
        return base, _counter_cache[base]


async def new_invoice_id(conn) -> str:
    """Return a fresh invoice id of shape '{8-digit-epoch}-{counter}'."""
    base, ctr = await _reserve_invoice_counter(conn)
    return f"{base}-{ctr}"


async def new_article_ids(conn, n: int) -> list[str]:
    """Return n article ids sharing one fresh base; counters run 1..N
    (offset by any DB-existing rows on that base)."""
    if n <= 0:
        return []
    async with _lock:
        base = _current_base()
        db_max = await conn.fetchval(
            "SELECT COALESCE(MAX(SPLIT_PART(id, '-', 2)::int), 0) "
            "FROM billed_articles WHERE id LIKE $1",
            f"{base}-%",
        )
        start = int(db_max or 0) + 1
        return [f"{base}-{start + i}" for i in range(n)]


def _reset_cache_for_test() -> None:
    """Test-only: clear the counter cache."""
    _counter_cache.clear()
