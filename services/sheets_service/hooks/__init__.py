from __future__ import annotations

from services.sheets_service.hooks.audit_hook import AuditHook
from services.sheets_service.hooks.base import Hook, HookContext, HookEvent
from services.sheets_service.hooks.timestamp_hook import TimestampHook
from services.sheets_service.hooks.validate_hook import ValidateHook

# NOTE: register_default_hooks is intentionally NOT re-exported here.
# Doing so would create a cycle: hook_registry imports hooks.base, which
# triggers hooks.__init__, which would then import registration, which
# imports hook_registry. Callers should import it directly:
#   from services.sheets_service.hooks.registration import register_default_hooks

__all__ = [
    "AuditHook", "Hook", "HookContext", "HookEvent",
    "TimestampHook", "ValidateHook",
]
