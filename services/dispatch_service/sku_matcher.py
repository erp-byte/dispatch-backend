"""Fuzzy match parsed article descriptions to the all_sku master table.

The matcher loads the SKU list into memory at startup (3713 rows ~few hundred KB)
and matches each parsed article description against the cached `particulars`
column using rapidfuzz token_set_ratio. Matches at or above the threshold
return the canonical SKU fields for denormalisation onto billed_articles.

The cache is refreshed via `await matcher.load()`. Callers that need
fresh-from-DB matching after admin changes to all_sku should call load()
again — there's no automatic invalidation.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import asyncpg
from rapidfuzz import fuzz, process


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(s: Optional[str]) -> str:
    """Lowercase + strip + collapse whitespace. Used on both sides of the match."""
    if s is None:
        return ""
    return _WHITESPACE_RE.sub(" ", str(s).strip().lower())


_LOAD_SQL = """
SELECT sku_id, particulars, item_type, item_group, sub_group,
       uom, gst, sale_group
FROM all_sku
"""


class SkuMatcher:
    """Loads all_sku into memory and fuzzy-matches article descriptions to it.

    Usage:
        matcher = SkuMatcher(pool=pool, threshold=80.0)
        await matcher.load()
        result = matcher.match("Atta 5 kg")
        # result: {"matched_sku_id": 1, "matched_item_type": "fg", ...} or None

    The returned dict's keys are intentionally the same as the new columns on
    billed_articles (matched_*), so callers can spread it directly into the
    INSERT statement params.
    """

    def __init__(self, pool: asyncpg.Pool, threshold: float = 80.0):
        self._pool = pool
        self._threshold = float(threshold)
        # Parallel lists: normalized_particulars[i] corresponds to rows[i].
        # rapidfuzz.process.extractOne is fastest against a flat list of strings.
        self._normalized: list[str] = []
        self._rows: list[dict[str, Any]] = []

    async def load(self) -> int:
        """Fetch all_sku rows into the in-memory cache. Returns row count."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_LOAD_SQL)
        self._rows = [dict(r) for r in rows]
        self._normalized = [_normalize(r["particulars"]) for r in self._rows]
        return len(self._rows)

    def match(self, article: Optional[str]) -> Optional[dict[str, Any]]:
        """Return the matched SKU fields (denorm dict) or None if no candidate
        clears the threshold.

        Returns None on empty input, None when the matcher hasn't been loaded,
        and None when the best score is below `threshold`.
        """
        if not self._normalized:
            return None
        needle = _normalize(article)
        if not needle:
            return None
        best = process.extractOne(
            needle, self._normalized,
            scorer=fuzz.token_set_ratio,
            score_cutoff=self._threshold,
        )
        if best is None:
            return None
        # extractOne returns (choice, score, index) when given a list
        _choice, _score, idx = best
        row = self._rows[idx]
        return {
            "matched_sku_id": row["sku_id"],
            "matched_item_type": row.get("item_type"),
            "matched_item_group": row.get("item_group"),
            "matched_sub_group": row.get("sub_group"),
            "matched_uom": row.get("uom"),
            "matched_gst": row.get("gst"),
            "matched_sale_group": row.get("sale_group"),
        }

    @property
    def loaded_count(self) -> int:
        """Number of SKUs currently in the cache."""
        return len(self._rows)
