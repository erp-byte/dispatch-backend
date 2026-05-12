"""Idempotent migration runner.

Reads .sql files from db/migrations/ in lexical order, applies any not yet
recorded in the `schema_migrations` table.

The `pretend_applied` argument lets you mark specific migration files as
already-applied without executing them — used on first-run against a database
that was bootstrapped manually (the live RDS instance was created this way).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

import asyncpg


_CREATE_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def _ensure_table(conn: asyncpg.Connection) -> None:
    exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'schema_migrations')"
    )
    if not exists:
        await conn.execute(_CREATE_SCHEMA_MIGRATIONS_SQL)


async def _applied_filenames(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT filename FROM schema_migrations")
    return {r["filename"] for r in rows}


async def _mark_applied(conn: asyncpg.Connection, filename: str) -> None:
    await conn.execute(
        "INSERT INTO schema_migrations (filename) VALUES ($1) ON CONFLICT DO NOTHING",
        filename,
    )


async def run_migrations(
    conn: asyncpg.Connection,
    *,
    migrations_dir: str = "db/migrations",
    pretend_applied: Optional[Iterable[str]] = None,
) -> list[str]:
    """Apply any pending migrations. Returns the list of filenames applied.

    If `pretend_applied` includes a filename whose migration has not yet been
    recorded, the runner records it WITHOUT executing the .sql content.
    """
    await _ensure_table(conn)
    already = await _applied_filenames(conn)
    pretend = set(pretend_applied or ())

    applied: list[str] = []
    sql_files = sorted(
        f for f in os.listdir(migrations_dir) if f.endswith(".sql")
    )
    for fname in sql_files:
        if fname in already:
            continue
        if fname in pretend:
            await _mark_applied(conn, fname)
            applied.append(fname)
            continue
        body = Path(migrations_dir, fname).read_text(encoding="utf-8")
        async with conn.transaction():
            await conn.execute(body)
            await _mark_applied(conn, fname)
        applied.append(fname)
    return applied
