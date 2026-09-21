from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import tempfile

from fastapi import HTTPException


MAX_PDF_PAGES = 40
MAX_EXTRACTED_CHARACTERS = 100_000
PDF_TIMEOUT_SECONDS = 15
DOCUMENT_TYPES = {
    "CUSTOMER_REQUEST", "CUSTOMER_QUOTE", "SUPPLIER_QUOTE", "PPS_QUOTE",
    "PPS_INVOICE", "SUPPLIER_INVOICE", "OTHER",
}


@dataclass(frozen=True)
class PDFExtraction:
    text: str
    page_count: int
    status: str
    evidence: str


def extract_pdf_text(data: bytes) -> PDFExtraction:
    if not data.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="The uploaded PDF has an invalid signature.")
    with tempfile.TemporaryDirectory(prefix="pps-intake-pdf-") as directory:
        source = Path(directory) / "source.pdf"
        output = Path(directory) / "source.txt"
        source.write_bytes(data)
        try:
            info = subprocess.run(
                ["pdfinfo", str(source)], capture_output=True, text=True,
                timeout=PDF_TIMEOUT_SECONDS, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(status_code=400, detail="PPS could not safely inspect this PDF.") from exc
        if info.returncode != 0:
            raise HTTPException(status_code=400, detail="The uploaded PDF is malformed or unsupported.")
        encrypted = re.search(r"^Encrypted:\s*(\S+)", info.stdout, re.I | re.M)
        if encrypted and encrypted.group(1).lower() not in {"no", "false"}:
            raise HTTPException(status_code=400, detail="Password-protected or encrypted PDFs are not supported.")
        pages_match = re.search(r"^Pages:\s*(\d+)", info.stdout, re.M)
        if not pages_match:
            raise HTTPException(status_code=400, detail="PPS could not determine the PDF page count.")
        pages = int(pages_match.group(1))
        if pages < 1 or pages > MAX_PDF_PAGES:
            raise HTTPException(
                status_code=400,
                detail=f"PDFs must contain between 1 and {MAX_PDF_PAGES} pages.",
            )
        try:
            extracted = subprocess.run(
                ["pdftotext", "-enc", "UTF-8", str(source), str(output)],
                capture_output=True, text=True, timeout=PDF_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return PDFExtraction("", pages, "UNAVAILABLE", "PDF text extraction failed safely.")
        if extracted.returncode != 0 or not output.is_file():
            return PDFExtraction("", pages, "UNAVAILABLE", "PDF contains no safely extractable text.")
        raw = output.read_text(encoding="utf-8", errors="replace")
        bounded = raw[:MAX_EXTRACTED_CHARACTERS].strip()
        truncated = len(raw) > MAX_EXTRACTED_CHARACTERS
        if not bounded:
            return PDFExtraction("", pages, "UNAVAILABLE", "PDF contains no extractable text; OCR was not attempted.")
        evidence = f"Extracted local text from {pages} PDF page{'s' if pages != 1 else ''}."
        if truncated:
            evidence += f" Text was bounded to {MAX_EXTRACTED_CHARACTERS:,} characters."
        return PDFExtraction(bounded, pages, "PROPOSED", evidence)


def _first(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.I | re.M)
    return match.group(1).strip() if match else ""


def classify_document(text: str, filename: str = "") -> dict:
    body = str(text or "")
    combined = f"{filename}\n{body}"
    pps_invoice = _first(r"\b(PPS-INV-\d{4,})\b", combined)
    pps_quote = _first(r"\b(PPS-Q-\d{4,})\b", combined)
    if pps_invoice:
        kind, number, rationale = "PPS_INVOICE", pps_invoice.upper(), "Recognized a PPS Invoice number."
    elif pps_quote:
        kind, number, rationale = "PPS_QUOTE", pps_quote.upper(), "Recognized a PPS Quote number."
    else:
        lowered = body.lower()
        supplier = bool(re.search(r"\b(?:supplier|vendor)\b", lowered))
        invoice = bool(re.search(r"(?:^|\n)\s*invoice\b|\b(?:supplier invoice|vendor invoice|invoice no\.?|invoice #)\b", lowered))
        quotation = bool(re.search(r"(?:^|\n)\s*(?:quote|quotation)\b|\b(?:supplier quote|vendor quote|quotation|quote no\.?|quote #)\b", lowered))
        request = bool(re.search(r"\b(?:requested parts|customer request|please source|\bneeds?\b)\b", lowered))
        customer_quote = bool(re.search(r"\b(?:customer quote|bill to|quoted items)\b", lowered))
        bill_to = bool(re.search(r"\b(?:bill(?:ed)? to|sold to|customer|account)\b", lowered))
        if invoice and (supplier or bill_to):
            kind, rationale = "SUPPLIER_INVOICE", "Supplier/vendor wording and invoice language were found."
        elif supplier and quotation:
            kind, rationale = "SUPPLIER_QUOTE", "Supplier/vendor wording and quote language were found."
        elif customer_quote and quotation:
            kind, rationale = "CUSTOMER_QUOTE", "Customer-facing and quote language were found."
        elif request:
            kind, rationale = "CUSTOMER_REQUEST", "Customer request or requested-need language was found."
        else:
            kind, rationale = "OTHER", "Document type could not be determined safely."
        number = _first(r"\b(?:quote|quotation|invoice)\s*(?:no\.?|number|#|:)\s*([A-Z0-9-]+)", body)
    return {
        "document_type": kind,
        "document_number": number,
        "review_required": kind == "OTHER",
        "rationale": rationale,
        "evidence": body[:800].strip(),
    }


def supplier_quote_fields(text: str) -> dict:
    supplier = _first(r"^(?:supplier|vendor)\s*:\s*(.+)$", text)
    reference = _first(r"^(?:quote|quotation)\s*(?:no\.?|number|#|:)\s*(.+)$", text)
    date = _first(r"^(?:quote\s+)?date\s*:\s*(.+)$", text)
    currency = _first(r"\b(USD|JMD|CAD|EUR|GBP)\b", text).upper()
    job_number = _first(r"\b(PPS-J-\d{4,})\b", text).upper()
    request_number = _first(r"\b(PPS-R-\d{4,})\b", text).upper()
    lines = []
    pattern = re.compile(
        r"^[ \t]*([A-Z0-9][A-Z0-9._/-]{2,})[ \t]+(.+?)[ \t]+(\d+(?:\.\d+)?)[ \t]+\$?([\d,]+(?:\.\d{1,2})?)[ \t]+\$?([\d,]+(?:\.\d{1,2})?)[ \t]*$",
        re.M,
    )
    for match in pattern.finditer(text):
        lines.append({
            "supplier_part_number": match.group(1),
            "description": match.group(2).strip(),
            "quantity": float(match.group(3)),
            "unit_cost": float(match.group(4).replace(",", "")),
            "line_total": float(match.group(5).replace(",", "")),
            "evidence": match.group(0).strip(),
        })
    return {
        "supplier": supplier, "reference": reference, "date": date,
        "currency": currency, "job_number": job_number,
        "request_number": request_number, "lines": lines,
    }


_SECTION_LABELS = (
    "bill to", "billed to", "sold to", "customer", "account", "ship to",
    "invoice", "invoice date", "date", "ship via", "terms", "description", "item",
    "part number", "qty", "quantity", "subtotal", "total",
)


def _clean_lines(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", line).strip(" |\t") for line in str(text or "").splitlines() if line.strip(" |\t")]


def _section(lines: list[str], labels: tuple[str, ...]) -> list[str]:
    start = None
    first_value = ""
    for index, line in enumerate(lines):
        match = re.match(rf"^(?:{'|'.join(re.escape(label) for label in labels)})\s*:?[ \t]*(.*)$", line, re.I)
        if match:
            start = index + 1
            first_value = match.group(1).strip()
            break
    if start is None:
        return []
    values = [first_value] if first_value else []
    for line in lines[start:]:
        normalized = line.lower().rstrip(":")
        if any(normalized == label or normalized.startswith(label + ":") for label in _SECTION_LABELS):
            break
        if re.match(r"^(?:invoice|quote)\s*(?:no\.?|#|number|date)\b", line, re.I):
            break
        values.append(line)
    return values


def _looks_company(line: str) -> bool:
    return bool(re.search(
        r"\b(?:inc(?:orporated)?|llc|ltd|limited|corp(?:oration)?|company|co|dev(?:elopment)?|construction|machinery|parts|services|group)\.?\b",
        line, re.I,
    ))


def supplier_invoice_fields(text: str) -> dict:
    """Extract issuer and Bill-To roles without treating the header as customer data."""
    lines = _clean_lines(text)
    bill_to = _section(lines, ("bill to", "billed to", "sold to", "customer", "account"))
    ship_to = _section(lines, ("ship to",))
    # PDF column extraction often emits both headings first, followed by the
    # left and right address columns. Reconstruct only when the two blocks are
    # exactly repeated; do not otherwise substitute Ship To for Bill To.
    if not bill_to and ship_to and len(ship_to) % 2 == 0:
        midpoint = len(ship_to) // 2
        if ship_to[:midpoint] == ship_to[midpoint:]:
            bill_to, ship_to = ship_to[:midpoint], ship_to[midpoint:]
    bill_company = next((line for line in bill_to if _looks_company(line)), "")
    bill_person = next((
        line for line in bill_to
        if line != bill_company
        and re.fullmatch(r"[A-Za-z][A-Za-z'.-]+(?:\s+[A-Za-z][A-Za-z'.-]+){1,3}", line)
        and not re.search(r"\b(?:street|st|avenue|ave|road|rd|drive|dr|fl|ny|ca|zip)\b", line, re.I)
    ), "")
    bill_address = "\n".join(line for line in bill_to if line not in {bill_company, bill_person})

    explicit_supplier = _section(lines, ("supplier", "vendor", "seller", "from", "issued by"))
    bill_index = next((index for index, line in enumerate(lines) if re.match(r"^(?:bill to|billed to|sold to|customer|account)\b", line, re.I)), len(lines))
    header = lines[:bill_index]
    supplier_company = next((line for line in explicit_supplier if _looks_company(line)), "")
    if not supplier_company:
        supplier_company = next((
            line for line in header
            if _looks_company(line)
            and not re.match(r"^(?:invoice|statement|date|tel|phone|fax|www\.|https?://)", line, re.I)
        ), "")
    supplier_phone = _first(r"\b(?:TEL(?:EPHONE)?|PHONE)\s*#?\s*:?[ \t]*([+()\d][\d ()-]{6,}\d)", text)
    supplier_email = _first(r"\b([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})\b", "\n".join(header))
    supplier_website = _first(r"\b((?:https?://|www\.)\S+)", "\n".join(header))
    supplier_address = "\n".join(
        line for line in header
        if line != supplier_company
        and not re.match(r"^(?:invoice|statement|date|tel|phone|fax|www\.|https?://)", line, re.I)
        and not re.fullmatch(r"\d{3,}", line)
    )
    invoice_number = ""
    invoice_label = next((index for index, line in enumerate(lines) if re.fullmatch(r"invoice\s*(?:no\.?|number|#)", line, re.I)), None)
    if invoice_label is not None:
        following = []
        for line in lines[invoice_label + 1:invoice_label + 7]:
            if re.match(r"^(?:bill to|ship to|terms|quantity|item)\b", line, re.I):
                break
            following.append(line)
        invoice_number = next((line for line in following if re.fullmatch(r"\d{4,}", line)), "")
    invoice_number = invoice_number or (
        _first(r"\binvoice\s*(?:no\.?|number|#|:)\s*([A-Z0-9-]+)", text)
        or _first(r"(?:^|\n)\s*invoice\s*\n\s*([A-Z0-9-]+)", text)
    )
    invoice_date = _first(r"^(?:invoice\s+)?date\s*:\s*(.+)$", text)
    currency = _first(r"\b(USD|JMD|CAD|EUR|GBP)\b", text).upper()
    total_text = (
        _first(r"^\s*(?:invoice\s+)?total\s*:?[ \t]*\$?([\d,]+(?:\.\d{2})?)\s*$", text)
        or _first(r"^\s*(?:invoice\s+)?total\s*$\s*^\s*\$?([\d,]+(?:\.\d{2})?)\s*$", text)
    )
    total = float(total_text.replace(",", "")) if total_text else None

    items = supplier_quote_fields(text)["lines"]
    if not items:
        money_pattern = re.compile(r"^\$?([\d,]+\.\d{2})$")
        for index, line in enumerate(lines):
            if not re.fullmatch(r"(?=.*\d)[A-Z0-9][A-Z0-9._/-]{2,}", line, re.I):
                continue
            if line == invoice_number or re.fullmatch(r"\d{5,}", line):
                continue
            description = lines[index + 1] if index + 1 < len(lines) else ""
            price = None
            price_evidence = ""
            for candidate in lines[index + 2:index + 7]:
                match = money_pattern.match(candidate)
                if match:
                    price = float(match.group(1).replace(",", ""))
                    price_evidence = candidate
                    break
            if description and price is not None and not re.match(r"^(?:subtotal|total|tax|shipping)\b", description, re.I):
                items.append({
                    "supplier_part_number": line, "description": description,
                    "quantity": 1.0, "unit_cost": price, "line_total": price,
                    "evidence": " · ".join((line, description, price_evidence)),
                })
    if not items:
        quantity_parts = []
        for index, line in enumerate(lines):
            match = re.fullmatch(r"(\d+(?:\.\d+)?)\s+([A-Z0-9][A-Z0-9._/-]{2,})", line, re.I)
            if match and float(match.group(1)) > 0:
                quantity_parts.append((index, float(match.group(1)), match.group(2)))
        description_index = next((index for index, line in enumerate(lines) if line.lower() == "description"), None)
        total_index = next((index for index, line in enumerate(lines) if line.lower() == "total"), len(lines))
        for _index, quantity, part_number in quantity_parts:
            search = lines[description_index + 1:total_index] if description_index is not None else lines[_index + 1:total_index]
            prices = [float(match.group(1).replace(",", "")) for line in search if (match := re.fullmatch(r"\$?([\d,]+\.\d{2})", line))]
            description = next((
                line for line in search
                if re.search(r"[A-Za-z]", line)
                and not re.match(r"^(?:rate|amount|sales tax|transaction|invoice number|balance due|subtotal|total|please read)\b", line, re.I)
            ), "").rstrip("`")
            if description and prices:
                items.append({
                    "supplier_part_number": part_number, "description": description,
                    "quantity": quantity, "unit_cost": prices[0],
                    "line_total": round(prices[0] * quantity, 2),
                    "evidence": f"{quantity:g} {part_number} · {description} · ${prices[0]:,.2f}",
                })
    return {
        "supplier": supplier_company,
        "supplier_address": supplier_address,
        "supplier_phone": re.sub(r"\D", "", supplier_phone),
        "supplier_email": supplier_email,
        "supplier_website": supplier_website,
        "supplier_invoice_number": invoice_number,
        "reference": invoice_number,
        "date": invoice_date,
        "currency": currency,
        "customer_person": bill_person,
        "customer_company": bill_company,
        "customer_address": bill_address,
        "bill_to_evidence": "\n".join(bill_to),
        "ship_to": "\n".join(ship_to),
        "lines": items,
        "total": total,
    }
