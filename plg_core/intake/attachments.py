from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import uuid

from fastapi import HTTPException, UploadFile

from legacy_app import UPLOADS_DIR
from plg_core.intake.documents import extract_pdf_text


UPLOAD_ROOT = UPLOADS_DIR / "intake-proposals"
REQUEST_UPLOAD_ROOT = UPLOADS_DIR / "requests"
MAX_FILES = 8
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024
IMAGE_TYPES = {
    ".jpg": ("image/jpeg",),
    ".jpeg": ("image/jpeg",),
    ".png": ("image/png",),
}
PDF_TYPES = {".pdf": ("application/pdf",)}


@dataclass(frozen=True)
class ValidatedImage:
    original_filename: str
    media_type: str
    data: bytes
    extracted_text: str = ""
    extraction_status: str = "UNAVAILABLE"
    extraction_evidence: str = ""
    page_count: int = 0


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._")
    return cleaned or "image"


def _valid_signature(data: bytes, media_type: str) -> bool:
    if media_type == "image/jpeg":
        return len(data) >= 4 and data[:3] == b"\xff\xd8\xff" and data[-2:] == b"\xff\xd9"
    if media_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n") and b"IEND" in data[-32:]
    return False


async def validate_images(uploads: list[UploadFile]) -> list[ValidatedImage]:
    """Compatibility validator for image-only callers."""
    validated = await validate_attachments(uploads, allow_pdf=False)
    return validated


async def validate_attachments(
    uploads: list[UploadFile], *, allow_pdf: bool = True,
) -> list[ValidatedImage]:
    present = [upload for upload in uploads if upload and upload.filename]
    validated: list[ValidatedImage] = []
    total = 0
    try:
        if len(present) > MAX_FILES:
            raise HTTPException(status_code=400, detail=f"Attach no more than {MAX_FILES} images.")
        for upload in present:
            original = Path(upload.filename or "").name
            extension = Path(original).suffix.lower()
            media_type = (upload.content_type or "").lower()
            allowed = dict(IMAGE_TYPES)
            if allow_pdf:
                allowed.update(PDF_TYPES)
            if extension not in allowed or media_type not in allowed[extension]:
                accepted = "JPEG, PNG, and PDF" if allow_pdf else "JPEG and PNG"
                raise HTTPException(status_code=400, detail=f"Smart Intake accepts {accepted} files only.")
            data = await upload.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise HTTPException(status_code=413, detail=f"Each image must be {MAX_FILE_BYTES // (1024 * 1024)} MB or smaller.")
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise HTTPException(status_code=413, detail=f"Combined images must be {MAX_TOTAL_BYTES // (1024 * 1024)} MB or smaller.")
            if media_type == "application/pdf":
                result = extract_pdf_text(data)
                validated.append(ValidatedImage(
                    original, media_type, data, result.text, result.status,
                    result.evidence, result.page_count,
                ))
                continue
            if not _valid_signature(data, media_type):
                raise HTTPException(status_code=400, detail=f"{original} is not a valid {media_type.removeprefix('image/').upper()} image.")
            validated.append(ValidatedImage(original, media_type, data))
    finally:
        for upload in present:
            await upload.close()
    return validated


def store_proposal_images(connection, proposal_id: int, images: list[ValidatedImage]) -> list[Path]:
    proposal_dir = UPLOAD_ROOT / str(proposal_id)
    created: list[Path] = []
    try:
        if images:
            proposal_dir.mkdir(parents=True, exist_ok=True)
        for image in images:
            stored = f"{uuid.uuid4().hex}_{safe_filename(image.original_filename)}"
            destination = proposal_dir / stored
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_bytes(image.data)
            os.replace(temporary, destination)
            created.append(destination)
            connection.execute(
                """INSERT INTO intake_proposal_attachments
                (proposal_id,original_filename,stored_path,media_type,extraction_status)
                VALUES (?,?,?,?, ?)""",
                (proposal_id, image.original_filename, str(destination), image.media_type,
                 image.extraction_status),
            )
        return created
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def copy_proposal_images_to_request(connection, proposal_id: int, request_id: int) -> list[Path]:
    rows = connection.execute(
        "SELECT * FROM intake_proposal_attachments WHERE proposal_id=? ORDER BY id", (proposal_id,)
    ).fetchall()
    request_dir = REQUEST_UPLOAD_ROOT / str(request_id)
    created: list[Path] = []
    try:
        if rows:
            request_dir.mkdir(parents=True, exist_ok=True)
        for row in rows:
            source = Path(row["stored_path"])
            if not source.is_file():
                raise HTTPException(status_code=409, detail=f"Proposal attachment {row['original_filename']} is missing.")
            stored = f"{uuid.uuid4().hex}_{safe_filename(row['original_filename'])}"
            destination = request_dir / stored
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_bytes(source.read_bytes())
            os.replace(temporary, destination)
            created.append(destination)
            connection.execute(
                """INSERT INTO customer_request_attachments
                (request_id,original_filename,stored_filename,file_path,media_type)
                VALUES (?,?,?,?,?)""",
                (request_id, row["original_filename"], stored, str(destination), row["media_type"]),
            )
        return created
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
