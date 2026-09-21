from __future__ import annotations

from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[2]

BRAND_ALIASES = {
    "audi": ("audi", "Audi"),
    "bmw": ("bmw", "BMW"),
    "bobcat": ("bobcat", "Bobcat"),
    "bomag": ("bomag", "BOMAG"),
    "case": ("case", "CASE"),
    "case construction": ("case", "CASE"),
    "case ih": ("case", "CASE"),
    "cat": ("caterpillar", "Caterpillar"),
    "caterpillar": ("caterpillar", "Caterpillar"),
    "chevrolet": ("chevrolet", "Chevrolet"),
    "chevy": ("chevrolet", "Chevrolet"),
    "cummins": ("cummins", "Cummins"),
    "deere": ("john-deere", "John Deere"),
    "detroit": ("detroit-diesel", "Detroit Diesel"),
    "detroit diesel": ("detroit-diesel", "Detroit Diesel"),
    "deutz": ("deutz", "Deutz"),
    "dodge": ("dodge", "Dodge"),
    "ford": ("ford", "Ford"),
    "ford motor company": ("ford", "Ford"),
    "freightliner": ("freightliner", "Freightliner"),
    "gmc": ("gmc", "GMC"),
    "gmc truck": ("gmc", "GMC"),
    "gmc trucks": ("gmc", "GMC"),
    "gradall": ("gradall", "Gradall"),
    "hamm": ("hamm", "HAMM"),
    "hino": ("hino", "Hino"),
    "hino trucks": ("hino", "Hino"),
    "honda": ("honda", "Honda"),
    "hyundai": ("hyundai", "Hyundai"),
    "international": ("international", "International"),
    "international truck": ("international", "International"),
    "international trucks": ("international", "International"),
    "isuzu": ("isuzu", "Isuzu"),
    "isuzu truck": ("isuzu", "Isuzu"),
    "jcb": ("jcb", "JCB"),
    "jeep": ("jeep", "Jeep"),
    "jlg": ("jlg", "JLG"),
    "jlg industries": ("jlg", "JLG"),
    "john deere": ("john-deere", "John Deere"),
    "kenworth": ("kenworth", "Kenworth"),
    "kia": ("kia", "Kia"),
    "komatsu": ("komatsu", "Komatsu"),
    "kubota": ("kubota", "Kubota"),
    "mack": ("mack", "Mack"),
    "mack trucks": ("mack", "Mack"),
    "mazda": ("mazda", "Mazda"),
    "mercedes": ("mercedes-benz", "Mercedes-Benz"),
    "mercedes benz": ("mercedes-benz", "Mercedes-Benz"),
    "mercedes-benz": ("mercedes-benz", "Mercedes-Benz"),
    "mercury": ("mercury", "Mercury Marine"),
    "mercury marine": ("mercury", "Mercury Marine"),
    "mercury outboard": ("mercury", "Mercury Marine"),
    "new holland": ("new-holland", "New Holland"),
    "new holland construction": ("new-holland", "New Holland"),
    "nissan": ("nissan", "Nissan"),
    "perkins": ("perkins", "Perkins"),
    "perkins engines": ("perkins", "Perkins"),
    "peterbilt": ("peterbilt", "Peterbilt"),
    "ram": ("ram", "Ram"),
    "ram truck": ("ram", "Ram"),
    "ram trucks": ("ram", "Ram"),
    "suzuki marine": ("suzuki-marine", "Suzuki Marine"),
    "suzuki outboard": ("suzuki-marine", "Suzuki Marine"),
    "toyota": ("toyota", "Toyota"),
    "volkswagen": ("volkswagen", "Volkswagen"),
    "vw": ("volkswagen", "Volkswagen"),
    "volvo": ("volvo", "Volvo"),
    "volvo penta": ("volvo-penta", "Volvo Penta"),
    "volvo truck": ("volvo-trucks", "Volvo Trucks"),
    "volvo trucks": ("volvo-trucks", "Volvo Trucks"),
    "yamaha": ("yamaha", "Yamaha"),
    "yamaha marine": ("yamaha", "Yamaha"),
    "yanmar": ("yanmar", "Yanmar"),
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
