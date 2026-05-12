"""One-off: apply db/migrations/003_sku_match.sql against the live RDS.

Reads DATABASE_URL from .env. Idempotent — uses ADD COLUMN IF NOT EXISTS
pattern (we wrap each ALTER in a try/except for IDEMPOTENCY since plain
ALTER TABLE ADD COLUMN errors if the column already exists).

Run with: python scripts/apply_migration_003.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set")
        return
    sql_path = Path("db/migrations/003_sku_match.sql")
    body = sql_path.read_text(encoding="utf-8")

    conn = await asyncpg.connect(dsn, ssl="require")
    try:
        # Idempotency guard — check if matched_sku_id already exists.
        already = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'billed_articles' AND column_name = 'matched_sku_id')"
        )
        if already:
            print("matched_sku_id already exists — migration 003 already applied. Exiting.")
            return

        async with conn.transaction():
            await conn.execute(body)
        print("migration 003 applied successfully")

        # Verify
        cols = await conn.fetch("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'billed_articles'
              AND column_name LIKE 'matched_%'
            ORDER BY column_name
        """)
        print(f"new columns on billed_articles ({len(cols)}):")
        for c in cols:
            print(f"  {c['column_name']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
