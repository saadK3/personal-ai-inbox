"""Durable, retryable GitHub repository metadata extraction."""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models.capture import Capture
from app.services.github import GitHubMetadata, fetch_github_metadata

logger = logging.getLogger(__name__)
GITHUB_EXTRACTION_PENDING_STATUS = "github_extraction_pending"
GITHUB_EXTRACTING_STATUS = "github_extracting"
GITHUB_EXTRACTION_FAILED_STATUS = "github_extraction_failed"
GITHUB_EXTRACTION_RETRYABLE_STATUSES = (
    GITHUB_EXTRACTION_PENDING_STATUS,
    GITHUB_EXTRACTION_FAILED_STATUS,
)
MAX_INDEX_TEXT_LENGTH = 5_000


def _error_message(error: Exception) -> str:
    message = str(error).strip() or error.__class__.__name__
    return message[:1_000]


def pending_github_ids(
    session: Session,
    *,
    max_attempts: int,
    limit: int = 50,
) -> list[uuid.UUID]:
    """Return GitHub captures that can resume metadata extraction."""

    return list(
        session.scalars(
            select(Capture.id)
            .where(
                Capture.deleted_at.is_(None),
                Capture.source_type == "github",
                Capture.processing_status.in_(GITHUB_EXTRACTION_RETRYABLE_STATUSES),
                Capture.extraction_attempts < max_attempts,
            )
            .order_by(Capture.created_at.asc(), Capture.id.asc())
            .limit(limit)
        )
    )


def _index_text(metadata: dict, github: GitHubMetadata) -> str:
    submitted_text = str(metadata.get("submitted_text") or "").strip()
    parts = [
        submitted_text,
        github.owner,
        github.repository,
        github.description or "",
        " ".join(github.topics),
        github.url,
    ]
    text = " ".join(" ".join(part.split()) for part in parts if part.strip())
    if len(text) <= MAX_INDEX_TEXT_LENGTH:
        return text
    return f"{text[: MAX_INDEX_TEXT_LENGTH - 1].rstrip()}…"


async def extract_github_capture(
    capture_id: uuid.UUID,
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> bool:
    """Fetch bounded GitHub metadata and persist success or failure."""

    del settings  # Kept in the worker interface for consistent scheduler wiring.
    session = session_factory()
    try:
        capture = session.get(Capture, capture_id)
        if (
            capture is None
            or capture.deleted_at is not None
            or capture.source_type != "github"
        ):
            return False
        github_metadata = (capture.source_metadata or {}).get("github")
        if (
            isinstance(github_metadata, dict)
            and github_metadata.get("extraction_status") in {"complete", "metadata_only"}
            and capture.processing_status in {"captured", "processing", "processed", "failed"}
        ):
            return True
        if capture.processing_status == GITHUB_EXTRACTING_STATUS:
            return False

        capture.processing_status = GITHUB_EXTRACTING_STATUS
        capture.processing_error = None
        capture.extraction_attempts += 1
        metadata = dict(capture.source_metadata or {})
        url = metadata.get("url")
        session.commit()
    finally:
        session.close()

    try:
        if not isinstance(url, str) or not url.strip():
            raise RuntimeError("The GitHub repository URL is unavailable")
        github = await asyncio.to_thread(fetch_github_metadata, url)
        metadata["github"] = github.as_dict()
    except Exception as exc:
        logger.exception("GitHub extraction failed", extra={"capture_id": str(capture_id)})
        failure_session = session_factory()
        try:
            failed_capture = failure_session.get(Capture, capture_id)
            if failed_capture is not None:
                failed_capture.processing_status = GITHUB_EXTRACTION_FAILED_STATUS
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
        extracted_capture.raw_text = _index_text(metadata, github)
        extracted_capture.processing_status = "captured"
        extracted_capture.processing_error = None
        success_session.commit()
        return True
    except Exception as exc:
        success_session.rollback()
        logger.exception(
            "GitHub extraction persistence failed",
            extra={"capture_id": str(capture_id)},
        )
        failed_capture = success_session.get(Capture, capture_id)
        if failed_capture is not None:
            failed_capture.processing_status = GITHUB_EXTRACTION_FAILED_STATUS
            failed_capture.processing_error = _error_message(exc)
            success_session.commit()
        return False
    finally:
        success_session.close()
