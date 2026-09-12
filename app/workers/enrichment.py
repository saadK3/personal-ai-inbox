"""Durable, retryable enrichment for captured text."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models.capture import Capture
from app.providers.openai import EnrichmentProvider, OpenAIProvider
from app.services.management import latest_corrections

logger = logging.getLogger(__name__)
PROCESSING_STATUS = "processing"
PROCESSED_STATUS = "processed"
FAILED_STATUS = "failed"
TRANSCRIPTION_PENDING_STATUS = "transcription_pending"
TRANSCRIBING_STATUS = "transcribing"
TRANSCRIPTION_FAILED_STATUS = "transcription_failed"
TRANSCRIPTION_UNSUPPORTED_STATUS = "transcription_unsupported"
AUTO_RETRYABLE_STATUSES = ("captured", "failed")
TRANSCRIPTION_RETRYABLE_STATUSES = (
    TRANSCRIPTION_PENDING_STATUS,
    TRANSCRIPTION_FAILED_STATUS,
)
MAX_AUDIO_BYTES = 25 * 1024 * 1024


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


def pending_transcription_ids(
    session: Session,
    *,
    max_attempts: int,
    limit: int = 50,
) -> list[uuid.UUID]:
    """Return voice captures that can resume transcription after a restart."""

    return list(
        session.scalars(
            select(Capture.id)
            .where(
                Capture.deleted_at.is_(None),
                Capture.source_type == "voice",
                Capture.processing_status.in_(TRANSCRIPTION_RETRYABLE_STATUSES),
                Capture.transcription_attempts < max_attempts,
            )
            .order_by(Capture.created_at.asc(), Capture.id.asc())
            .limit(limit)
        )
    )


def _audio_path(capture_id: uuid.UUID, metadata: dict, storage_dir: Path) -> Path:
    audio = metadata.get("audio")
    if not isinstance(audio, dict):
        raise RuntimeError("Voice capture has no audio metadata")
    local_path = audio.get("local_path")
    if isinstance(local_path, str) and local_path.strip():
        return Path(local_path)
    filename = audio.get("filename")
    suffix = Path(filename).suffix.lower() if isinstance(filename, str) else ".ogg"
    if not suffix or len(suffix) > 10:
        suffix = ".ogg"
    return storage_dir / "audio" / f"{capture_id}{suffix}"


def _download_audio(url: str, destination: Path) -> Path:
    """Download one Discord attachment to local durable storage."""

    request = Request(url, headers={"User-Agent": "personal-ai-inbox/1.0"})
    with urlopen(request, timeout=60) as response:  # noqa: S310  # URL comes from Discord.
        payload = response.read(MAX_AUDIO_BYTES + 1)
    if not payload:
        raise RuntimeError("The voice attachment was empty")
    if len(payload) > MAX_AUDIO_BYTES:
        raise RuntimeError("The voice attachment is larger than 25 MB")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return destination


async def transcribe_capture(
    capture_id: uuid.UUID,
    settings: Settings,
    session_factory: sessionmaker[Session],
    provider: EnrichmentProvider | None = None,
) -> bool:
    """Download and transcribe one durable voice capture, preserving failures."""

    session = session_factory()
    try:
        capture = session.get(Capture, capture_id)
        if (
            capture is None
            or capture.deleted_at is not None
            or capture.source_type != "voice"
        ):
            return False
        if capture.raw_transcription and capture.processing_status in {
            "captured",
            PROCESSING_STATUS,
            PROCESSED_STATUS,
        }:
            return True
        if capture.processing_status == TRANSCRIBING_STATUS:
            return False

        capture.processing_status = TRANSCRIBING_STATUS
        capture.processing_error = None
        capture.transcription_attempts += 1
        metadata = dict(capture.source_metadata or {})
        session.commit()
    finally:
        session.close()

    try:
        audio_metadata = metadata.get("audio")
        if not isinstance(audio_metadata, dict):
            raise RuntimeError("Voice capture has no audio metadata")
        destination = _audio_path(capture_id, metadata, settings.storage_dir)
        if destination.is_file() and destination.stat().st_size == 0:
            raise RuntimeError("The voice attachment was empty")
        if not destination.is_file():
            url = audio_metadata.get("url")
            if not isinstance(url, str) or not url.strip():
                raise RuntimeError("The voice attachment URL is unavailable")
            destination = await asyncio.to_thread(_download_audio, url, destination)
            metadata["audio"] = {**audio_metadata, "local_path": str(destination)}
            metadata_session = session_factory()
            try:
                stored_capture = metadata_session.get(Capture, capture_id)
                if stored_capture is not None:
                    stored_capture.source_metadata = metadata
                    metadata_session.commit()
            finally:
                metadata_session.close()

        active_provider = provider or OpenAIProvider(settings)
        transcript = await asyncio.to_thread(active_provider.transcribe, destination)
        if not transcript or not transcript.strip():
            raise RuntimeError("The transcription was empty")
        transcript = " ".join(transcript.split())
    except Exception as exc:
        logger.exception("Voice transcription failed", extra={"capture_id": str(capture_id)})
        failure_session = session_factory()
        try:
            failed_capture = failure_session.get(Capture, capture_id)
            if failed_capture is not None:
                failed_capture.processing_status = TRANSCRIPTION_FAILED_STATUS
                failed_capture.processing_error = _error_message(exc)
                failure_session.commit()
        finally:
            failure_session.close()
        return False

    success_session = session_factory()
    try:
        transcribed_capture = success_session.get(Capture, capture_id)
        if transcribed_capture is None or transcribed_capture.deleted_at is not None:
            return False
        transcribed_capture.raw_transcription = transcript
        transcribed_capture.raw_text = transcript
        transcribed_capture.processing_status = "captured"
        transcribed_capture.processing_error = None
        success_session.commit()
        return True
    except Exception as exc:
        success_session.rollback()
        logger.exception(
            "Voice transcription persistence failed",
            extra={"capture_id": str(capture_id)},
        )
        failure_capture = success_session.get(Capture, capture_id)
        if failure_capture is not None:
            failure_capture.processing_status = TRANSCRIPTION_FAILED_STATUS
            failure_capture.processing_error = _error_message(exc)
            success_session.commit()
        return False
    finally:
        success_session.close()


async def enrich_capture(
    capture_id: uuid.UUID,
    settings: Settings,
    session_factory: sessionmaker[Session],
    provider: EnrichmentProvider | None = None,
    *,
    store_summary: bool = True,
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
        corrections_session = session_factory()
        try:
            corrections = latest_corrections(corrections_session, capture_id)
        finally:
            corrections_session.close()
        normalized_text = corrections.get("meaning", result.normalized_text)
        summary = corrections.get("summary", result.summary)
        inferred_type = corrections.get("type", result.inferred_type)
        embedding = await asyncio.to_thread(active_provider.embed, normalized_text)
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
        processed_capture.normalized_text = normalized_text
        processed_capture.summary = (
            summary
            if store_summary or "summary" in corrections
            else None
        )
        processed_capture.inferred_type = inferred_type
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
