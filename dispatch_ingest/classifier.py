"""Route an invoice PDF (by filename) to a target sheet tab, or skip."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Route(str, Enum):
    CFPL = "CFPL"
    CDPL = "CDPL"
    SERVICE_CFPL = "SERVICE CFPL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class Classification:
    route: Route
    reason: str
    filename: str


_SKIP_MARKERS = (
    "[Not Final]",
    "[Cancelled]",
    "{Material Out}",
    "[Material Out]",
)
_CN_RE = re.compile(r"\{CN\d+\}")
_OLD_INVOICES_PREFIX = "Old Invoices/"


def classify(filename: str) -> Classification:
    """Determine destination tab from the invoice filename alone."""
    name_only = filename.split("/")[-1]

    if filename.startswith(_OLD_INVOICES_PREFIX) or "/Old Invoices/" in filename:
        return Classification(Route.SKIP, "old-invoices subfolder", filename)
    for marker in _SKIP_MARKERS:
        if marker in name_only:
            return Classification(Route.SKIP, f"marker {marker}", filename)
    if _CN_RE.search(name_only):
        return Classification(Route.SKIP, "credit note", filename)

    is_service = "[Service Bill]" in name_only or "{Service Bill}" in name_only
    is_cfpl = "[CFPL]" in name_only
    is_cdpl = "[CDPL]" in name_only

    if is_service and (is_cfpl or is_cdpl):
        return Classification(Route.SERVICE_CFPL, "service bill", filename)
    if is_cfpl:
        return Classification(Route.CFPL, "CFPL tag", filename)
    if is_cdpl:
        return Classification(Route.CDPL, "CDPL tag", filename)
    if name_only.lower().startswith("d-mart") or name_only.lower().startswith("dmart"):
        return Classification(Route.CFPL, "D-Mart untagged → CFPL", filename)
    return Classification(Route.SKIP, "no recognised tag", filename)
