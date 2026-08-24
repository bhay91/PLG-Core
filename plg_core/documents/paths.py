from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def configured_document_root() -> Path:
    """Return the configured PPS document root as a normalized absolute path."""
    return Path(
        os.getenv("PPS_DOCUMENT_ROOT", str(PROJECT_ROOT / "documents"))
    ).expanduser().resolve()


def resolve_manifest_path(
    stored_path: str | Path,
    *,
    root: str | Path | None = None,
) -> Path:
    """Resolve a portable manifest path without permitting escape from its root."""
    document_root = (
        Path(root).expanduser().resolve()
        if root is not None
        else configured_document_root()
    )
    raw_path = str(stored_path or "").strip()
    if not raw_path:
        raise ValueError("Document manifest path is empty.")
    supplied = Path(raw_path)

    if supplied.is_absolute():
        candidate = supplied.expanduser().resolve()
    else:
        parts = supplied.parts
        if parts and parts[0].lower() == "documents":
            supplied = Path(*parts[1:])
        candidate = (document_root / supplied).resolve()

    try:
        candidate.relative_to(document_root)
    except ValueError as exc:
        raise ValueError("Document manifest path is outside the PPS document root.") from exc
    return candidate


def portable_manifest_path(
    path: str | Path,
    *,
    root: str | Path | None = None,
) -> str:
    """Serialize a generated document path independently of the host filesystem."""
    document_root = (
        Path(root).expanduser().resolve()
        if root is not None
        else configured_document_root()
    )
    resolved = resolve_manifest_path(path, root=document_root)
    return (Path("documents") / resolved.relative_to(document_root)).as_posix()
