from __future__ import annotations

import re


IDENTIFIER_TYPES = (
    "AUTOMOTIVE_VIN", "JDM_FRAME", "JDM_CHASSIS", "MODEL_CODE", "PIN",
    "MACHINE_SERIAL", "ENGINE_SERIAL", "COMPONENT_SERIAL", "OTHER_IDENTIFIER",
    "UNKNOWN",
)
MARKETS = ("UNKNOWN", "JDM", "USDM", "EDM", "UK", "GLOBAL")

EQUIPMENT_MAKES = {
    "john deere", "deere", "jcb", "caterpillar", "cat", "komatsu", "bomag",
    "hamm", "gradall", "cummins", "mack",
}


def normalize_identifier(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().upper())


def plausible_global_vin(value: str) -> bool:
    value = normalize_identifier(value)
    return bool(re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", value))


def classify_identifier(
    value: str,
    *,
    label: str = "",
    manufacturer: str = "",
    asset_category: str = "",
) -> str:
    """Classify syntax/context only; never infer vehicle market."""
    label = re.sub(r"[^a-z]+", " ", label.lower()).strip()
    make = manufacturer.lower().strip()
    if "model code" in label:
        return "MODEL_CODE"
    if "engine" in label and ("serial" in label or "esn" in label):
        return "ENGINE_SERIAL"
    if any(word in label for word in ("component", "axle", "transmission", "ecu")):
        return "COMPONENT_SERIAL"
    if "frame" in label:
        return "JDM_FRAME"
    if "chassis" in label:
        return "JDM_CHASSIS"
    if "pin" in label:
        return "PIN"
    if "vin" in label:
        return "AUTOMOTIVE_VIN"
    if "serial" in label:
        return "MACHINE_SERIAL"
    if make in EQUIPMENT_MAKES or asset_category in {"machine", "equipment", "engine"}:
        return "PIN" if plausible_global_vin(value) else "MACHINE_SERIAL"
    if plausible_global_vin(value):
        return "AUTOMOTIVE_VIN"
    if re.fullmatch(r"[A-Z0-9]{2,12}-[A-Z0-9]{4,16}", normalize_identifier(value)):
        return "JDM_FRAME"
    return "UNKNOWN"


def decoder_route_contract(context: dict) -> dict:
    """Stable future external-decoder boundary; performs no network decoding."""
    identifier_type = context.get("identifier_type", "UNKNOWN")
    families = {
        "AUTOMOTIVE_VIN": "GLOBAL_AUTOMOTIVE_VIN",
        "JDM_FRAME": "JDM_FRAME_CHASSIS",
        "JDM_CHASSIS": "JDM_FRAME_CHASSIS",
        "PIN": "EQUIPMENT_PIN",
        "MACHINE_SERIAL": "MANUFACTURER_MACHINE_SERIAL",
        "ENGINE_SERIAL": "ENGINE_SERIAL",
        "COMPONENT_SERIAL": "COMPONENT_SERIAL",
    }
    return {
        "decoder_family": families.get(identifier_type, "MANUAL_REVIEW"),
        "identifier": context.get("identifier", ""),
        "identifier_type": identifier_type,
        "manufacturer": context.get("manufacturer", ""),
        "model": context.get("model", ""),
        "market": context.get("market", "UNKNOWN") or "UNKNOWN",
        "asset_category": context.get("asset_category", "other"),
        "external_lookup_performed": False,
    }
