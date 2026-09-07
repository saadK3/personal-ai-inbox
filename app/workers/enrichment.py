"""Durable, retryable enrichment for captured text."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models.capture import Capture
from app.providers.openai import EnrichmentProvider, OpenAIProvider

logger = logging.getLogger(__name__)
PROCESSING_STATUS = "processing"
PROCESSED_STATUS = "processed"
FAILED_STATUS = "failed"
AUTO_RETRYABLE_STATUSES = ("captured", "failed")


def _error_message(error: Exception) -> str:
    message = str(error).strip() or error.__class__.__name__
    return message[:1_000]


def pending_capture_ids(
    session: Session,
    *,
    max_attempts: int,
    limit: int = 50,
) -> list[uuid.UUID]:
    """Return captures that can be recovered after a process restart."""

    return list(
        session.scalars(
            select(Capture.id)
            .where(
                Capture.deleted_at.is_(None),
                Capture.processing_status.in_(AUTO_RETRYABLE_STATUSES),
                Capture.enrichment_attempts < max_attempts,
            )
            .order_by(Capture.created_at.asc(), Capture.id.asc())
            .limit(limit)
        )
    )


async def enrich_capture(
    capture_id: uuid.UUID,
    settings: Settings,
    session_factory: sessionmaker[Session],
    provider: EnrichmentProvider | None = None,
) -> bool:
    """Enrich one capture and persist success or failure without losing raw text."""

    session = session_factory()
    try:
        capture = session.get(Capture, capture_id)
        if capture is None or capture.deleted_at is not None:
            return False
        if capture.processing_status == PROCESSED_STATUS and capture.embedding is not None:
            return True
        if capture.processing_status == PROCESSING_STATUS:
            return False

        capture.processing_status = PROCESSING_STATUS
        capture.processing_error = None
        capture.enrichment_attempts += 1
        raw_text = capture.raw_text
        session.commit()
    finally:
        session.close()

    try:
        active_provider = provider or OpenAIProvider(settings)
        # The SDK is synchronous. Keep both network calls off Discord's event loop.
        result = await asyncio.to_thread(active_provider.enrich, raw_text)
        embedding = await asyncio.to_thread(active_provider.embed, result.normalized_text)
    except Exception as exc:
        logger.exception("Capture enrichment failed", extra={"capture_id": str(capture_id)})
        failure_session = session_factory()
        try:
            failed_capture = failure_session.get(Capture, capture_id)
            if failed_capture is not None:
                failed_capture.processing_status = FAILED_STATUS
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
        processed_capture.normalized_text = result.normalized_text
        processed_capture.summary = result.summary
        processed_capture.inferred_type = result.inferred_type
        processed_capture.topics = result.topics
        processed_capture.entities = result.entities
        processed_capture.embedding = embedding
        processed_capture.embedding_model = settings.openai_embedding_model
        processed_capture.processing_status = PROCESSED_STATUS
        processed_capture.processing_error = None
        processed_capture.processed_at = datetime.now(UTC)
        success_session.commit()
        return True
    except Exception as exc:
        success_session.rollback()
        logger.exception(
            "Capture enrichment persistence failed",
            extra={"capture_id": str(capture_id)},
        )
        failure_capture = success_session.get(Capture, capture_id)
        if failure_capture is not None:
            failure_capture.processing_status = FAILED_STATUS
            failure_capture.processing_error = _error_message(exc)
            success_session.commit()
        return False
    finally:
        success_session.close()
