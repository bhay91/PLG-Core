from __future__ import annotations

import json
import re
import sqlite3
from urllib.parse import quote_plus, urlparse

from fastapi import HTTPException

SOURCE_TYPES = (
    "OEM_CATALOG", "DEALER_PORTAL", "AFTERMARKET_CATALOG", "SUPPLIER",
    "VIN_OR_ASSET_DECODER", "GENERAL_RESEARCH", "SAVED_PPS_SOURCE", "OTHER",
)
TRUST_LEVELS = ("OEM_VERIFIED", "SUPPLIER_VERIFIED", "NEEDS_REVIEW")
CONNECTOR_TYPES = ("CART", "CATALOG", "UPLOAD", "LINK")
ALLOWED_TEMPLATE_FIELDS = {"identifier", "need", "manufacturer", "model"}


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def validate_source_url(value: str, *, allow_blank: bool = True) -> str:
    value = str(value or "").strip()
    if not value and allow_blank:
        return ""
    if len(value) > 2048:
        raise HTTPException(status_code=400, detail="Source URL is too long.")
    # Validate a template after replacing only explicitly supported placeholders.
    candidate = value
    for field in ALLOWED_TEMPLATE_FIELDS:
        candidate = candidate.replace("{" + field + "}", "context")
    if re.search(r"\{[^{}]+\}", candidate):
        raise HTTPException(status_code=400, detail="Source URL contains an unsupported context placeholder.")
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Source URLs must use HTTP or HTTPS.")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Source URLs may not contain embedded credentials.")
    return value


def build_launch_url(source, context: dict) -> str:
    template = validate_source_url(source["launch_url"] or "")
    if not template:
        return ""
    values = {
        "identifier": context.get("identifier", ""),
        "need": context.get("need", ""),
        "manufacturer": context.get("manufacturer", ""),
        "model": context.get("model", ""),
    }
    for key, value in values.items():
        template = template.replace("{" + key + "}", quote_plus(str(value or "")))
    return validate_source_url(template, allow_blank=False)


def _csv(value: str) -> set[str]:
    return {_normalize(part) for part in str(value or "").split(",") if part.strip()}


def canonical_asset_category(value: str) -> str:
    """Return the routing family for equivalent operator-facing asset labels."""
    normalized = _normalize(value)
    if normalized in {"machine", "equipment", "heavy equipment"}:
        return "machine"
    if normalized in {"vehicle", "automotive", "car", "truck"}:
        return "vehicle"
    return normalized


def _source_matches(row, *, manufacturer: str, asset_category: str, market: str) -> tuple[bool, int]:
    manufacturer_rules = _csv(row["manufacturer_applicability"])
    asset_rules = {
        canonical_asset_category(rule)
        for rule in _csv(row["asset_category_applicability"])
    }
    market_rules = _csv(row["market_applicability"])
    normalized_make = _normalize(manufacturer)
    normalized_asset = canonical_asset_category(asset_category)
    normalized_market = _normalize(market)
    if manufacturer_rules and normalized_make not in manufacturer_rules:
        return False, 0
    if asset_rules and normalized_asset not in asset_rules:
        return False, 0
    # GLOBAL describes a source that is usable across markets. It therefore
    # remains eligible when an asset's market is UNKNOWN, GLOBAL, or a known
    # regional market; regional sources still require an exact market match.
    if market_rules and "global" not in market_rules and normalized_market not in market_rules:
        return False, 0
    # Legacy automotive profiles without explicit applicability remain vehicle-only.
    category = str(row["category"] or "").lower()
    if not asset_rules and "automotive" in category and normalized_asset not in {"vehicle", "automotive"}:
        return False, 0
    # Legacy sources with no applicability/type are directory entries, not automatic recommendations.
    source_type = str(row["source_type"] or "OTHER").upper()
    if source_type == "OTHER" and not (manufacturer_rules or asset_rules or market_rules):
        return False, 0
    specificity = (300 if manufacturer_rules else 0) + (80 if asset_rules else 0) + (30 if market_rules else 0)
    if source_type == "GENERAL_RESEARCH":
        specificity = -100
    return True, specificity


def list_sources_for_context(
    connection: sqlite3.Connection, *, manufacturer: str, asset_category: str, market: str = "UNKNOWN"
) -> list[dict]:
    candidates = []
    rows = connection.execute(
        "SELECT * FROM connector_profiles WHERE is_enabled=1 AND COALESCE(is_archived,0)=0 ORDER BY id"
    ).fetchall()
    for row in rows:
        matches, specificity = _source_matches(
            row, manufacturer=manufacturer, asset_category=asset_category, market=market
        )
        if not matches:
            continue
        item = dict(row)
        item["routing_score"] = specificity
        try:
            item["safe_launch_url"] = validate_source_url(item["launch_url"] or "")
        except HTTPException:
            # Legacy/imported values are untrusted configuration. One unsafe
            # row must not break the Job workspace or become launchable.
            continue
        candidates.append(item)
    candidates.sort(
        key=lambda row: (
            -int(row["is_default"] or 0), -int(row["source_priority"] or 0),
            -int(row["routing_score"]), int(row["sort_order"] or 100),
            str(row["display_name"]).lower(),
        )
    )
    return candidates


def create_source(
    connection: sqlite3.Connection, *, display_name: str, source_type: str,
    launch_url: str = "", manufacturer_applicability: str = "",
    asset_category_applicability: str = "", market_applicability: str = "",
    notes: str = "", provenance: str = "OPERATOR_CONFIRMED", is_default: bool = False,
    category: str = "", connector_type: str = "CATALOG",
    trust_level: str = "NEEDS_REVIEW", source_priority: int = 100,
    is_enabled: bool = True, is_archived: bool = False,
) -> int:
    display_name = str(display_name or "").strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="Source name is required.")
    source_type = str(source_type or "OTHER").upper()
    if source_type not in SOURCE_TYPES:
        raise HTTPException(status_code=400, detail="Invalid source type.")
    connector_type = str(connector_type or "CATALOG").upper()
    if connector_type not in CONNECTOR_TYPES:
        raise HTTPException(status_code=400, detail="Invalid connector type.")
    trust_level = str(trust_level or "NEEDS_REVIEW").upper()
    if trust_level not in TRUST_LEVELS:
        raise HTTPException(status_code=400, detail="Invalid trust level.")
    try:
        source_priority = int(source_priority)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Priority must be a whole number.") from error
    launch_url = validate_source_url(launch_url)
    category = str(category or source_type.replace("_", " ").title()).strip()
    existing = connection.execute(
        "SELECT * FROM connector_profiles WHERE lower(trim(display_name))=lower(trim(?)) ORDER BY id LIMIT 1",
        (display_name,),
    ).fetchone()
    if existing is not None:
        # One logical name maps to one master directory record. Job Workspace
        # saves reuse it without silently replacing Admin-managed applicability.
        return int(existing["id"])
    key_base = re.sub(r"[^a-z0-9]+", "_", display_name.lower()).strip("_") or "source"
    key = key_base
    suffix = 2
    while connection.execute("SELECT 1 FROM connector_profiles WHERE connector_key=?", (key,)).fetchone():
        key = f"{key_base}_{suffix}"
        suffix += 1
    source_id = int(connection.execute(
        """INSERT INTO connector_profiles (
            connector_key,display_name,category,trust_level,launch_url,connector_type,
            parser_key,is_enabled,is_archived,sort_order,manufacturer_applicability,notes,
            source_type,asset_category_applicability,market_applicability,source_priority,
            is_default,provenance
        ) VALUES (?,?,?,?,?,?,'',?,?,100,?,?,?,?,?,?,?,?)""",
        (key, display_name, category, trust_level, launch_url, connector_type,
         int(bool(is_enabled)), int(bool(is_archived)),
         str(manufacturer_applicability or "").strip(), str(notes or "").strip(), source_type,
         str(asset_category_applicability or "").strip(), str(market_applicability or "").strip(),
         source_priority, int(bool(is_default)), str(provenance or "OPERATOR_CONFIRMED").strip()),
    ).lastrowid)
    return source_id


def update_source(
    connection: sqlite3.Connection, source_id: int, **values,
) -> int:
    current = connection.execute(
        "SELECT * FROM connector_profiles WHERE id=?", (source_id,)
    ).fetchone()
    if current is None:
        raise HTTPException(status_code=404, detail="Source not found.")
    display_name = str(values.get("display_name") or "").strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="Source name is required.")
    duplicate = connection.execute(
        "SELECT id FROM connector_profiles WHERE lower(trim(display_name))=lower(trim(?)) AND id<>?",
        (display_name, source_id),
    ).fetchone()
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="A source with this name already exists.")
    source_type = str(values.get("source_type") or "OTHER").upper()
    connector_type = str(values.get("connector_type") or "CATALOG").upper()
    trust_level = str(values.get("trust_level") or "NEEDS_REVIEW").upper()
    if source_type not in SOURCE_TYPES:
        raise HTTPException(status_code=400, detail="Invalid source type.")
    if connector_type not in CONNECTOR_TYPES:
        raise HTTPException(status_code=400, detail="Invalid connector type.")
    if trust_level not in TRUST_LEVELS:
        raise HTTPException(status_code=400, detail="Invalid trust level.")
    try:
        priority = int(values.get("source_priority", 100))
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Priority must be a whole number.") from error
    connection.execute(
        """UPDATE connector_profiles SET display_name=?,launch_url=?,category=?,trust_level=?,
                  connector_type=?,manufacturer_applicability=?,asset_category_applicability=?,
                  market_applicability=?,notes=?,source_type=?,source_priority=?,is_default=?,
                  is_enabled=?,is_archived=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (display_name, validate_source_url(values.get("launch_url") or ""),
         str(values.get("category") or "Supplier").strip(), trust_level, connector_type,
         str(values.get("manufacturer_applicability") or "").strip(),
         str(values.get("asset_category_applicability") or "").strip(),
         str(values.get("market_applicability") or "").strip(),
         str(values.get("notes") or "").strip(), source_type, priority,
         int(bool(values.get("is_default"))), int(bool(values.get("is_enabled"))),
         int(bool(values.get("is_archived"))), source_id),
    )
    return source_id


def create_capture_proposal(
    connection: sqlite3.Connection, *, proposal_type: str, job_id: int,
    verification_session_id: int | None, job_asset_id: int | None,
    requested_need_id: int | None, connector_profile_id: int | None,
    page_url: str, payload: dict,
) -> int:
    """Future Firefox boundary: stores untrusted data for review, never authoritative records."""
    proposal_type = str(proposal_type or "").upper()
    if proposal_type not in {"PART_RESULT", "CART_RESULTS", "SOURCE"}:
        raise HTTPException(status_code=400, detail="Invalid research capture proposal type.")
    page_url = validate_source_url(page_url)
    if connection.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="Research Job not found.")
    session = None
    if verification_session_id is not None:
        session = connection.execute(
            "SELECT * FROM verification_sessions WHERE id=? AND job_id=?",
            (verification_session_id, job_id),
        ).fetchone()
        if session is None:
            raise HTTPException(status_code=409, detail="Research Session does not belong to this Job.")
        for supplied, field, label in (
            (job_asset_id, "job_asset_id", "machine"),
            (requested_need_id, "requested_need_id", "Requested Need"),
            (connector_profile_id, "connector_profile_id", "source"),
        ):
            if supplied is not None and session[field] != supplied:
                raise HTTPException(status_code=409, detail=f"Capture {label} conflicts with the active Research Session.")
        job_asset_id = session["job_asset_id"]
        requested_need_id = session["requested_need_id"]
        connector_profile_id = session["connector_profile_id"]
    if job_asset_id is not None and connection.execute(
        "SELECT 1 FROM job_assets WHERE id=? AND job_id=?", (job_asset_id, job_id)
    ).fetchone() is None:
        raise HTTPException(status_code=409, detail="Capture machine does not belong to this Job.")
    if requested_need_id is not None:
        need = connection.execute(
            "SELECT job_asset_id FROM requested_needs WHERE id=? AND job_id=?",
            (requested_need_id, job_id),
        ).fetchone()
        if need is None or (need["job_asset_id"] is not None and need["job_asset_id"] != job_asset_id):
            raise HTTPException(status_code=409, detail="Capture Requested Need conflicts with the machine context.")
    if connector_profile_id is not None and connection.execute(
        "SELECT 1 FROM connector_profiles WHERE id=? AND is_enabled=1", (connector_profile_id,)
    ).fetchone() is None:
        raise HTTPException(status_code=409, detail="Capture source is unavailable.")
    return int(connection.execute(
        """INSERT INTO research_capture_proposals (
            proposal_type,verification_session_id,job_id,job_asset_id,requested_need_id,
            connector_profile_id,page_url,payload_json
        ) VALUES (?,?,?,?,?,?,?,?)""",
        (proposal_type, verification_session_id, job_id, job_asset_id, requested_need_id,
         connector_profile_id, page_url, json.dumps(payload or {}, sort_keys=True)),
    ).lastrowid)
