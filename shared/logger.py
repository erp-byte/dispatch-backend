import logging
import os
import re
import sys


_REDACT_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-~+/=]+"),
    # JWT-shaped strings (header.payload.signature, base64url segments).
    # Position before the JSON-field patterns so a JWT inside a JSON value
    # is scrubbed in full rather than leaving the signature intact.
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    re.compile(r'"(?:access_token|refresh_token|private_key|client_secret)"\s*:\s*"[^"]*"'),
    re.compile(r"(?i)(access_token|refresh_token|private_key|client_secret)=\S+"),
]
# Note: client_email is intentionally NOT redacted — it is logged on startup
# so the operator knows which SA email to share resources with. We redact
# client_secret, private_key, tokens, and JWT-shaped strings.


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for pat in _REDACT_PATTERNS:
            text = pat.sub("[REDACTED]", text)
        return text


_TEXT_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_JSON_FMT = '{"ts":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","msg":"%(message)s"}'


def _make_formatter() -> logging.Formatter:
    if os.getenv("LOG_FORMAT", "text").lower() == "json":
        return RedactingFormatter(_JSON_FMT)
    return RedactingFormatter(_TEXT_FMT)


def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if log.handlers:
        return log
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_make_formatter())
    log.addHandler(handler)
    log.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    log.propagate = False
    return log
