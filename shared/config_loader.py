from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from shared.exceptions import ConfigError


load_dotenv(override=False)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except ValueError as e:
        raise ConfigError(f"must be an integer, got {raw!r}", field=name) from e
    if not (min_value <= val <= max_value):
        raise ConfigError(f"must be in [{min_value}, {max_value}], got {val}", field=name)
    return val


def _enum(name: str, default: str, *, choices: tuple[str, ...]) -> str:
    raw = os.getenv(name) or default
    if raw not in choices:
        raise ConfigError(f"must be one of {choices}, got {raw!r}", field=name)
    return raw


def _path_or_none(name: str) -> Path | None:
    raw = os.getenv(name)
    return Path(raw) if raw else None


_SHEET_ID_URL_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_.\-]+)")


def _database_url() -> str | None:
    raw = (os.getenv("DATABASE_URL") or "").strip()
    return raw or None


def _spreadsheet_id() -> str | None:
    """Read SPREADSHEET_ID (or a full sheet URL — we strip to the bare ID)."""
    raw = (os.getenv("SPREADSHEET_ID") or "").strip()
    if not raw:
        return None
    m = _SHEET_ID_URL_RE.search(raw)
    return m.group(1) if m else raw


@dataclass(frozen=True)
class SheetsHooksConfig:
    audit_enabled: bool
    audit_file: Path | None
    timestamp_column: str | None
    timestamp_format: Literal["iso", "epoch"]
    validate_enabled: bool


@dataclass(frozen=True)
class SheetsConfig:
    default_value_input: Literal["USER_ENTERED", "RAW"]
    default_value_render: Literal["FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"]
    hooks: SheetsHooksConfig


@dataclass(frozen=True)
class AppConfig:
    google_credentials_json: str | None
    google_application_credentials: Path | None
    sheets: SheetsConfig
    spreadsheet_id: str | None
    database_url: str | None
    log_level: str
    log_format: Literal["text", "json"]


def load_config() -> AppConfig:
    timestamp_col_raw = os.getenv("SHEETS_TIMESTAMP_COLUMN") or None

    hooks = SheetsHooksConfig(
        audit_enabled=_bool("SHEETS_AUDIT_ENABLED", False),
        audit_file=_path_or_none("SHEETS_AUDIT_FILE"),
        timestamp_column=timestamp_col_raw,
        timestamp_format=_enum("SHEETS_TIMESTAMP_FORMAT", "iso", choices=("iso", "epoch")),  # type: ignore[arg-type]
        validate_enabled=_bool("SHEETS_VALIDATE_ENABLED", False),
    )

    sheets = SheetsConfig(
        default_value_input=_enum(
            "SHEETS_DEFAULT_VALUE_INPUT", "USER_ENTERED",
            choices=("USER_ENTERED", "RAW"),
        ),  # type: ignore[arg-type]
        default_value_render=_enum(
            "SHEETS_DEFAULT_VALUE_RENDER", "FORMATTED_VALUE",
            choices=("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"),
        ),  # type: ignore[arg-type]
        hooks=hooks,
    )

    return AppConfig(
        google_credentials_json=os.getenv("GOOGLE_CREDENTIALS_JSON") or None,
        google_application_credentials=_path_or_none("GOOGLE_APPLICATION_CREDENTIALS"),
        sheets=sheets,
        spreadsheet_id=_spreadsheet_id(),
        database_url=_database_url(),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        log_format=_enum("LOG_FORMAT", "text", choices=("text", "json")),  # type: ignore[arg-type]
    )
