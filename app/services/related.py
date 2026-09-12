"""Conservative related-memory discovery and feedback."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.capture import Capture, CaptureRelation

RELATED_MIN_SIMILARITY = 0.84
OBVIOUS_DUPLICATE_SIMILARITY = 0.985
MAX_RELATED_MEMORIES = 3
MAX_NOTIFICATION_MEMORIES = 1
FEEDBACK_VALUES = {"useful", "not_useful"}
TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class RelatedMemory:
    """A relationship row together with the target capture it describes."""

    relation: CaptureRelation
    capture: Capture


def _cosine_similarity(left: list[float] | None, right: list[float] | None) -> float | None:
    if not left or not right or len(left) != len(right):
        return None
    try:
        dot = sum(float(a) * float(b) for a, b in zip(left, right, strict=True))
        left_norm = sum(float(value) ** 2 for value in left) ** 0.5
        right_norm = sum(float(value) ** 2 for value in right) ** 0.5
    except (TypeError, ValueError):
        return None
    if left_norm == 0 or right_norm == 0:
        return None
    return dot / (left_norm * right_norm)


def _tokens(capture: Capture) -> set[str]:
    metadata = capture.source_metadata or {}
    source_context: list[str] = []
    for key in ("web", "youtube", "github", "image"):
        value = metadata.get(key)
        if isinstance(value, dict):
            source_context.extend(str(item) for item in value.values() if isinstance(item, str))
    text = " ".join(
        part
        for part in (
            capture.raw_text,
            capture.normalized_text or "",
            capture.summary or "",
            " ".join(capture.topics or []),
            " ".join(source_context),
        )
        if part
    )
    return {
        token.casefold()
        for token in TOKEN_PATTERN.findall(text)
        if len(token) > 2
    }


def _source_identity(capture: Capture) -> str | None:
    metadata = capture.source_metadata or {}
    if capture.source_type in {"webpage", "youtube", "github"}:
        value = metadata.get("canonical_url") or metadata.get("url")
        if isinstance(value, str) and value.strip():
            return value.strip().casefold().rstrip("/")
    return None


def _is_obvious_duplicate(source: Capture, target: Capture, similarity: float) -> bool:
    source_identity = _source_identity(source)
    target_identity = _source_identity(target)
    if source_identity and target_identity and source_identity == target_identity:
        return True
    source_text = " ".join((source.raw_text or "").split()).casefold()
    target_text = " ".join((target.raw_text or "").split()).casefold()
    return bool(source_text and source_text == target_text) or (
        similarity >= OBVIOUS_DUPLICATE_SIMILARITY
    )


def _explain(source: Capture, target: Capture) -> str:
    overlap = sorted(_tokens(source) & _tokens(target))
    if overlap:
        terms = ", ".join(overlap[:4])
        return f"Both memories mention {terms}."
    if source.source_type == target.source_type:
        return f"Both are {source.source_type} memories with strong semantic overlap."
    return "The memories have strong semantic overlap."


def discover_related_memories(
    session: Session,
    capture_id: uuid.UUID,
    *,
    minimum_similarity: float = RELATED_MIN_SIMILARITY,
    limit: int = MAX_RELATED_MEMORIES,
) -> list[CaptureRelation]:
    """Persist the strongest non-duplicate relationships for one processed capture."""

    source = session.get(Capture, capture_id)
    if (
        source is None
        or source.deleted_at is not None
        or source.embedding is None
        or source.processing_status != "processed"
    ):
        return []

    existing = {
        relation.target_capture_id: relation
        for relation in session.scalars(
            select(CaptureRelation).where(CaptureRelation.source_capture_id == capture_id)
        )
    }
    for relation in existing.values():
        relation.active = False

    candidates = list(
        session.scalars(
            select(Capture)
            .where(
                Capture.platform == source.platform,
                Capture.conversation_id == source.conversation_id,
                Capture.id != source.id,
                Capture.deleted_at.is_(None),
                Capture.processing_status == "processed",
                Capture.embedding.is_not(None),
            )
        )
    )
    scored: list[tuple[float, Capture]] = []
    for target in candidates:
        # Relationships point backwards to an item that was already in the inbox.
        if target.created_at > source.created_at:
            continue
        similarity = _cosine_similarity(source.embedding, target.embedding)
        if similarity is None or similarity < minimum_similarity:
            continue
        if _is_obvious_duplicate(source, target, similarity):
            continue
        scored.append((similarity, target))
    scored.sort(key=lambda item: (item[0], item[1].created_at, str(item[1].id)), reverse=True)

    relationships: list[CaptureRelation] = []
    for similarity, target in scored[:limit]:
        relation = existing.get(target.id)
        if relation is None:
            relation = CaptureRelation(
                source_capture_id=source.id,
                target_capture_id=target.id,
                similarity=similarity,
                explanation=_explain(source, target),
            )
            session.add(relation)
        else:
            relation.similarity = similarity
            relation.explanation = _explain(source, target)
            relation.active = relation.feedback != "not_useful"
        relationships.append(relation)
    session.flush()
    return relationships


def related_memories(
    session: Session,
    capture_id: uuid.UUID,
    *,
    include_feedback: bool = False,
    limit: int = MAX_RELATED_MEMORIES,
) -> list[RelatedMemory]:
    """Return active relationships and their live target captures."""

    relations = list(
        session.scalars(
            select(CaptureRelation)
            .where(
                CaptureRelation.source_capture_id == capture_id,
                CaptureRelation.active.is_(True),
            )
            .order_by(CaptureRelation.similarity.desc(), CaptureRelation.created_at.desc())
            .limit(limit)
        )
    )
    result: list[RelatedMemory] = []
    for relation in relations:
        target = session.get(Capture, relation.target_capture_id)
        if target is None or target.deleted_at is not None:
            continue
        if not include_feedback and relation.feedback == "not_useful":
            continue
        result.append(RelatedMemory(relation=relation, capture=target))
    return result


def pending_related_memories(
    session: Session,
    capture_id: uuid.UUID,
    *,
    limit: int = MAX_NOTIFICATION_MEMORIES,
) -> list[RelatedMemory]:
    """Return unshown, unreviewed relationships eligible for a notification."""

    relations = list(
        session.scalars(
            select(CaptureRelation)
            .where(
                CaptureRelation.source_capture_id == capture_id,
                CaptureRelation.active.is_(True),
                CaptureRelation.feedback.is_(None),
                CaptureRelation.notified_at.is_(None),
            )
            .order_by(CaptureRelation.similarity.desc())
            .limit(limit)
        )
    )
    result: list[RelatedMemory] = []
    for relation in relations:
        target = session.get(Capture, relation.target_capture_id)
        if target is not None and target.deleted_at is None:
            result.append(RelatedMemory(relation=relation, capture=target))
    return result


def resolve_relation(
    session: Session,
    *,
    conversation_id: int,
    selector: str,
) -> CaptureRelation | None:
    """Resolve a relationship UUID or unique prefix within one DM."""

    normalized = selector.strip().lstrip("#").casefold()
    if not normalized:
        return None
    relations = list(
        session.scalars(
            select(CaptureRelation)
            .join(Capture, Capture.id == CaptureRelation.source_capture_id)
            .where(
                Capture.platform == "discord",
                Capture.conversation_id == conversation_id,
            )
        )
    )
    try:
        relation_uuid = uuid.UUID(normalized)
    except ValueError:
        relation_uuid = None
    if relation_uuid is not None:
        return next((item for item in relations if item.id == relation_uuid), None)
    matches = [item for item in relations if str(item.id).casefold().startswith(normalized)]
    return matches[0] if len(matches) == 1 else None


def record_feedback(
    session: Session,
    relation: CaptureRelation,
    value: str,
) -> bool:
    """Persist useful/not-useful feedback and suppress dismissed suggestions."""

    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized not in FEEDBACK_VALUES:
        return False
    relation.feedback = normalized
    relation.active = normalized != "not_useful"
    return True
