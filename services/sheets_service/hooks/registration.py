from __future__ import annotations

from services.sheets_service.hook_registry import HookHub
from services.sheets_service.hooks.audit_hook import AuditHook
from services.sheets_service.hooks.base import HookEvent
from services.sheets_service.hooks.timestamp_hook import TimestampHook
from services.sheets_service.hooks.validate_hook import ValidateHook
from shared.config_loader import AppConfig


def register_default_hooks(hub: HookHub, config: AppConfig) -> ValidateHook | None:
    """Register configured default hooks. Returns the ValidateHook instance if validation
    is enabled (so callers can register schemas on it), else None."""
    cfg = config.sheets.hooks
    if cfg.audit_enabled:
        audit = AuditHook(file_path=cfg.audit_file)
        for evt in (HookEvent.POST_APPEND, HookEvent.POST_UPDATE,
                    HookEvent.POST_CREATE, HookEvent.ON_ERROR):
            hub.register(evt, audit)

    if cfg.timestamp_column:
        ts = TimestampHook(column=cfg.timestamp_column, format=cfg.timestamp_format)
        hub.register(HookEvent.PRE_APPEND, ts)
        hub.register(HookEvent.PRE_UPDATE, ts)

    validator: ValidateHook | None = None
    if cfg.validate_enabled:
        validator = ValidateHook()
        hub.register(HookEvent.PRE_APPEND, validator)
        hub.register(HookEvent.PRE_UPDATE, validator)

    return validator
