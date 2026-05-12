from __future__ import annotations

import json

from google.auth import default as google_auth_default
from google.oauth2.service_account import Credentials

from shared.config_loader import AppConfig
from shared.constants import ALL_SCOPES
from shared.exceptions import GoogleAuthError
from shared.logger import get_logger

log = get_logger("auth.credentials")


def load_credentials(config: AppConfig) -> Credentials:
    if config.google_credentials_json:
        try:
            info = json.loads(config.google_credentials_json)
        except json.JSONDecodeError as e:
            raise GoogleAuthError(f"GOOGLE_CREDENTIALS_JSON is not valid JSON: {e}") from e
        log.info("loading credentials from GOOGLE_CREDENTIALS_JSON env var")
        return Credentials.from_service_account_info(info, scopes=ALL_SCOPES)

    if config.google_application_credentials:
        log.info("loading credentials from file %s", config.google_application_credentials)
        return Credentials.from_service_account_file(
            str(config.google_application_credentials), scopes=ALL_SCOPES
        )

    log.info("falling back to Application Default Credentials")
    creds, _ = google_auth_default(scopes=ALL_SCOPES)
    return creds
