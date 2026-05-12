from __future__ import annotations

from typing import Any

from googleapiclient.discovery import build


class ClientFactory:
    """Lazy, per-process cache of Google API client objects."""

    def __init__(self, creds: Any):
        self._creds = creds
        self._cache: dict[str, Any] = {}

    @property
    def credentials(self) -> Any:
        return self._creds

    def sheets(self) -> Any:
        if "sheets" not in self._cache:
            self._cache["sheets"] = build(
                "sheets", "v4", credentials=self._creds, cache_discovery=False
            )
        return self._cache["sheets"]
