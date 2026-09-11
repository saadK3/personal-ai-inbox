"""Durable, retryable metadata extraction for YouTube captures."""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models.capture import Capture
from app.services.youtube import YouTubeMetadata, fetch_youtube_metadata

logger = logging.getLogger(__name__)
YOUTUBE_EXTRACTION_PENDING_STATUS = "youtube_extraction_pending"
YOUTUBE_EXTRACTING_STATUS = "youtube_extracting"
YOUTUBE_EXTRACTION_FAILED_STATUS = "youtube_extraction_failed"
YOUTUBE_EXTRACTION_RETRYABLE_STATUSES = (
    YOUTUBE_EXTRACTION_PENDING_STATUS,
    YOUTUBE_EXTRACTION_FAILED_STATUS,
)
MAX_INDEX_TEXT_LENGTH = 5_000


def _error_message(error: Exception) -> str:
    message = str(error).strip() or error.__class__.__name__
    return message[:1_000]


def pending_youtube_ids(
    session: Session,
    *,
    max_attempts: int,
    limit: int = 50,
) -> list[uuid.UUID]:
    """Return YouTube captures that can resume metadata extraction."""

    return list(
        session.scalars(
            select(Capture.id)
            .where(
                Capture.deleted_at.is_(None),
                Capture.source_type == "youtube",
                Capture.processing_status.in_(YOUTUBE_EXTRACTION_RETRYABLE_STATUSES),
                Capture.extraction_attempts < max_attempts,
            )
            .order_by(Capture.created_at.asc(), Capture.id.asc())
            .limit(limit)
        )
    )


def _index_text(metadata: dict, youtube: YouTubeMetadata) -> str:
    submitted_text = str(metadata.get("submitted_text") or "").strip()
    parts = [
        submitted_text,
        youtube.title or "",
        youtube.channel or "",
        youtube.published_date or "",
        youtube.description or "",
        youtube.url,
    ]
    text = " ".join(" ".join(part.split()) for part in parts if part.strip())
    if len(text) <= MAX_INDEX_TEXT_LENGTH:
        return text
    return f"{text[: MAX_INDEX_TEXT_LENGTH - 1].rstrip()}…"


async def extract_youtube_capture(
    capture_id: uuid.UUID,
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> bool:
    """Fetch bounded YouTube metadata and persist success or failure."""

    del settings  # Kept in the worker interface for consistent scheduler wiring.
    session = session_factory()
    try:
        capture = session.get(Capture, capture_id)
        if (
            capture is None
            or capture.deleted_at is not None
            or capture.source_type != "youtube"
        ):
            return False
        youtube_metadata = (capture.source_metadata or {}).get("youtube")
        if (
            isinstance(youtube_metadata, dict)
            and youtube_metadata.get("extraction_status") in {"complete", "metadata_only"}
            and capture.processing_status in {"captured", "processing", "processed", "failed"}
        ):
            return True
        if capture.processing_status == YOUTUBE_EXTRACTING_STATUS:
            return False

        capture.processing_status = YOUTUBE_EXTRACTING_STATUS
        capture.processing_error = None
        capture.extraction_attempts += 1
        metadata = dict(capture.source_metadata or {})
        url = metadata.get("url")
        session.commit()
    finally:
        session.close()

    try:
        if not isinstance(url, str) or not url.strip():
            raise RuntimeError("The YouTube URL is unavailable")
        youtube = await asyncio.to_thread(fetch_youtube_metadata, url)
        metadata["youtube"] = youtube.as_dict()
    except Exception as exc:
        logger.exception("YouTube extraction failed", extra={"capture_id": str(capture_id)})
        failure_session = session_factory()
        try:
            failed_capture = failure_session.get(Capture, capture_id)
            if failed_capture is not None:
                failed_capture.processing_status = YOUTUBE_EXTRACTION_FAILED_STATUS
                failed_capture.processing_error = _error_message(exc)
                failure_session.commit()
        finally:
            failure_session.close()
        return False

    success_session = session_factory()
    try:
        extracted_capture = success_session.get(Capture, capture_id)
        if extracted_capture is None or extracted_capture.deleted_at is not None:
            return False
        extracted_capture.source_metadata = metadata
        extracted_capture.raw_text = _index_text(metadata, youtube)
        extracted_capture.processing_status = "captured"
        extracted_capture.processing_error = None
        success_session.commit()
        return True
    except Exception as exc:
        success_session.rollback()
        logger.exception(
            "YouTube extraction persistence failed",
            extra={"capture_id": str(capture_id)},
        )
        failed_capture = success_session.get(Capture, capture_id)
        if failed_capture is not None:
            failed_capture.processing_status = YOUTUBE_EXTRACTION_FAILED_STATUS
            failed_capture.processing_error = _error_message(exc)
            success_session.commit()
        return False
    finally:
        success_session.close()
