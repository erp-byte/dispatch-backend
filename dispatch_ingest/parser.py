"""Parse a Candor Foods invoice PDF into structured line items.

This module is regex-heavy because the underlying PyMuPDF text extraction
preserves line breaks unpredictably across Tally-rendered invoice templates.
Each extractor below is paired with a tolerance for both same-line and
split-across-lines forms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Optional

import fitz  # PyMuPDF — pinned >=1.23 for stable text-extraction behavior


# Word-boundary anchored so `CND/26-27/7444` doesn't accidentally match `…/744` prefix
_INVOICE_NO_RE = re.compile(r"\b(CND|CDPL|SR/CF)/\d{2}-\d{2}/\d+\b")
_DATE_RE = re.compile(r"\b(\d{1,2})-([A-Za-z]{3})-(\d{2})\b")
_AMOUNT_RE = re.compile(r"^[\d,]+\.\d{2}$")
_KGS_QTY_RE = re.compile(r"^([\d,]+\.\d+)\s*Kgs?$", re.IGNORECASE)
_KGS_PAREN_RE = re.compile(r"\(([\d,]+\.\d+)\s*Kgs?\)", re.IGNORECASE)
_CARTN_RE = re.compile(r"(\d+)\s*Cartn", re.IGNORECASE)
_BOXES_FREETEXT_RE = re.compile(r"(\d+)\s*(?:Box(?:es)?|Bags?)\b", re.IGNORECASE)

# Header tokens that PyMuPDF emits as standalone lines inside the goods table.
# Skipping them keeps `_extract_line_items` from confusing them with item starts.
_TABLE_HEADER_ARTIFACTS = (
    "Sl", "No.", "Amount", "Disc. %", "per", "Rate", "Quantity", "GST",
    "HSN/SAC", "Description of Goods", "Description of", "Services", "1.",
)

_CONSIGNEE_HEADERS = ("Consignee (Ship to)", "Dispatch To")

_MONTH_MAP = MappingProxyType({  # M5
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
    "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
    "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
})

# Canonical Indian GSTIN: 2 digit state, 5 letter PAN prefix, 4 digit PAN num,
# 1 letter PAN check, 1 alphanumeric entity, 'Z' literal, 1 alphanumeric check.
_GSTIN_RE = re.compile(r"\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z])\b")


@dataclass
class LineItem:
    article: str
    boxes: int
    net_wt_kg: float
    taxable_amount: float
    hsn_code: Optional[str] = None
    rate_per_unit: Optional[float] = None


@dataclass
class InvoiceData:
    invoice_no: str
    date_iso: str  # YYYY-MM-DD
    customer: str
    location: str
    po_no: Optional[str]
    transporter: Optional[str]
    lr_no: Optional[str]
    line_items: list[LineItem] = field(default_factory=list)
    total_taxable: float = 0.0
    total_gross: float = 0.0  # taxable + tax
    freight_charge: float = 0.0
    warehouse: Optional[str] = None
    pin_code: Optional[str] = None
    e_way_bill_no: Optional[str] = None
    customer_gstin: Optional[str] = None
    payment_terms: Optional[str] = None
    approx_distance_km: Optional[int] = None
    irn: Optional[str] = None
    invoice_type: str = "tax"

    @property
    def unique_articles(self) -> list[LineItem]:
        """Merge line items with identical article names (sum qty / amount).

        For HSN/rate we use `is None` checks instead of truthy `or` — so a
        legitimate rate of 0.0 (free sample) on the first item doesn't get
        silently replaced by the second item's value (I10).
        """
        by_article: dict[str, LineItem] = {}
        order: list[str] = []
        for li in self.line_items:
            key = li.article.strip().casefold()
            if key in by_article:
                e = by_article[key]
                by_article[key] = LineItem(
                    article=e.article,
                    boxes=e.boxes + li.boxes,
                    net_wt_kg=e.net_wt_kg + li.net_wt_kg,
                    taxable_amount=e.taxable_amount + li.taxable_amount,
                    hsn_code=e.hsn_code if e.hsn_code is not None else li.hsn_code,
                    rate_per_unit=(e.rate_per_unit if e.rate_per_unit is not None
                                   else li.rate_per_unit),
                )
            else:
                by_article[key] = li
                order.append(key)
        return [by_article[k] for k in order]


# ---------- public API ----------


def parse_pdf(path: str, *, keep_raw_text: bool = False) -> InvoiceData:
    """Parse a single invoice PDF into structured data.

    `keep_raw_text` is intentionally False by default — the full text held PII
    (GSTINs, IRNs, vehicle numbers) and was never used by downstream code (C6).
    Set to True only for debugging via a custom caller.
    """
    with fitz.open(path) as doc:
        pages = [p.get_text() for p in doc]
    full = "\n".join(pages)
    page1 = pages[0] if pages else ""

    inv_type = _detect_type(full)
    invoice_no = _extract_invoice_no(full)
    date_iso = _extract_date(full)
    customer, location, pin_code, customer_gstin = _extract_consignee_extended(page1)
    po_no = _extract_po(full)
    transporter, lr_no = _extract_transport(full)
    line_items = _extract_line_items(full)
    total_taxable = _extract_total_taxable(full, line_items)
    freight_charge = _extract_freight_charge(full)
    warehouse = _extract_warehouse(full)
    e_way_bill_no = _extract_e_way_bill(full)
    payment_terms = _extract_payment_terms(full)
    approx_distance_km = _extract_approx_distance(full)
    irn = _extract_irn(full)
    total_gross = _extract_total_gross(full)

    return InvoiceData(
        invoice_no=invoice_no,
        date_iso=date_iso,
        customer=customer,
        location=location,
        po_no=po_no,
        transporter=(transporter or None),
        lr_no=(lr_no or None),
        line_items=line_items,
        total_taxable=total_taxable,
        total_gross=total_gross,
        freight_charge=freight_charge,
        warehouse=warehouse,
        pin_code=pin_code,
        e_way_bill_no=e_way_bill_no,
        customer_gstin=customer_gstin,
        payment_terms=payment_terms,
        approx_distance_km=approx_distance_km,
        irn=irn,
        invoice_type=inv_type,
    )


# ---------- type detection ----------


def _detect_type(text: str) -> str:
    head = text[:200].upper()
    if "MATERIAL OUT" in head:
        return "material_out"
    if "CREDIT NOTE" in head:
        return "credit_note"
    if "SERVICE INVOICE" in head:
        return "service"
    if "TAX INVOICE" in head:
        return "tax"
    return "unknown"


# ---------- field extractors ----------


def _extract_invoice_no(text: str) -> str:
    m = _INVOICE_NO_RE.search(text)
    return m.group(0) if m else ""


def _extract_date(text: str) -> str:
    """First Dated value after the Invoice No block — that's the invoice date.

    I2: if the month abbreviation is not one we recognise, return "" rather
    than silently defaulting to January.
    """
    lines = text.splitlines()
    inv_idx = next((i for i, ln in enumerate(lines) if "Invoice No." in ln), 0)
    for i in range(inv_idx, min(inv_idx + 60, len(lines))):
        if lines[i].strip() == "Dated":
            for j in range(i + 1, min(i + 4, len(lines))):
                d = _parse_date_match(lines[j])
                if d:
                    return d
    d = _parse_date_match(text)
    return d or ""


def _parse_date_match(s: str) -> Optional[str]:
    m = _DATE_RE.search(s)
    if not m:
        return None
    day, mon, yr = m.group(1), m.group(2).capitalize(), m.group(3)
    mm = _MONTH_MAP.get(mon)
    if not mm:  # I2
        return None
    return f"20{yr}-{mm}-{int(day):02d}"


def _extract_consignee(text: str) -> tuple[str, str]:
    cust, loc, _pin, _gstin = _extract_consignee_extended(text)
    return cust, loc


def _consignee_header_index(lines: list[str]) -> int:
    """I3: tolerate trailing colon or whitespace on the Consignee/Dispatch header."""
    for h in _CONSIGNEE_HEADERS:
        for i, ln in enumerate(lines):
            stripped = ln.strip().rstrip(":")
            if stripped == h:
                return i
    return -1


def _extract_consignee_extended(text: str) -> tuple[str, str, Optional[str], Optional[str]]:
    """Customer name, location, pin code, and GSTIN from the Consignee block."""
    lines = text.splitlines()
    idx = _consignee_header_index(lines)
    if idx < 0:
        return "", "", None, None

    block: list[str] = []
    for j in range(idx + 1, min(idx + 12, len(lines))):
        s = lines[j].strip()
        if not s:
            continue
        if s.startswith("GSTIN") or s.startswith("Buyer (") or s.startswith("State Name"):
            break
        block.append(s)
    if not block:
        return "", "", None, None

    customer = block[0]
    location = _infer_location(block, lines, idx)
    pin: Optional[str] = None
    for line in block:
        m = re.search(r"\b(\d{6})\b", line)
        if m:
            pin = m.group(1)
            break

    gstin: Optional[str] = None
    for j in range(idx + 1, min(idx + 25, len(lines))):
        s = lines[j].strip()
        if not (s.startswith("GSTIN") or s.startswith("GSTIN/UIN")):
            continue
        m = _GSTIN_RE.search(s)
        if m:
            gstin = m.group(1)
            break
        # value may be on the immediate next non-empty line
        for k in range(j + 1, min(j + 3, len(lines))):
            nxt = lines[k].strip()
            if not nxt:
                continue
            m = _GSTIN_RE.search(nxt)
            if m:
                gstin = m.group(1)
            break
        break
    return customer, location, pin, gstin


def _infer_location(block: list[str], lines: list[str], idx: int) -> str:
    """Pick city/state from the address block. Prefer State Name when present.

    I4: tolerate hyphens / `& Code` separators in the state name.
    """
    for j in range(idx + 1, min(idx + 25, len(lines))):
        s = lines[j].strip()
        if not s.startswith("State Name"):
            continue
        m = re.search(r"State Name\s*:?\s*([A-Za-z][A-Za-z &.\-]*?)(?:\s*,|\s+Code)", s)
        if m:
            return m.group(1).strip()
        for k in range(j + 1, min(j + 3, len(lines))):
            nxt = lines[k].strip()
            if not nxt:
                continue
            nm = re.search(r":\s*([A-Za-z][A-Za-z &.\-]*?)(?:\s*,|\s+Code)", nxt)
            if nm:
                return nm.group(1).strip()
            break  # never bleed into Buyer block
        break
    # Fallback: last address line — pull out "City - PIN" pattern
    for line in reversed(block[1:]):
        m = re.search(r"([A-Za-z][A-Za-z .]+?)\s*-\s*\d{6}", line)
        if m:
            return m.group(1).strip().rstrip(",")
    return ""


# Known label words that may follow the PO field — used to reject them as values.
_NEXT_LABEL_TOKENS = ("Dispatch Doc No.", "Other References", "Dated", "Destination",
                      "Mode/Terms of Payment", "Buyer's Order No.", "Buyer’s Order No.",
                      "Delivery Note Date", "Reference No. & Date.")


def _extract_po(text: str) -> Optional[str]:
    """Value of 'Buyer's Order No.' — reject neighbouring labels as bogus values (I8)."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() not in ("Buyer's Order No.", "Buyer’s Order No."):
            continue
        for j in range(i + 1, min(i + 3, len(lines))):
            s = lines[j].strip()
            if not s:
                continue
            if any(s.startswith(tok) for tok in _NEXT_LABEL_TOKENS):
                break
            # require ≥3 char tokens with letters/digits/slash/hyphen only
            if re.match(r"^[A-Za-z0-9/_\-]{3,}$", s):
                return s
            break
    return None


_VEHICLE_RE = re.compile(r"\b([A-Z]{2}\s?\d{1,2}\s?[A-Z]{1,3}\s?\d{1,5})\b")


def _extract_transport(text: str) -> tuple[Optional[str], Optional[str]]:
    """Transporter name + vehicle number from e-Way Bill / Motor Vehicle No."""
    lines = text.splitlines()
    transporter: Optional[str] = None
    vehicle: Optional[str] = None

    for i, ln in enumerate(lines):
        if "Transportation Details" not in ln:
            continue
        for j in range(i + 1, min(i + 20, len(lines))):
            s = lines[j].strip()
            m = re.match(r"^Name\s*:\s*(.+)$", s)
            if m:
                transporter = m.group(1).strip()
                break
            if s == "Name":
                for k in range(j + 1, min(j + 3, len(lines))):
                    nxt = lines[k].strip()
                    if not nxt:
                        continue
                    if nxt.startswith(":"):
                        transporter = nxt.lstrip(": ").strip()
                    break
                break
        break

    for i, ln in enumerate(lines):
        if ln.strip() == "Motor Vehicle No.":
            for j in range(i + 1, min(i + 3, len(lines))):
                m = _VEHICLE_RE.search(lines[j])
                if m:
                    vehicle = m.group(1).replace(" ", "")
                    break
            break

    if not vehicle:
        for i, ln in enumerate(lines):
            if ln.strip().startswith("Vehicle No."):
                for j in range(i, min(i + 4, len(lines))):
                    m = _VEHICLE_RE.search(lines[j])
                    if m:
                        vehicle = m.group(1).replace(" ", "")
                        break
                if vehicle:
                    break

    return transporter, vehicle


# ---------- line item extraction ----------

_LINE_ITEM_START_RE = re.compile(r"^(\d+)\s+(.+)$")


def _looks_like_real_item_start(desc: str) -> bool:
    """Reject 'N %' GST/discount rows and 'N Cartn …' carton-count rows."""
    d = desc.strip()
    if not d or not any(c.isalpha() for c in d):
        return False
    lower = d.lower()
    if lower.startswith("cartn") or lower.startswith("cartons") or lower.startswith("cart"):
        return False
    if lower.startswith("box") or lower.startswith("bags") or lower.startswith("bag "):
        return False
    return True


def _extract_line_items(text: str) -> list[LineItem]:
    """Walk the goods/services table and emit a LineItem per `N <desc>` row."""
    lines = text.splitlines()
    items: list[LineItem] = []

    table_start = -1
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s in ("Description of Goods", "Description of"):
            window = " ".join(lines[i:i + 8])
            if "HSN/SAC" in window or "Rate" in window:
                table_start = i
                break
    if table_start < 0:
        return items

    end_markers = ("Total", "Amount Chargeable", "Tax Amount", "Tot.Taxable")
    item_blocks: list[list[str]] = []
    current: list[str] = []
    in_table = False
    item_count = 0

    for i in range(table_start, len(lines)):
        s = lines[i].strip()
        if s in _TABLE_HEADER_ARTIFACTS:
            continue
        if "continued to page" in s.lower():
            continue

        if not in_table:
            m = _LINE_ITEM_START_RE.match(s)
            if m and m.group(1) == "1" and _looks_like_real_item_start(m.group(2)):
                in_table = True
                item_count = 1
                current = [m.group(2).strip()]
            continue

        m = _LINE_ITEM_START_RE.match(s)
        if (m and m.group(1).isdigit() and int(m.group(1)) == item_count + 1
                and _looks_like_real_item_start(m.group(2))):
            if current:
                item_blocks.append(current)
            item_count += 1
            current = [m.group(2).strip()]
            continue
        if any(s.startswith(em) for em in end_markers):
            if current:
                item_blocks.append(current)
            break
        if ("(OUTPUT)" in s or s.startswith("CGST") or s.startswith("SGST")
                or s.startswith("IGST") or s.startswith("UTGST")):
            if current:
                item_blocks.append(current)
            break
        current.append(s)
    else:
        if current:
            item_blocks.append(current)

    for block in item_blocks:
        li = _parse_item_block(block)
        if li is not None:
            items.append(li)
    return items


def _parse_item_block(block: list[str]) -> Optional[LineItem]:
    """Turn a list of lines describing one item into a LineItem.

    I7: if BOTH amount and quantity are absent, return None — the row is junk
    and downstream code is better off ignoring it than recording zeros.
    """
    if not block:
        return None
    article_parts: list[str] = [block[0].strip()]
    amount: Optional[float] = None
    qty_kg: Optional[float] = None
    paren_kg: Optional[float] = None
    boxes: Optional[int] = None
    hsn: Optional[str] = None
    rate: Optional[float] = None
    seen_amount = False

    for line in block[1:]:
        s = line.strip()
        if not s:
            continue
        if not seen_amount and _AMOUNT_RE.match(s):
            amount = _to_float(s)
            seen_amount = True
            continue
        m = _KGS_QTY_RE.match(s)
        if m and qty_kg is None:
            qty_kg = _to_float(m.group(1))
            continue
        m = _KGS_PAREN_RE.search(s)
        if m and paren_kg is None:
            paren_kg = _to_float(m.group(1))
            continue
        m = _CARTN_RE.search(s)
        if m and boxes is None:
            try:
                boxes = int(m.group(1))
            except ValueError:
                pass
            continue
        m = _BOXES_FREETEXT_RE.search(s)
        if m and boxes is None:
            try:
                boxes = int(m.group(1))
            except ValueError:
                pass
            continue
        if re.match(r"^[\d,]+\.\d+\s*NOS$", s, re.IGNORECASE):
            continue
        if re.match(r"^\d+\s*%$", s):
            continue
        m = re.match(r"^(\d{6,10})$", s)
        if m and hsn is None:
            hsn = m.group(1)
            continue
        if re.match(r"^[\d,]+\.\d+$", s):
            if amount is None:
                amount = _to_float(s)
                seen_amount = True
            elif rate is None:
                rate = _to_float(s)
            continue
        if not seen_amount:
            article_parts.append(s)

    # I7: junk row — no amount, no kg/boxes/etc. Don't emit a zero LineItem.
    if amount is None and qty_kg is None and paren_kg is None and boxes is None:
        return None

    article = re.sub(r"\s+", " ", " ".join(article_parts)).strip()
    net_wt = qty_kg if qty_kg is not None else (paren_kg if paren_kg is not None else 0.0)
    return LineItem(
        article=article,
        boxes=boxes or 0,
        net_wt_kg=net_wt,
        taxable_amount=amount or 0.0,
        hsn_code=hsn,
        rate_per_unit=rate,
    )


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


# ---------- total taxable extraction ----------

_TOT_TAXABLE_RE = re.compile(r"Tot\.Taxable Amt\s*:?\s*([\d,]+\.\d{2})", re.IGNORECASE)


def _extract_total_taxable(text: str, line_items: list[LineItem]) -> float:
    m = _TOT_TAXABLE_RE.search(text)
    if m:
        return _to_float(m.group(1))
    return round(sum(li.taxable_amount for li in line_items), 2)


# Warehouse / dispatch unit code in the Remarks block.
# I21: require a hyphen between the prefix and the 3-digit unit number, and a
# word boundary on either side. Brackets are optional.
_WAREHOUSE_RE = re.compile(r"(?:^|[^A-Za-z0-9])\[?([WAE])-(\d{3})\]?(?:[^A-Za-z0-9]|$)")


def _extract_warehouse(text: str) -> Optional[str]:
    """Find the dispatch-unit code (e.g. 'W-202', 'A-185') in the Remarks block."""
    lines = text.splitlines()
    in_remarks = False
    block: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("Remarks:"):
            in_remarks = True
            tail = s[len("Remarks:"):].strip()
            if tail:
                block.append(tail)
            continue
        if in_remarks:
            if (s.startswith("Company") or s.startswith("Declaration") or s.startswith("for ")
                    or "PAN" in s or s.startswith("1. ") or s.startswith("2. ")):
                break
            if s:
                block.append(s)
    # Try both space-joined and concat-joined forms — PyMuPDF sometimes
    # splits a bracketed code like [A-185] across newlines, which the
    # space-joined form ("[A -185]") would miss.
    for blob in (" ".join(block), "".join(block)):
        m = _WAREHOUSE_RE.search(blob)
        if m:
            return f"{m.group(1).upper()}-{m.group(2)}"
    return None


_FREIGHT_LABEL_RE = re.compile(r"Freight\s*&\s*Transport Charges", re.IGNORECASE)
# I22: tolerate leading currency glyphs / spaces / 'INR' prefix
_FREIGHT_AMOUNT_RE = re.compile(r"([\d,]+\.\d{2})\s*$")


def _extract_freight_charge(text: str) -> float:
    """Capture the 'Freight & Transport Charges-Income' line value if present."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if not _FREIGHT_LABEL_RE.search(ln):
            continue
        for j in range(i + 1, min(i + 4, len(lines))):
            s = lines[j].strip()
            m = _FREIGHT_AMOUNT_RE.search(s)
            if m:
                return _to_float(m.group(1))
        # Same-line value fallback
        m = _FREIGHT_AMOUNT_RE.search(ln.strip())
        if m:
            return _to_float(m.group(1))
        break
    return 0.0


# ---------- additional invoice-level fields ----------

# I5: e-Way Bill is exactly 12 digits; tighten to avoid grabbing other long ids.
_E_WAY_BILL_RE = re.compile(r"\b(\d{12})\b")


def _extract_e_way_bill(text: str) -> Optional[str]:
    """Find the 12-digit e-Way Bill No. value following the labeled field."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if "e-Way Bill No." not in ln:
            continue
        m = re.search(r"e-Way Bill No\.\s*[:\s]*(\d{12})\b", ln)
        if m:
            return m.group(1)
        for j in range(i + 1, min(i + 8, len(lines))):
            s = lines[j].strip()
            m2 = _E_WAY_BILL_RE.fullmatch(s) or re.match(r"^(\d{12})\b", s)
            if m2:
                return m2.group(1)
        break
    return None


# I9: only accept payment-term values matching a known shape.
_PAYMENT_TERMS_RE = re.compile(
    r"^(?:\d+\s*Days?|Cash|Credit|UPI|Cheque|Bank\s+Transfer|Advance|COD|Net\s+\d+)\b",
    re.IGNORECASE,
)


def _extract_payment_terms(text: str) -> Optional[str]:
    """Value of 'Mode/Terms of Payment' field — accept only well-formed shapes."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() != "Mode/Terms of Payment":
            continue
        for j in range(i + 1, min(i + 4, len(lines))):
            s = lines[j].strip()
            if not s:
                continue
            if any(s.startswith(tok) for tok in _NEXT_LABEL_TOKENS):
                return None
            if _PAYMENT_TERMS_RE.match(s):
                return s
            return None
        break
    return None


_DISTANCE_RE = re.compile(r"(\d+)\s*KM", re.IGNORECASE)


def _extract_approx_distance(text: str) -> Optional[int]:
    """Approx Distance in km from the e-Way Bill section."""
    m = re.search(r"Approx Distance\s*[:\s]*\s*(\d+)\s*KM", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == "Approx Distance":
            for j in range(i + 1, min(i + 3, len(lines))):
                m2 = _DISTANCE_RE.search(lines[j])
                if m2:
                    return int(m2.group(1))
            break
    return None


def _extract_irn(text: str) -> Optional[str]:
    """64-char hex IRN, possibly split across two lines.

    I6: validate the final length (64 hex chars after stripping hyphens). Bail
    if the assembled value is suspiciously short — better None than corrupted.
    """
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() != "IRN":
            continue
        value = ""
        for j in range(i + 1, min(i + 4, len(lines))):
            s = lines[j].strip()
            if not s:
                continue
            if s.startswith(":"):
                s = s.lstrip(": ").strip()
            if re.match(r"^[a-f0-9-]+$", s):
                value += s
            else:
                break
        if value:
            normalized = value.replace("-", "")
            if len(normalized) == 64 and re.fullmatch(r"[a-f0-9]{64}", normalized):
                return normalized
            return None
        break
    return None


def _extract_total_gross(text: str) -> float:
    """Total amount including tax.

    C5 fix: a Tally-rendered invoice prints `Total` multiple times — once for
    the line-item subtotal (taxable), and again for the grand total after the
    tax breakdown. We scan all `Total` lines and return the LAST amount before
    'Amount Chargeable (in words)', which is always the grand total.
    """
    lines = text.splitlines()
    last_total: Optional[float] = None
    in_total_section = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if "Amount Chargeable" in s:
            break
        if s == "Total":
            in_total_section = True
            for j in range(i + 1, min(i + 4, len(lines))):
                m = re.search(r"([\d,]+\.\d{2})", lines[j])
                if m:
                    last_total = _to_float(m.group(1))
                    break
    return last_total or 0.0
