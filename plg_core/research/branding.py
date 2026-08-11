from __future__ import annotations

from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def normalize_manufacturer(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


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
            path = str(row["local_logo_path"] or "").strip()
            available = bool(path and (PROJECT_ROOT / path.lstrip("/")).is_file())
            return {
                "canonical_name": row["canonical_name"],
                "display_name": row["canonical_name"],
                "key": row["normalized_key"],
                "logo_url": f"/{path.lstrip('/')}" if available else "",
                "has_logo": available,
                "initials": "".join(word[0] for word in row["canonical_name"].split())[:3].upper(),
            }
    label = str(manufacturer or "Asset").strip() or "Asset"
    return {
        "canonical_name": label,
        "display_name": label,
        "key": "generic",
        "logo_url": "",
        "has_logo": False,
        "initials": "".join(word[0] for word in label.split())[:3].upper() or "A",
    }
