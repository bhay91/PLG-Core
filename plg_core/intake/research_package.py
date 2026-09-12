"""Offline authoring for the deployed .ppsresearch v1 transport."""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping
from zipfile import ZIP_DEFLATED, ZipFile

from plg_core.intake.research_import import ResearchImportPackage


def build_ppsresearch_package(
    research_payload: Mapping[str, Any],
    source_pdf_bytes: bytes,
    *,
    overwrite: bool = False,
    output_path: str | Path | None = None,
) -> bytes:
    """Validate and package one existing-schema research payload and PDF."""
    if not source_pdf_bytes or not source_pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("source PDF is missing or invalid")
    if len(source_pdf_bytes) > 8 * 1024 * 1024:
        raise ValueError("source PDF exceeds the 8 MiB member limit")
    payload = dict(research_payload)
    source = dict(payload.get("source_pdf") or {})
    source["filename"] = "source.pdf"
    source["sha256"] = hashlib.sha256(source_pdf_bytes).hexdigest()
    payload["source_pdf"] = source
    package = ResearchImportPackage.model_validate(payload)
    research_json = json.dumps(package.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest = json.dumps({"format": "ppsresearch", "version": 1, "research": "research.json", "source_pdf": "source.pdf"}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", manifest)
        archive.writestr("research.json", research_json)
        archive.writestr("source.pdf", source_pdf_bytes)
    result = stream.getvalue()
    if output_path is not None:
        destination = Path(output_path)
        if destination.suffix.lower() != ".ppsresearch":
            raise ValueError("output path must use the .ppsresearch extension")
        if destination.exists() and not overwrite:
            raise FileExistsError(f"output already exists: {destination}")
        destination.write_bytes(result)
    return result
