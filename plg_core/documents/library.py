from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException


def _document_type(parts: tuple[str, ...]) -> str:
    lowered = {
        str(part).strip().lower()
        for part in parts
    }

    if "parts order sheets" in lowered:
        return "Parts Order Sheet"

    if "quotes" in lowered:
        return "Quote"

    if "invoices" in lowered:
        return "Invoice"

    return "Document"


def _audience(parts: tuple[str, ...]) -> str:
    lowered = [
        str(part).strip().lower()
        for part in parts
    ]

    if "internal" in lowered:
        return "Internal"

    if "customer" in lowered:
        return "Customer"

    if "custom" in lowered:
        return "Custom"

    return "General"


def scan_documents(
    root: Path,
    *,
    q: str = "",
    limit: int = 500,
) -> dict:
    root = Path(root)
    safe_limit = max(
        1,
        min(int(limit or 500), 2000),
    )

    query = str(q or "").strip().lower()

    if not root.exists():
        return {
            "items": [],
            "summary": {
                "total": 0,
                "quotes": 0,
                "invoices": 0,
                "purchasing": 0,
                "customers": 0,
            },
            "query": str(q or "").strip(),
        }

    root_resolved = root.resolve()
    items = []

    for path in root.rglob("*.pdf"):
        try:
            resolved = path.resolve()
            relative = resolved.relative_to(
                root_resolved
            )
            stat = resolved.stat()
        except (OSError, ValueError):
            continue

        parts = relative.parts

        customer = ""

        if (
            len(parts) >= 2
            and parts[0].strip().lower()
                == "customers"
        ):
            customer = parts[1]

        document_type = _document_type(parts)
        audience = _audience(parts)

        search_text = " ".join(
            [
                str(relative),
                resolved.name,
                customer,
                document_type,
                audience,
            ]
        ).lower()

        if query and query not in search_text:
            continue

        relative_text = relative.as_posix()

        items.append(
            {
                "name": resolved.name,
                "relative_path": relative_text,
                "customer": customer or "—",
                "document_type": document_type,
                "audience": audience,
                "size_bytes": int(stat.st_size),
                "size_kb": round(
                    stat.st_size / 1024,
                    1,
                ),
                "modified_at": datetime.fromtimestamp(
                    stat.st_mtime
                ).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "_mtime": stat.st_mtime,
                "open_url": (
                    "/documents/file?path="
                    + quote(
                        relative_text,
                        safe="",
                    )
                ),
            }
        )

    items.sort(
        key=lambda item: (
            -float(item["_mtime"]),
            str(item["name"]).lower(),
        )
    )

    items = items[:safe_limit]

    for item in items:
        item.pop("_mtime", None)

    customers = {
        item["customer"]
        for item in items
        if item["customer"] != "—"
    }

    return {
        "items": items,
        "summary": {
            "total": len(items),
            "quotes": sum(
                1
                for item in items
                if item["document_type"] == "Quote"
            ),
            "invoices": sum(
                1
                for item in items
                if item["document_type"] == "Invoice"
            ),
            "purchasing": sum(
                1
                for item in items
                if item["document_type"]
                    == "Parts Order Sheet"
            ),
            "customers": len(customers),
        },
        "query": str(q or "").strip(),
    }


def resolve_document_path(
    root: Path,
    relative_path: str,
) -> Path:
    root = Path(root).resolve()
    supplied = str(
        relative_path or ""
    ).strip()

    if not supplied:
        raise HTTPException(
            status_code=400,
            detail="Document path is required.",
        )

    candidate = (
        root / supplied
    ).resolve()

    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail="Invalid document path.",
        )

    if (
        not candidate.exists()
        or not candidate.is_file()
    ):
        raise HTTPException(
            status_code=404,
            detail="Document not found.",
        )

    if candidate.suffix.lower() != ".pdf":
        raise HTTPException(
            status_code=403,
            detail="Only PPS PDF documents may be opened.",
        )

    return candidate
