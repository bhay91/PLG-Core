from __future__ import annotations

from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[2]

BRAND_ALIASES = {
    "cat": ("caterpillar", "Caterpillar"), "caterpillar": ("caterpillar", "Caterpillar"),
    "deere": ("john-deere", "John Deere"), "john deere": ("john-deere", "John Deere"),
    "jcb": ("jcb", "JCB"), "bomag": ("bomag", "BOMAG"), "hamm": ("hamm", "HAMM"),
    "mack": ("mack", "Mack"), "gmc": ("gmc", "GMC"),
    "international": ("international", "International"),
    "cummins": ("cummins", "Cummins"), "detroit": ("detroit-diesel", "Detroit Diesel"),
    "detroit diesel": ("detroit-diesel", "Detroit Diesel"), "yanmar": ("yanmar", "Yanmar"),
    "gradall": ("gradall", "Gradall"), "isuzu": ("isuzu", "Isuzu"),
    "kubota": ("kubota", "Kubota"), "honda": ("honda", "Honda"),
    "toyota": ("toyota", "Toyota"), "ford": ("ford", "Ford"),
    "mercedes": ("mercedes-benz", "Mercedes-Benz"), "mercedes benz": ("mercedes-benz", "Mercedes-Benz"),
    "mercedes-benz": ("mercedes-benz", "Mercedes-Benz"), "volvo": ("volvo", "Volvo"),
    "komatsu": ("komatsu", "Komatsu"),
}

BRAND_IMAGE_DIRECTORY = PROJECT_ROOT / "static" / "manufacturer-logos"


def normalize_manufacturer(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def manufacturer_identity(manufacturer: str) -> dict:
    original = str(manufacturer or "").strip()
    normalized = normalize_manufacturer(original)
    match = BRAND_ALIASES.get(normalized)
    if not original:
        return {"key": "", "canonical_name": "", "logo_url": "", "has_logo": False}
    if match:
        key, canonical = match
        image = next((BRAND_IMAGE_DIRECTORY / f"{key}.{suffix}" for suffix in ("webp", "png")
                      if (BRAND_IMAGE_DIRECTORY / f"{key}.{suffix}").is_file()), None)
        return {"key": key, "canonical_name": canonical,
                "logo_url": f"/static/manufacturer-logos/{image.name}" if image else "",
                "has_logo": bool(image)}
    return {"key": "generic", "canonical_name": original, "logo_url": "", "has_logo": False}


def manufacturer_brand(connection, manufacturer: str) -> dict:
    normalized = normalize_manufacturer(manufacturer)
    rows = connection.execute(
        "SELECT * FROM manufacturer_brands WHERE active=1 ORDER BY id"
    ).fetchall()
    for row in rows:
        aliases = {
            normalize_manufacturer(row["canonical_name"]),
            *(normalize_manufacturer(alias) for alias in str(row["aliases"] or "").split(",")),
        }
        if normalized in aliases:
            approved = manufacturer_identity(row["canonical_name"])
            path = str(row["local_logo_path"] or "").strip()
            available = bool(
                path
                and Path(path).suffix.lower() in {".png", ".webp"}
                and (PROJECT_ROOT / path.lstrip("/")).is_file()
            )
            return {
                "canonical_name": row["canonical_name"],
                "display_name": row["canonical_name"],
                "key": row["normalized_key"],
                "logo_url": f"/{path.lstrip('/')}" if available else approved["logo_url"],
                "has_logo": available or approved["has_logo"],
            }
    identity = manufacturer_identity(manufacturer)
    return {**identity, "display_name": identity["canonical_name"] or "Asset"}
