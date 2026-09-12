"""User-controlled capture selection and correction helpers."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.capture import Capture, CaptureCorrection

CORRECTABLE_FIELDS: Mapping[str, str] = {
    "summary": "summary",
    "type": "inferred_type",
    "meaning": "normalized_text",
}
CORRECTION_FIELD_LABELS: Mapping[str, str] = {
    "summary": "summary",
    "type": "type",
    "meaning": "meaning",
}
MAX_CORRECTION_LENGTH = 2_000


def _active_captures(
    session: Session,
    conversation_id: int,
) -> list[Capture]:
    return list(
        session.scalars(
            select(Capture)
            .where(
                Capture.platform == "discord",
                Capture.conversation_id == conversation_id,
                Capture.deleted_at.is_(None),
            )
            .order_by(Capture.created_at.desc(), Capture.id.desc())
        )
    )


def resolve_capture(
    session: Session,
    *,
    conversation_id: int,
    selector: str,
    include_deleted: bool = False,
) -> Capture | None:
    """Resolve a recent ordinal, full UUID, or unique UUID prefix."""

    normalized = selector.strip().lstrip("#")
    if not normalized:
        return None
    captures = _active_captures(session, conversation_id)
    if normalized.isdecimal():
        ordinal = int(normalized)
        if ordinal < 1 or ordinal > len(captures):
            return None
        return captures[ordinal - 1]

    candidates = captures
    if include_deleted:
        candidates = list(
            session.scalars(
                select(Capture)
                .where(
                    Capture.platform == "discord",
                    Capture.conversation_id == conversation_id,
                )
                .order_by(Capture.created_at.desc(), Capture.id.desc())
            )
        )
    try:
        capture_uuid = uuid.UUID(normalized)
    except ValueError:
        capture_uuid = None
    if capture_uuid is not None:
        for capture in candidates:
            if capture.id == capture_uuid:
                return capture
        return None
    folded = normalized.casefold()
    matches = [capture for capture in candidates if str(capture.id).startswith(folded)]
    return matches[0] if len(matches) == 1 else None


def latest_capture(
    session: Session,
    *,
    conversation_id: int,
) -> Capture | None:
    """Return the most recent capture, including a deleted one for safe undo."""

    return session.scalar(
        select(Capture)
        .where(
            Capture.platform == "discord",
            Capture.conversation_id == conversation_id,
        )
        .order_by(Capture.created_at.desc(), Capture.id.desc())
        .limit(1)
    )


def latest_corrections(session: Session, capture_id: uuid.UUID) -> dict[str, str]:
    """Return the latest user correction for each derived field."""

    result: dict[str, str] = {}
    rows = session.scalars(
        select(CaptureCorrection)
        .where(CaptureCorrection.capture_id == capture_id)
        .order_by(CaptureCorrection.created_at.asc(), CaptureCorrection.id.asc())
    )
    for correction in rows:
        if correction.field in CORRECTABLE_FIELDS:
            result[correction.field] = correction.new_value
    return result


def apply_correction(
    session: Session,
    capture: Capture,
    *,
    field: str,
    value: str,
) -> bool:
    """Apply and audit a correction without mutating the original raw capture."""

    normalized_field = field.strip().casefold()
    model_field = CORRECTABLE_FIELDS.get(normalized_field)
    cleaned_value = " ".join(value.split())[:MAX_CORRECTION_LENGTH].strip()
    if model_field is None or not cleaned_value:
        return False
    old_value = getattr(capture, model_field)
    if old_value == cleaned_value:
        return False
    session.add(
        CaptureCorrection(
            capture_id=capture.id,
            field=normalized_field,
            old_value=old_value,
            new_value=cleaned_value,
        )
    )
    setattr(capture, model_field, cleaned_value)
    # The corrected meaning must not retain an embedding generated from stale data.
    capture.embedding = None
    capture.embedding_model = None
    capture.processed_at = None
    capture.processing_status = "captured"
    capture.processing_error = None
    return True


def correction_help() -> str:
    return "Usage: /correct <item> <summary|type|meaning> <new value>"
