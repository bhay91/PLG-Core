"""Phase 1 PPS Assistant research upload normalization.

The output is deliberately a plain, provisional review document.  This module
has no database or authoritative-record dependencies.
"""
from __future__ import annotations

import re
from typing import Any

from plg_core.intake.research_import import ResearchImportPackage


def _value(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.I | re.M)
    return match.group(1).strip() if match else ""


def review_from_text(text: str, *, filename: str = "") -> dict[str, Any]:
    """Extract only explicit labelled values; unknown values stay blank."""
    body = str(text or "")
    customer = {"name": _value(r"^(?:customer|name)\s*:\s*(.+)$", body),
                "company": _value(r"^company\s*:\s*(.+)$", body)}
    machine = {"manufacturer": _value(r"^(?:manufacturer|make)\s*:\s*(.+)$", body),
               "model": _value(r"^model\s*:\s*(.+)$", body),
               "year": _value(r"^year\s*:\s*(.+)$", body),
               "asset_type": _value(r"^(?:asset type|equipment)\s*:\s*(.+)$", body),
               "identifiers": []}
    for label, kind in (("vin", "AUTOMOTIVE_VIN"), ("pin", "PIN"), ("serial", "MACHINE_SERIAL")):
        value = _value(rf"^{label}\s*:\s*(.+)$", body)
        if value:
            machine["identifiers"].append({"type": kind, "value": value, "confidence": "MEDIUM"})
    needs = []
    for line in body.splitlines():
        match = re.match(r"^\s*(?:requested\s+part|part|need)\s*:\s*(.+?)(?:\s+qty\s*[:=]?\s*(\d+(?:\.\d+)?))?\s*$", line, re.I)
        if match:
            needs.append({"original_wording": match.group(1).strip(), "normalized_description": match.group(1).strip(), "quantity": match.group(2) or None, "confidence": "MEDIUM"})
    return {"status": "PROVISIONAL", "source": {"filename": filename, "page": None},
            "customer": customer, "machine": machine, "requested_needs": needs,
            "part_evidence": [], "supplier_evidence": [], "pricing_evidence": [],
            "fitment_evidence": [], "research_evidence": [{"summary": body[:2000], "confidence": "MEDIUM"}]}


def review_from_package(package: ResearchImportPackage) -> dict[str, Any]:
    """Map PPS_INTAKE_PACKAGE_V1 into the same provisional review shape."""
    data = package.model_dump(mode="json")
    machine = data["machine"]
    options = data.get("research_options", [])
    return {"status": "PROVISIONAL", "source": {"filename": data["source_pdf"]["filename"], "sha256": data["source_pdf"]["sha256"]},
            "customer": data["customer"], "machine": machine, "requested_needs": data["requested_needs"],
            "part_evidence": [{"need": o["requested_need_reference"], "oem_part_number": o.get("oem_part_number", ""), "cross_references": o.get("cross_reference_part_numbers", []), "evidence": o.get("part_number_evidence", ""), "confidence": o.get("confidence", "UNKNOWN")} for o in options],
            "supplier_evidence": [{"need": o["requested_need_reference"], "supplier": o.get("supplier", ""), "url": o.get("supplier_url"), "availability": "", "confidence": o.get("confidence", "UNKNOWN")} for o in options],
            "pricing_evidence": [{"need": o["requested_need_reference"], "unit_cost": o.get("tax_inclusive_unit_cost") or o.get("supplier_base_unit_price"), "currency": o.get("currency", "USD"), "shipping": data.get("estimated_inbound_freight"), "confidence": o.get("confidence", "UNKNOWN")} for o in options],
            "fitment_evidence": [{"need": o["requested_need_reference"], "claim": o.get("fitment_evidence", ""), "confidence": o.get("confidence", "UNKNOWN")} for o in options],
            "research_evidence": [o.get("source_evidence", {}) for o in options]}
