SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
# Backward-compat alias. The Drive service has been removed, but the auth
# resolver still imports ALL_SCOPES — keep it pointing at the sheets-only set.
ALL_SCOPES = list(SHEETS_SCOPES)

SHEETS_VALUE_INPUT_OPTIONS = ("USER_ENTERED", "RAW")
SHEETS_VALUE_RENDER_OPTIONS = ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")


# -----------------------------------------------------------------------------
# Database (Postgres mirror) — connection pool settings
# -----------------------------------------------------------------------------
DB_POOL_MIN_SIZE: int = 1
DB_POOL_MAX_SIZE: int = 5
DB_COMMAND_TIMEOUT_S: float = 10.0
DB_APPLICATION_NAME: str = "dispatch-mcp"
