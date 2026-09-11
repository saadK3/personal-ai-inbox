"""Durable image storage and retryable vision/OCR processing."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models.capture import Capture
from app.providers.openai import VisionProvider

logger = logging.getLogger(__name__)
IMAGE_PENDING_STATUS = "image_pending"
IMAGE_PROCESSING_STATUS = "image_processing"
IMAGE_FAILED_STATUS = "image_failed"
IMAGE_UNSUPPORTED_STATUS = "image_unsupported"
IMAGE_RETRYABLE_STATUSES = (IMAGE_PENDING_STATUS, IMAGE_FAILED_STATUS)
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_INDEX_TEXT_LENGTH = 5_000


def _error_message(error: Exception) -> str:
    message = str(error).strip() or error.__class__.__name__
    return message[:1_000]


def pending_image_ids(
    session: Session,
    *,
    max_attempts: int,
    limit: int = 50,
) -> list[uuid.UUID]:
    """Return image captures that can resume vision processing."""

    return list(
        session.scalars(
            select(Capture.id)
            .where(
                Capture.deleted_at.is_(None),
                Capture.source_type == "image",
                Capture.processing_status.in_(IMAGE_RETRYABLE_STATUSES),
                Capture.vision_attempts < max_attempts,
            )
            .order_by(Capture.created_at.asc(), Capture.id.asc())
            .limit(limit)
        )
    )


def _image_path(capture_id: uuid.UUID, metadata: dict, storage_dir: Path) -> Path:
    image = metadata.get("image")
    if not isinstance(image, dict):
        raise RuntimeError("Image capture has no image metadata")
    local_path = image.get("local_path")
    if isinstance(local_path, str) and local_path.strip():
        return Path(local_path)
    filename = image.get("filename")
    suffix = Path(filename).suffix.lower() if isinstance(filename, str) else ".jpg"
    if not suffix or len(suffix) > 10:
        suffix = ".jpg"
    return storage_dir / "images" / f"{capture_id}{suffix}"


def _download_image(url: str, destination: Path, mime_type: str | None) -> Path:
    """Download one Discord image attachment with a strict byte limit."""

    request = Request(url, headers={"User-Agent": "personal-ai-inbox/1.0"})
    with urlopen(request, timeout=60) as response:  # noqa: S310  # URL comes from Discord.
        content_type = (response.headers.get_content_type() or "").casefold()
        payload = response.read(MAX_IMAGE_BYTES + 1)
    if len(payload) == 0:
        raise RuntimeError("The image attachment was empty")
    if len(payload) > MAX_IMAGE_BYTES:
        raise RuntimeError("The image attachment is larger than 25 MB")
    expected_type = (mime_type or "").split(";", 1)[0].casefold()
    if content_type and not content_type.startswith("image/") and not expected_type.startswith(
        "image/"
    ):
        raise RuntimeError(f"unsupported image content type ({content_type})")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return destination


def _index_text(
    metadata: dict,
    description: str,
    ocr_text: str | None,
    uncertainty: str | None,
) -> str:
    image = metadata.get("image")
    filename = image.get("filename") if isinstance(image, dict) else ""
    submitted_text = str(metadata.get("submitted_text") or "").strip()
    parts = [submitted_text, str(filename), description, ocr_text or "", uncertainty or ""]
    text = " ".join(" ".join(part.split()) for part in parts if part and part.strip())
    if len(text) <= MAX_INDEX_TEXT_LENGTH:
        return text
    return f"{text[: MAX_INDEX_TEXT_LENGTH - 1].rstrip()}…"


async def process_image_capture(
    capture_id: uuid.UUID,
    settings: Settings,
    session_factory: sessionmaker[Session],
    provider: VisionProvider | None = None,
) -> bool:
    """Persist a durable image copy, describe it, and retain OCR/search text."""

    session = session_factory()
    try:
        capture = session.get(Capture, capture_id)
        if capture is None or capture.deleted_at is not None or capture.source_type != "image":
            return False
        image_metadata = (capture.source_metadata or {}).get("image")
        if (
            isinstance(image_metadata, dict)
            and image_metadata.get("description")
            and capture.processing_status in {"captured", "processing", "processed", "failed"}
        ):
            return True
        if capture.processing_status == IMAGE_PROCESSING_STATUS:
            return False
        capture.processing_status = IMAGE_PROCESSING_STATUS
        capture.processing_error = None
        capture.vision_attempts += 1
        metadata = dict(capture.source_metadata or {})
        session.commit()
    finally:
        session.close()

    try:
        image = metadata.get("image")
        if not isinstance(image, dict):
            raise RuntimeError("Image capture has no image metadata")
        image_path = _image_path(capture_id, metadata, settings.storage_dir)
        image_url = image.get("url")
        if not image_path.is_file():
            if not isinstance(image_url, str) or not image_url.strip():
                raise RuntimeError("The image attachment URL is unavailable")
            image_path = await asyncio.to_thread(
                _download_image,
                image_url,
                image_path,
                image.get("content_type"),
            )
        # Persist the copy reference before the provider call so a vision failure
        # never loses the user's durable original attachment.
        metadata["image"] = {**image, "local_path": str(image_path)}
        reference_session = session_factory()
        try:
            referenced_capture = reference_session.get(Capture, capture_id)
            if referenced_capture is not None:
                referenced_metadata = dict(referenced_capture.source_metadata or {})
                referenced_metadata["image"] = metadata["image"]
                referenced_capture.source_metadata = referenced_metadata
                reference_session.commit()
        finally:
            reference_session.close()
        active_provider = provider
        if active_provider is None:
            from app.providers.openai import OpenAIProvider

            active_provider = OpenAIProvider(settings)
        result = await asyncio.to_thread(
            active_provider.describe_image,
            image_path,
            str(image.get("content_type") or "image/jpeg"),
            str(metadata.get("submitted_text") or ""),
        )
        metadata["image"] = {
            **image,
            "local_path": str(image_path),
            "description": result.description,
            "ocr_text": result.ocr_text,
            "uncertainty": result.uncertainty,
        }
        indexed_text = _index_text(
            metadata,
            result.description,
            result.ocr_text,
            result.uncertainty,
        )
    except Exception as exc:
        logger.exception("Image processing failed", extra={"capture_id": str(capture_id)})
        failure_session = session_factory()
        try:
            failed_capture = failure_session.get(Capture, capture_id)
            if failed_capture is not None:
                failed_capture.processing_status = IMAGE_FAILED_STATUS
                failed_capture.processing_error = _error_message(exc)
                failure_session.commit()
        finally:
            failure_session.close()
        return False

    success_session = session_factory()
    try:
        processed_capture = success_session.get(Capture, capture_id)
        if processed_capture is None or processed_capture.deleted_at is not None:
            return False
        processed_capture.source_metadata = metadata
        processed_capture.raw_text = indexed_text
        processed_capture.processing_status = "captured"
        processed_capture.processing_error = None
        success_session.commit()
        return True
    except Exception as exc:
        success_session.rollback()
        logger.exception(
            "Image processing persistence failed",
            extra={"capture_id": str(capture_id)},
        )
        failed_capture = success_session.get(Capture, capture_id)
        if failed_capture is not None:
            failed_capture.processing_status = IMAGE_FAILED_STATUS
            failed_capture.processing_error = _error_message(exc)
            success_session.commit()
        return False
    finally:
        success_session.close()
