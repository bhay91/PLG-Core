from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from plg_core.intake.identifiers import classify_identifier, normalize_identifier


ALIASES = {
    "john deere": ("John Deere", "machine"), "deere": ("John Deere", "machine"),
    "jcb": ("JCB", "machine"), "caterpillar": ("Caterpillar", "machine"),
    "cat": ("Caterpillar", "machine"), "komatsu": ("Komatsu", "machine"),
    "bomag": ("BOMAG", "machine"), "hamm": ("HAMM", "machine"),
    "gradall": ("Gradall", "machine"), "cummins": ("Cummins", "engine"),
    "toyota": ("Toyota", "vehicle"), "honda": ("Honda", "vehicle"),
    "bmw": ("BMW", "vehicle"), "mercedes benz": ("Mercedes-Benz", "vehicle"),
    "mercedes-benz": ("Mercedes-Benz", "vehicle"), "mack": ("Mack", "vehicle"),
    "volvo": ("Volvo", "vehicle"), "isuzu": ("Isuzu", "vehicle"),
}
COMPANY_WORDS = r"(?:Development|Construction|Real Estate|Limited|Ltd|Inc|LLC|Company|Co|Group|Holdings|Services|Trading|Enterprises)"
LOCATION_PATTERN = re.compile(
    r"\b(Montego Bay(?:\s*,?\s*Jamaica)?|Nassau(?:\s*,?\s*Bahamas)?|[A-Z][a-z]+\s*,\s*(?:Jamaica|Bahamas))\b",
    re.I,
)


@dataclass
class Identifier:
    identifier_type: str
    value: str
    component_label: str = ""
    primary: bool = False
    review_state: str = "CONFIDENT"


@dataclass
class Asset:
    manufacturer: str
    model: str = ""
    year: str = ""
    asset_category: str = "other"
    market_region: str = "UNKNOWN"
    model_code: str = ""
    review_state: str = "REVIEW"
    identifiers: list[Identifier] = field(default_factory=list)
    needs: list[dict] = field(default_factory=list)


def _clean_need(value: str) -> str:
    value = re.sub(r"^[\s\-•:]+", "", value.strip())
    value = re.sub(r"[.\s]+$", "", value)
    return value.strip()


def _split_needs(value: str) -> list[str]:
    value = re.sub(r"\b(?:and|&)\b", "\n", value, flags=re.I)
    return [
        cleaned for part in re.split(r"[\n;,]+", value)
        if (cleaned := _clean_need(part)) and not re.fullmatch(r"(?:19|20)\d{2}", cleaned)
    ]


def _identity_prefix(text: str, first_make: int) -> tuple[str, str, str]:
    prefix = text[:first_make].strip(" ,.-\n")
    prefix = re.sub(r"^PPS-[A-Z0-9-]+\s*", "", prefix, flags=re.I)
    email = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", prefix)
    phone = re.search(r"(?:\+?\d[\d ()-]{6,}\d)", prefix)
    prefix = re.sub(re.escape(email.group()) if email else r"(?!x)x", "", prefix)
    prefix = re.sub(re.escape(phone.group()) if phone else r"(?!x)x", "", prefix)
    location_match = LOCATION_PATTERN.search(prefix)
    location = location_match.group(1) if location_match else ""
    location = re.sub(r"\s*,?\s+(Jamaica|Bahamas)$", r", \1", location, flags=re.I)
    if location_match:
        prefix = prefix[: location_match.start()] + prefix[location_match.end() :]
    prefix = re.sub(r"\b(?:Customer|Name|Company|Location)\s*:\s*", "", prefix, flags=re.I)
    lines = [x.strip(" ,") for x in prefix.splitlines() if x.strip(" ,")]
    contact = company = ""
    if len(lines) >= 2:
        contact, company = lines[0], lines[1]
    elif lines:
        candidate = lines[0]
        company_match = re.search(rf"\b(.+?{COMPANY_WORDS}(?:\s+{COMPANY_WORDS})*)\b", candidate, re.I)
        if company_match:
            company = company_match.group(1).strip()
            contact = (candidate[: company_match.start()] + candidate[company_match.end() :]).strip(" ,")
            if not contact:
                words = company.split()
                if len(words) >= 5:
                    contact = " ".join(words[:2])
                    company = " ".join(words[2:])
        else:
            contact = candidate
    contact = re.sub(r"\bfrom\b.*$", "", contact, flags=re.I).strip()
    return contact, company, location


def _asset_matches(text: str) -> list[tuple[int, int, str, str]]:
    matches = []
    for alias, (canonical, category) in sorted(ALIASES.items(), key=lambda item: -len(item[0])):
        for match in re.finditer(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", text, re.I):
            if any(start <= match.start() < end for start, end, _, _ in matches):
                continue
            matches.append((match.start(), match.end(), canonical, category))
    return sorted(matches)


def _identifier_candidates(segment: str, asset: Asset) -> list[Identifier]:
    found: list[Identifier] = []
    labeled = re.compile(
        r"\b(Model\s*Code|Engine\s*(?:Serial|ESN)|Component\s*Serial|Transmission\s*Serial|Axle\s*Serial|VIN|PIN|Frame|Chassis|Serial(?:\s*Number)?)\s*:?\s*([A-Z0-9][A-Z0-9-]{4,})",
        re.I,
    )
    occupied: list[tuple[int, int]] = []
    for match in labeled.finditer(segment):
        label, value = match.group(1), normalize_identifier(match.group(2))
        kind = classify_identifier(value, label=label, manufacturer=asset.manufacturer, asset_category=asset.asset_category)
        if kind == "MODEL_CODE":
            asset.model_code = value
        else:
            found.append(Identifier(kind, value, label if kind in {"ENGINE_SERIAL", "COMPONENT_SERIAL"} else "", not found))
        occupied.append(match.span())
    for match in re.finditer(r"\b[A-HJ-NPR-Z0-9]{17}\b", segment, re.I):
        if any(a <= match.start() < b for a, b in occupied):
            continue
        value = normalize_identifier(match.group())
        kind = classify_identifier(value, manufacturer=asset.manufacturer, asset_category=asset.asset_category)
        found.append(Identifier(kind, value, primary=not found, review_state="REVIEW"))
    return found


def parse_intake(raw_input: str) -> dict:
    """Deterministic proposal parser. It proposes; confirmation remains authoritative."""
    raw = str(raw_input or "").strip()
    normalized = re.sub(r"\r\n?", "\n", raw)
    # Natural sentence form gets structural line breaks without destroying evidence.
    working = re.sub(r"^(.+?)\s+from\s+(.+?)\s+in\s+(.+?)\s+needs\s+(.+?)\s+for\s+(?:their|his|her)\s+", r"\1\n\2\n\3\nNeed: \4\n", normalized, flags=re.I)
    matches = _asset_matches(working)
    first_make = matches[0][0] if matches else len(working)
    contact, company, location = _identity_prefix(working, first_make)
    email_match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", raw)
    phone_match = re.search(r"(?:\+?\d[\d ()-]{6,}\d)", raw)
    assets: list[Asset] = []
    for index, (start, end, canonical, category) in enumerate(matches):
        segment_end = matches[index + 1][0] if index + 1 < len(matches) else len(working)
        segment = working[start:segment_end]
        lead = segment[end - start :]
        model_part = re.split(r"\b(?:VIN|PIN|Frame|Chassis|Serial|Model\s*Code|Engine\s*Serial|needs?|Need)\b", lead, maxsplit=1, flags=re.I)[0]
        year_match = re.search(r"\b(19\d{2}|20\d{2})\b", model_part)
        # Year can precede manufacturer by a few characters.
        if not year_match:
            before = working[max(0, start - 6):start]
            year_match = re.search(r"\b(19\d{2}|20\d{2})\b", before)
        model = re.sub(r"\b(?:19\d{2}|20\d{2})\b", "", model_part).strip(" ,.:\n")
        model = re.split(r"\.\s*(?:The\s+)?machine\b", model, flags=re.I)[0].strip()
        model = model.splitlines()[0].strip() if model else ""
        asset = Asset(canonical, model, year_match.group(1) if year_match else "", category, review_state="CONFIDENT" if model else "REVIEW")
        asset.identifiers = _identifier_candidates(segment, asset)
        for need_match in re.finditer(r"\bneeds?\s*:?\s*(.+?)(?=(?:\n\s*\n|\b(?:VIN|PIN|Frame|Chassis|Serial|Model\s*Code)\b|$))", segment, re.I | re.S):
            need_text = need_match.group(1)
            for wording in _split_needs(need_text):
                # Avoid swallowing natural-sentence identifier clauses.
                wording = re.split(r"\.\s*The machine", wording, flags=re.I)[0]
                if wording:
                    asset.needs.append({"wording": wording, "original_wording": wording, "review_state": "CONFIDENT"})
        assets.append(asset)

    natural_need = re.search(
        r"\bneeds?\s+(?:an?\s+)?(.+?)\s+for\s+(?:their|his|her)\s+", raw,
        re.I,
    )
    if len(assets) == 1 and natural_need and not assets[0].needs:
        for wording in _split_needs(natural_need.group(1)):
            assets[0].needs.append({"wording": wording, "original_wording": wording, "review_state": "CONFIDENT"})

    # Multiline unlabeled final values: identifier then requested wording.
    lines = [line.strip(" -•\t") for line in working.splitlines() if line.strip(" -•\t")]
    if len(assets) == 1 and not assets[0].needs:
        make_line = next((i for i, line in enumerate(lines) if assets[0].manufacturer.lower() in line.lower()), -1)
        tail = lines[make_line + 1 :]
        ignored = re.compile(r"^(?:PIN|VIN|Frame|Chassis|Serial|Model Code)\b", re.I)
        candidates = [line for line in tail if not ignored.search(line) and not re.fullmatch(r"[A-Z0-9-]{7,}", line, re.I)]
        if candidates:
            wording = _clean_need(candidates[-1])
            assets[0].needs.append({"wording": wording, "original_wording": wording, "review_state": "REVIEW"})

    unassigned: list[dict] = []
    if not assets:
        zero_need = re.search(r"^(.+?)\s+needs?\s+(?:an?\s+)?(.+?)(?:\s+but\b|[.!?]|$)", raw, re.I)
        if zero_need:
            contact = re.sub(r"\b(?:Customer|Name)\s*:\s*", "", zero_need.group(1), flags=re.I).strip()
            for wording in _split_needs(zero_need.group(2)):
                unassigned.append({"wording": wording, "original_wording": wording, "review_state": "UNASSIGNED"})
    if len(assets) > 1:
        last_asset_end = matches[-1][1]
        # A generic trailing Need after multiple bare asset mentions is ambiguous.
        ambiguous = re.search(r"\bNeed\s*:?\s*([^\n.]+)\s*$", working, re.I)
        if ambiguous and not any(asset.needs for asset in assets[:-1]):
            wording = _clean_need(ambiguous.group(1))
            assets[-1].needs = [n for n in assets[-1].needs if n["wording"].lower() != wording.lower()]
            unassigned.append({"wording": wording, "original_wording": wording, "review_state": "UNASSIGNED"})

    return {
        "raw_input": raw,
        "contact_name": contact,
        "company_name": company,
        "location": location,
        "phone": phone_match.group().strip() if phone_match else "",
        "email": email_match.group() if email_match else "",
        "review_state": "REVIEW" if not (contact or company) else "CONFIDENT",
        "assets": [asdict(asset) for asset in assets],
        "unassigned_needs": unassigned,
        "contributor": "DETERMINISTIC",
    }
