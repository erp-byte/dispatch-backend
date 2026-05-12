# Dispatch MCP Server

MCP (Model Context Protocol) server exposing Google Sheets and Drive as typed tools for Claude.
Service-account auth, stdio transport, hook-extensible Sheets surface.

## Setup

```bash
cd D:\CANDOR\DISPATCH\server
pip install -e ".[dev]"
cp .env.example .env
```

Set **one** of the following in `.env`:
- `GOOGLE_CREDENTIALS_JSON` — raw service-account JSON blob (preferred for Render/Docker)
- `GOOGLE_APPLICATION_CREDENTIALS` — path to a service-account JSON file

Then **share the spreadsheet/folder** with the service account's `client_email` (Editor role).

## Run

```bash
dispatch-mcp-server
```

It will exit immediately if credentials don't pass the startup self-test (one `drive.about.get` call).

## Register with Claude Code

```bash
claude mcp add dispatch dispatch-mcp-server
```

## Tools

### Sheets
- `sheets_list_tabs`
- `sheets_read_range`
- `sheets_append_rows` *(hooks: pre_append, post_append)*
- `sheets_update_range` *(hooks: pre_update, post_update)*
- `sheets_clear_range`
- `sheets_create_spreadsheet`
- `sheets_add_tab`
- `sheets_format_cells`

### Drive
- `drive_list_files`
- `drive_search_files`
- `drive_read_file`
- `drive_upload_file` *(resumable above `DRIVE_UPLOAD_RESUMABLE_THRESHOLD_MB`)*
- `drive_copy_file`
- `drive_move_file`
- `drive_set_permissions`

## Hooks

Sheets writes pass through a `HookHub`. Built-in hooks (off unless toggled):

| Hook | Trigger env | Behavior |
|---|---|---|
| `AuditHook` | `SHEETS_AUDIT_ENABLED=true` | Logs every write/error to stderr (and `SHEETS_AUDIT_FILE` if set). |
| `TimestampHook` | `SHEETS_TIMESTAMP_COLUMN=H` | Auto-fills column H with current time. `SHEETS_TIMESTAMP_FORMAT=iso\|epoch`. |
| `ValidateHook` | `SHEETS_VALIDATE_ENABLED=true` | Validates rows against per-spreadsheet pydantic schema. |

Custom hooks: subclass `Hook` (`services/sheets_service/hooks/base.py`) and register on the `HookHub` returned from `main`. A `pre_*` hook may `raise HookAbort(reason, hook_name)` to short-circuit.

## Error format

Every Google API failure becomes a structured payload:

```json
{
  "code": "PERMISSION_DENIED",
  "status": 403,
  "reason": "forbidden",
  "message": "The caller does not have permission",
  "service_account_email": "dispatch-sa@…iam.gserviceaccount.com",
  "hint": "Share the spreadsheet/file with the service account email above (Editor role)."
}
```

`HookAbort` becomes `{ "code": "HOOK_ABORTED", "hook": "...", "reason": "..." }`.

## Tests

```bash
pytest -v                    # hermetic unit suite (no network)
RUN_LIVE_TESTS=1 pytest -v   # opt-in live integration (requires creds)
```

## Layout

```
main.py
shared/   logger, exceptions, config_loader, constants
services/
  auth_service/    credentials, client_factory, scopes
  sheets_service/  manager, tools, models, hook_registry, hooks/
  drive_service/   manager, tools, models
server/   app, registry
```

See `docs/superpowers/specs/2026-05-07-mcp-server-design.md` for the full design.
