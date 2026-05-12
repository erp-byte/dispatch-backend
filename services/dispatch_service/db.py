"""asyncpg connection pool lifecycle.

The pool is created lazily on first `get_pool()` call. If `DATABASE_URL` is
empty, `get_pool()` returns None — the DB mirror is then disabled and the
manager's `_mirror` helper will skip DB writes silently.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

import asyncpg

from shared.constants import (
    DB_APPLICATION_NAME,
    DB_COMMAND_TIMEOUT_S,
    DB_POOL_MAX_SIZE,
    DB_POOL_MIN_SIZE,
)

_pool: Optional[asyncpg.Pool] = None
_lock = asyncio.Lock()


async def get_pool() -> Optional[asyncpg.Pool]:
    """Return the shared asyncpg pool, creating it on first call.

    Returns None when `DATABASE_URL` is not set — callers must treat None as
    "DB mirror disabled" and skip silently.
    """
    global _pool
    if _pool is not None:
        return _pool
    dsn = (os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        return None
    async with _lock:
        if _pool is not None:
            return _pool
        _pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=DB_POOL_MIN_SIZE,
            max_size=DB_POOL_MAX_SIZE,
            command_timeout=DB_COMMAND_TIMEOUT_S,
            ssl="require",
            server_settings={"application_name": DB_APPLICATION_NAME},
        )
        return _pool


async def close_pool() -> None:
    """Close the pool if open. Safe to call at any time."""
    global _pool
    if _pool is None:
        return
    pool = _pool
    _pool = None
    await pool.close()
