"""Durable, retryable metadata extraction for webpage captures."""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models.capture import Capture
from app.services.webpage import WebpageMetadata, fetch_webpage_metadata

logger = logging.getLogger(__name__)
WEB_EXTRACTION_PENDING_STATUS = "web_extraction_pending"
WEB_EXTRACTING_STATUS = "web_extracting"
WEB_EXTRACTION_FAILED_STATUS = "web_extraction_failed"
WEB_EXTRACTION_RETRYABLE_STATUSES = (
    WEB_EXTRACTION_PENDING_STATUS,
    WEB_EXTRACTION_FAILED_STATUS,
)
MAX_INDEX_TEXT_LENGTH = 5_000


def _error_message(error: Exception) -> str:
    message = str(error).strip() or error.__class__.__name__
    return message[:1_000]


def pending_webpage_ids(
    session: Session,
    *,
    max_attempts: int,
    limit: int = 50,
) -> list[uuid.UUID]:
    """Return webpage captures that can resume metadata extraction."""

    return list(
        session.scalars(
            select(Capture.id)
            .where(
                Capture.deleted_at.is_(None),
                Capture.source_type == "webpage",
                Capture.processing_status.in_(WEB_EXTRACTION_RETRYABLE_STATUSES),
                Capture.extraction_attempts < max_attempts,
            )
            .order_by(Capture.created_at.asc(), Capture.id.asc())
            .limit(limit)
        )
    )


def _index_text(metadata: dict, webpage: WebpageMetadata) -> str:
    submitted_text = str(metadata.get("submitted_text") or "").strip()
    parts = [
        submitted_text,
        webpage.title or "",
        webpage.description or "",
        webpage.author or "",
        webpage.published_date or "",
        " ".join(webpage.headings),
        webpage.url,
    ]
    text = " ".join(" ".join(part.split()) for part in parts if part.strip())
    if len(text) <= MAX_INDEX_TEXT_LENGTH:
        return text
    return f"{text[: MAX_INDEX_TEXT_LENGTH - 1].rstrip()}…"


async def extract_webpage_capture(
    capture_id: uuid.UUID,
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> bool:
    """Fetch bounded webpage metadata and persist success or failure."""

    session = session_factory()
    try:
        capture = session.get(Capture, capture_id)
        if (
            capture is None
            or capture.deleted_at is not None
            or capture.source_type != "webpage"
        ):
            return False
        web_metadata = (capture.source_metadata or {}).get("web")
        if (
            isinstance(web_metadata, dict)
            and web_metadata.get("extraction_status") in {"complete", "metadata_only"}
            and capture.processing_status in {"captured", "processing", "processed", "failed"}
        ):
            return True
        if capture.processing_status == WEB_EXTRACTING_STATUS:
            return False

        capture.processing_status = WEB_EXTRACTING_STATUS
        capture.processing_error = None
        capture.extraction_attempts += 1
        metadata = dict(capture.source_metadata or {})
        url = metadata.get("url")
        session.commit()
    finally:
        session.close()

    try:
        if not isinstance(url, str) or not url.strip():
            raise RuntimeError("The webpage URL is unavailable")
        webpage = await asyncio.to_thread(fetch_webpage_metadata, url)
        metadata["web"] = webpage.as_dict()
    except Exception as exc:
        logger.exception("Webpage extraction failed", extra={"capture_id": str(capture_id)})
        failure_session = session_factory()
        try:
            failed_capture = failure_session.get(Capture, capture_id)
            if failed_capture is not None:
                failed_capture.processing_status = WEB_EXTRACTION_FAILED_STATUS
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
        extracted_capture.raw_text = _index_text(metadata, webpage)
        extracted_capture.processing_status = "captured"
        extracted_capture.processing_error = None
        success_session.commit()
        return True
    except Exception as exc:
        success_session.rollback()
        logger.exception(
            "Webpage extraction persistence failed",
            extra={"capture_id": str(capture_id)},
        )
        failed_capture = success_session.get(Capture, capture_id)
        if failed_capture is not None:
            failed_capture.processing_status = WEB_EXTRACTION_FAILED_STATUS
            failed_capture.processing_error = _error_message(exc)
            success_session.commit()
        return False
    finally:
        success_session.close()
