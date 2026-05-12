"""One-off introspection — schema of all_sku table. Read-only metadata query.

Run with: python scripts/inspect_all_sku.py
"""
from __future__ import annotations

import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set")
        return
    conn = await asyncpg.connect(dsn, ssl="require")
    try:
        # Confirm table exists
        exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'all_sku')"
        )
        print(f"all_sku exists: {exists}\n")
        if not exists:
            # See what tables look similar
            tables = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' "
                "ORDER BY table_name"
            )
            print("public tables:")
            for t in tables:
                print(f"  {t['table_name']}")
            return

        # Columns
        cols = await conn.fetch("""
            SELECT column_name, data_type, is_nullable,
                   character_maximum_length, numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_name = 'all_sku'
            ORDER BY ordinal_position
        """)
        print(f"all_sku columns ({len(cols)}):")
        for c in cols:
            t = c['data_type']
            if c['character_maximum_length']:
                t = f"{t}({c['character_maximum_length']})"
            elif c['numeric_precision']:
                t = f"{t}({c['numeric_precision']},{c['numeric_scale']})"
            nn = "NOT NULL" if c['is_nullable'] == 'NO' else ""
            print(f"  {c['column_name']:30s} {t:25s} {nn}")

        # Row count
        n = await conn.fetchval("SELECT COUNT(*) FROM all_sku")
        print(f"\nrow count: {n}")

        # First 3 rows (peek at shape, not full data dump)
        sample = await conn.fetch("SELECT * FROM all_sku LIMIT 3")
        print(f"\nfirst {len(sample)} rows:")
        for row in sample:
            print(f"  {dict(row)}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
