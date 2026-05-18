from __future__ import annotations

import json
from typing import Any

from googleapiclient.errors import HttpError


class ConfigError(Exception):
    def __init__(self, message: str, field: str | None = None):
        self.field = field
        super().__init__(f"[{field}] {message}" if field else message)


class GoogleAuthError(Exception):
    pass


class HookAbort(Exception):
    def __init__(self, reason: str, hook_name: str):
        self.reason = reason
        self.hook_name = hook_name
        super().__init__(f"{hook_name}: {reason}")


_HINTS_BY_STATUS = {
    403: "Share the spreadsheet/file with the service account email above (Editor role).",
    404: "Verify the spreadsheet_id / file_id is correct and accessible to the service account.",
    400: "Check the request payload (range syntax, value types).",
    401: "Service-account credentials are invalid or expired. Re-issue the SA key.",
    429: "Rate limit hit. Reduce request frequency or request quota increase.",
}


class _GoogleApiError(Exception):
    code: str
    status: int
    reason: str
    message: str
    service_account_email: str | None
    hint: str

    def __init__(self, *, code: str, status: int, reason: str, message: str,
                 service_account_email: str | None = None, hint: str = ""):
        self.code = code
        self.status = status
        self.reason = reason
        self.message = message
        self.service_account_email = service_account_email
        self.hint = hint
        super().__init__(f"{code} ({status}): {message}")

    @classmethod
    def from_http(cls, err: HttpError, *, service_account_email: str | None = None) -> "_GoogleApiError":
        status = getattr(err.resp, "status", 0) or 0
        try:
            content = json.loads(err.content.decode() if isinstance(err.content, bytes) else err.content)
            inner = content.get("error", {})
            code = inner.get("status", "UNKNOWN")
            message = inner.get("message", str(err))
        except Exception:
            code = "UNKNOWN"
            message = str(err)
        reason = getattr(err.resp, "reason", "") or ""
        return cls(
            code=code, status=int(status), reason=str(reason), message=message,
            service_account_email=service_account_email,
            hint=_HINTS_BY_STATUS.get(int(status), ""),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status,
            "reason": self.reason,
            "message": self.message,
            "service_account_email": self.service_account_email,
            "hint": self.hint,
        }


class SheetsError(_GoogleApiError):
    pass


class DriveError(_GoogleApiError):
    pass
