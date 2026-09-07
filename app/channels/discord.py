import asyncio
import logging
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import discord
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models.capture import Capture, MessageReceipt
from app.providers.openai import EnrichmentProvider, OpenAIProvider
from app.workers.enrichment import (
    TRANSCRIPTION_FAILED_STATUS,
    TRANSCRIPTION_PENDING_STATUS,
    TRANSCRIPTION_UNSUPPORTED_STATUS,
    enrich_capture,
    pending_capture_ids,
    pending_transcription_ids,
    transcribe_capture,
)

logger = logging.getLogger(__name__)
PLATFORM = "discord"
DISCORD_MESSAGE_LIMIT = 2_000
SEARCH_RESULT_LIMIT = 5
TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
ISO_DATE_PATTERN = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
SEMANTIC_MIN_SIMILARITY = 0.55
SHORT_QUERY_SEMANTIC_MIN_SIMILARITY = 0.65
EnrichmentScheduler = Callable[[uuid.UUID], None]
TranscriptionScheduler = Callable[[uuid.UUID], None]
MessageRoute = Literal["command", "query", "capture"]
SUPPORTED_AUDIO_EXTENSIONS = {
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".ogg",
    ".wav",
    ".webm",
}
SUPPORTED_AUDIO_CONTENT_TYPES = {
    "audio/flac",
    "audio/mp4",
    "audio/m4a",
    "audio/mpeg",
    "audio/ogg",
    "audio/opus",
    "audio/wav",
    "audio/x-wav",
    "audio/webm",
}
MAX_DISCORD_ATTACHMENT_BYTES = 25 * 1024 * 1024
SEARCH_STOPWORDS = {
    "a",
    "an",
    "about",
    "and",
    "are",
    "did",
    "do",
    "does",
    "for",
    "have",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "the",
    "to",
    "was",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "you",
}
RETRIEVAL_CAPTURE_PATTERN = re.compile(
    r"\b(?:what|where|when|which|who|how|did|do|does|have|has|can|show|list|find|tell)\b"
    r".*\b(?:save|saved|capture|captures|memory|memories|note|notes|stored|sent)\b",
    re.IGNORECASE,
)
QUESTION_PREFIX_PATTERN = re.compile(
    r"^(?:what|where|when|which|who|whom|why|how|did|do|does|is|are|have|has|can|could|would|should|show|find|list|tell)\b",
    re.IGNORECASE,
)
HYPOTHETICAL_PREFIX_PATTERN = re.compile(
    r"^(?:what if|if i|should i build|could i build|would it be|idea:|thinking of)\b",
    re.IGNORECASE,
)
MEMORY_CUE_PATTERN = re.compile(
    r"\b(?:save|saved|sent|stored|bookmarked|capture|captured|memory|memories|"
    r"my notes|my captures)\b",
    re.IGNORECASE,
)
RECALL_ACTION_PATTERN = re.compile(
    r"\b(?:recommend(?:ed|ation)?|mention(?:ed)?|said|say|wanted to try|looked at|watched)\b",
    re.IGNORECASE,
)
RECALL_CONTEXT_PATTERN = re.compile(
    r"\b(?:what was that|what did(?:\s+\w+){0,4}\s+(?:say|mention|recommend)|"
    r"where did i|when did i|who did i)\b",
    re.IGNORECASE,
)
DATE_CUE_PATTERN = re.compile(
    r"\b(?:today|yesterday|this week|last week|this month|last month|a few weeks ago|recently)\b",
    re.IGNORECASE,
)
ADVICE_QUESTION_PATTERN = re.compile(
    r"^(?:what|where|how)\s+(?:should|could|would|can|do)\s+(?:i|you)\b|^what\s+(?:do|would|should)\s+you\s+recommend\b",
    re.IGNORECASE,
)


def _parse_command(text: str) -> tuple[str, str] | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    command, _, argument = stripped.partition(" ")
    return command.lower(), argument.strip()


def _is_natural_query(text: str) -> bool:
    """Return true only for high-confidence questions about saved memories."""

    stripped = " ".join(text.split())
    if not stripped or stripped.startswith("/"):
        return False
    if HYPOTHETICAL_PREFIX_PATTERN.match(stripped):
        return False
    if not QUESTION_PREFIX_PATTERN.match(stripped):
        return False
    if ADVICE_QUESTION_PATTERN.match(stripped):
        return False
    if MEMORY_CUE_PATTERN.search(stripped):
        return True
    has_recall_context = bool(RECALL_CONTEXT_PATTERN.search(stripped))
    has_recall_action = bool(RECALL_ACTION_PATTERN.search(stripped))
    has_past_marker = bool(
        re.search(
            r"\b(?:did|was|were|have|has|sent|mentioned|recommended|said|ago)\b",
            stripped,
            re.I,
        )
    )
    if has_recall_context:
        return True
    if has_recall_action and has_past_marker:
        return True
    return bool(DATE_CUE_PATTERN.search(stripped)) and bool(
        re.search(r"\b(?:my|i|from)\b", stripped, re.I)
    )


def _route_message(text: str) -> MessageRoute:
    """Classify a message while preserving capture as the safe default."""

    if _parse_command(text) is not None:
        return "command"
    if _is_natural_query(text):
        return "query"
    return "capture"


def _truncate(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def _format_recent(captures: list[Capture]) -> str:
    if not captures:
        return "No captures yet. Send me a note to save it."

    lines = ["Recent captures:"]
    for index, capture in enumerate(captures, start=1):
        created_at = capture.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        timestamp = created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        text = " ".join(capture.raw_text.split())
        if len(text) > 280:
            text = f"{text[:277]}..."
        lines.append(f"{index}. [{timestamp}] ({capture.processing_status}) {text}")
        if capture.processing_error:
            lines.append(f"   Error: {_truncate(capture.processing_error, 240)}")
        if len("\n".join(lines)) > DISCORD_MESSAGE_LIMIT - 20:
            lines.append("...more captures available later")
            break
    return _truncate("\n".join(lines))


def _search_tokens(text: str) -> list[str]:
    """Return unique, case-folded words suitable for exact lexical matching."""

    return list(
        dict.fromkeys(
            token.casefold()
            for token in TOKEN_PATTERN.findall(text)
            if len(token) > 1 and token.casefold() not in SEARCH_STOPWORDS
        )
    )


def _token_matches(query_token: str, capture_tokens: set[str]) -> bool:
    """Match common singular/plural forms without adding a heavyweight stemmer."""

    variants = {query_token}
    if query_token.endswith("ies") and len(query_token) > 4:
        variants.add(f"{query_token[:-3]}y")
    elif query_token.endswith("y") and len(query_token) > 3:
        variants.add(f"{query_token[:-1]}ies")
    elif query_token.endswith("s") and not query_token.endswith(("ss", "us", "is")):
        variants.add(query_token[:-1])
    else:
        variants.add(f"{query_token}s")
    return bool(variants & capture_tokens)


def _is_retrieval_question_capture(capture: Capture) -> bool:
    """Identify generic saved-inbox questions accidentally captured as notes."""

    raw_text = " ".join(capture.raw_text.split())
    inferred_type = (capture.inferred_type or "").casefold()
    summary = (capture.summary or "").casefold()
    if inferred_type in {"question", "query", "search"}:
        return True
    if "question" in summary and any(
        keyword in summary for keyword in ("saved", "capture", "memory", "note")
    ):
        return True
    return bool(RETRIEVAL_CAPTURE_PATTERN.search(raw_text))


def _capture_source(capture: Capture) -> str:
    metadata = capture.source_metadata or {}
    if capture.source_type == "voice":
        audio = metadata.get("audio")
        if isinstance(audio, dict) and audio.get("url"):
            return str(audio["url"])
    channel_id = metadata.get("channel_id")
    message_id = metadata.get("message_id")
    if capture.platform == PLATFORM and channel_id and message_id:
        return f"https://discord.com/channels/@me/{channel_id}/{message_id}"
    return capture.platform


def _format_search_results(captures: list[Capture], query: str) -> str:
    if not captures:
        return f'No saved captures matched "{query}".'

    lines = [f'Matches for "{query}":']
    for index, capture in enumerate(captures, start=1):
        created_at = capture.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        timestamp = created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        text = " ".join(capture.raw_text.split())
        if len(text) > 280:
            text = f"{text[:277]}..."
        lines.append(f"{index}. [{timestamp}] {text}")
        if capture.summary:
            lines.append(f"   Summary: {_truncate(capture.summary, 240)}")
        lines.append(f"   Source: {_capture_source(capture)}")
    return _truncate("\n".join(lines))


def _date_bounds(
    query: str,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Extract a small set of unambiguous UTC date filters from a query."""

    current = now or datetime.now(UTC)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    folded = query.casefold()
    if "yesterday" in folded:
        return day_start - timedelta(days=1), day_start
    if "today" in folded:
        return day_start, day_start + timedelta(days=1)
    if "last week" in folded:
        this_monday = day_start - timedelta(days=day_start.weekday())
        return this_monday - timedelta(days=7), this_monday
    if "this week" in folded:
        return day_start - timedelta(days=day_start.weekday()), day_start + timedelta(days=1)
    if "last month" in folded:
        first_this_month = day_start.replace(day=1)
        previous_month_start = (first_this_month - timedelta(days=1)).replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return previous_month_start, first_this_month
    if "this month" in folded:
        return day_start.replace(day=1), day_start + timedelta(days=1)

    match = ISO_DATE_PATTERN.search(query)
    if match:
        try:
            target = datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            return None, None
        return target, target + timedelta(days=1)
    return None, None


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


def _search_captures(
    session: Session,
    conversation_id: int,
    query: str,
    query_embedding: list[float] | None = None,
) -> list[Capture]:
    """Find active captures with hybrid lexical and semantic ranking."""

    tokens = _search_tokens(query)
    if not tokens and query_embedding is None:
        return []

    start, end = _date_bounds(query)
    base_conditions = [
        Capture.platform == PLATFORM,
        Capture.conversation_id == conversation_id,
        Capture.deleted_at.is_(None),
    ]
    if start is not None:
        base_conditions.append(Capture.created_at >= start)
    if end is not None:
        base_conditions.append(Capture.created_at < end)

    lexical_condition = or_(
        *(
            expression
            for token in tokens
            for expression in (
                Capture.raw_text.ilike(f"%{token}%"),
                Capture.normalized_text.ilike(f"%{token}%"),
                Capture.summary.ilike(f"%{token}%"),
            )
        )
    ) if tokens else None

    lexical_candidates: list[Capture] = []
    if lexical_condition is not None:
        lexical_candidates = list(
            session.scalars(select(Capture).where(*base_conditions, lexical_condition))
        )

    candidates_by_id = {capture.id: capture for capture in lexical_candidates}
    if query_embedding is not None:
        semantic_statement = select(Capture).where(
            *base_conditions,
            Capture.embedding.is_not(None),
        )
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            semantic_statement = semantic_statement.order_by(
                Capture.embedding.cosine_distance(query_embedding)
            ).limit(100)
        try:
            semantic_candidates = list(session.scalars(semantic_statement))
        except SQLAlchemyError:
            session.rollback()
            logger.exception("Semantic database search failed; using lexical candidates")
            semantic_candidates = []
        candidates_by_id.update({capture.id: capture for capture in semantic_candidates})

    candidates = list(candidates_by_id.values())

    query_text = " ".join(tokens)
    ranked: list[tuple[float, float, Capture]] = []
    for capture in candidates:
        if _is_retrieval_question_capture(capture):
            continue
        searchable_text = " ".join(
            part
            for part in (
                capture.raw_text,
                capture.normalized_text or "",
                capture.summary or "",
                " ".join(capture.topics or []),
                " ".join(
                    value
                    for values in (capture.entities or {}).values()
                    for value in values
                    if isinstance(value, str)
                ),
            )
            if part
        )
        capture_tokens = set(_search_tokens(searchable_text))
        matched_count = sum(_token_matches(token, capture_tokens) for token in tokens)
        semantic_score = _cosine_similarity(query_embedding, capture.embedding)
        if query_embedding is None and matched_count == 0:
            continue
        if query_embedding is not None and matched_count == 0:
            minimum_similarity = (
                SHORT_QUERY_SEMANTIC_MIN_SIMILARITY
                if len(tokens) <= 1
                else SEMANTIC_MIN_SIMILARITY
            )
            if semantic_score is None or semantic_score < minimum_similarity:
                continue
        normalized_capture = " ".join(_search_tokens(searchable_text))
        phrase_bonus = 100 if query_text and query_text in normalized_capture else 0
        # A full phrase or exact term remains a strong signal, while semantic
        # similarity can still surface a conceptually related memory.
        lexical_score = phrase_bonus + (matched_count * 5)
        semantic_component = max(semantic_score or 0.0, 0.0) * 100
        score = lexical_score + semantic_component
        ranked.append((score, semantic_score or 0.0, capture))

    ranked.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2].created_at,
            str(item[2].id),
        ),
        reverse=True,
    )
    return [capture for _, _, capture in ranked[:SEARCH_RESULT_LIMIT]]


async def _run_search(
    session: Session,
    conversation_id: int,
    query: str,
    provider: EnrichmentProvider | None,
) -> list[Capture]:
    """Run semantic search when available and safely fall back to lexical search."""

    query_embedding: list[float] | None = None
    if provider is not None:
        try:
            query_embedding = await asyncio.to_thread(provider.embed, query)
        except Exception:
            logger.exception("Semantic query embedding failed; using lexical search")
    return _search_captures(
        session,
        conversation_id=conversation_id,
        query=query,
        query_embedding=query_embedding,
    )


def _format_natural_query_results(captures: list[Capture], query: str) -> str:
    """Make the route visible and provide a recovery path if routing was wrong."""

    body = _format_search_results(captures, query)
    prefix = (
        "Search only — this message was not saved. "
        "To save it instead, send `/save <text>`.\n"
    )
    return _truncate(prefix + body)


def _source_metadata(message: Any) -> dict[str, Any]:
    return {
        "platform": PLATFORM,
        "message_id": str(message.id),
        "channel_id": str(message.channel.id),
        "author_id": str(message.author.id),
        "guild_id": str(message.guild.id) if message.guild is not None else None,
        "attachments": [
            {
                "id": str(attachment.id),
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "size": attachment.size,
                "url": attachment.url,
            }
            for attachment in message.attachments
        ],
    }


def _is_voice_attachment(attachment: Any) -> bool:
    content_type = (getattr(attachment, "content_type", None) or "").split(";", 1)[0]
    filename = str(getattr(attachment, "filename", ""))
    return content_type.casefold().startswith("audio/") or (
        Path(filename).suffix.casefold() in SUPPORTED_AUDIO_EXTENSIONS
    )


def _voice_attachment(message: Any) -> Any | None:
    attachments = list(getattr(message, "attachments", []) or [])
    for attachment in attachments:
        if _is_voice_attachment(attachment):
            return attachment
    return None


def _voice_metadata(message: Any, attachment: Any) -> dict[str, Any]:
    metadata = _source_metadata(message)
    filename = str(getattr(attachment, "filename", "voice-note.ogg"))
    content_type = getattr(attachment, "content_type", None)
    size = getattr(attachment, "size", None)
    metadata["audio"] = {
        "id": str(getattr(attachment, "id", "")),
        "filename": filename,
        "content_type": content_type,
        "size": size,
        "url": getattr(attachment, "url", None),
    }
    return metadata


def _voice_display_name(capture: Capture) -> str:
    audio = (capture.source_metadata or {}).get("audio")
    if isinstance(audio, dict) and audio.get("filename"):
        return str(audio["filename"])
    return "voice note"


async def _send_ack(message: Any, text: str) -> bool:
    try:
        await message.channel.send(_truncate(text))
    except Exception:
        logger.exception(
            "Failed to send Discord acknowledgement",
            extra={"channel_id": message.channel.id},
        )
        return False
    return True


async def process_discord_message(
    message: Any,
    settings: Settings,
    session_factory: sessionmaker[Session],
    provider: EnrichmentProvider | None = None,
    enrichment_scheduler: EnrichmentScheduler | None = None,
    transcription_scheduler: TranscriptionScheduler | None = None,
) -> None:
    """Handle one Discord message while keeping capture persistence synchronous and durable."""

    if getattr(message.author, "bot", False):
        return
    # V1 is intentionally DM-first. Server messages are ignored so private
    # captures cannot accidentally be written to a shared channel.
    if message.guild is not None:
        return
    if (
        settings.discord_allowed_user_id is None
        or message.author.id != settings.discord_allowed_user_id
    ):
        logger.warning(
            "Ignored Discord message from unauthorized user",
            extra={"user_id": message.author.id},
        )
        return

    session = session_factory()
    try:
        receipt_key = {"platform": PLATFORM, "external_message_id": message.id}
        if session.get(MessageReceipt, receipt_key) is not None:
            return

        session.add(
            MessageReceipt(
                platform=PLATFORM,
                external_message_id=message.id,
                conversation_id=message.channel.id,
                sender_id=message.author.id,
            )
        )
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            return

        text = message.content or ""
        command = _parse_command(text)

        if command is not None:
            command_name, _ = command
            retry_capture: Capture | None = None
            save_capture: Capture | None = None
            if command_name in {"/start", "/help"}:
                response_text = (
                    "Send me a note and I’ll save it, or ask a clear question "
                    "naturally to search. Use /recent to review captures, "
                    "/ask <query> to search explicitly, /save <text> to force "
                    "a save, /retry to reprocess a failed capture, or /undo "
                    "to remove the latest one."
                )
            elif command_name == "/recent":
                captures = list(
                    session.scalars(
                        select(Capture)
                        .where(
                            Capture.platform == PLATFORM,
                            Capture.conversation_id == message.channel.id,
                            Capture.deleted_at.is_(None),
                        )
                        .order_by(Capture.created_at.desc(), Capture.id.desc())
                        .limit(10)
                    )
                )
                response_text = _format_recent(captures)
            elif command_name == "/undo":
                capture = session.scalar(
                    select(Capture)
                    .where(
                        Capture.platform == PLATFORM,
                        Capture.conversation_id == message.channel.id,
                        Capture.deleted_at.is_(None),
                    )
                    .order_by(Capture.created_at.desc(), Capture.id.desc())
                    .limit(1)
                )
                if capture is None:
                    response_text = "There is nothing to undo."
                else:
                    capture.deleted_at = datetime.now(UTC)
                    response_text = f"Undid: {capture.raw_text}"
            elif command_name == "/ask":
                if not command[1]:
                    response_text = "Usage: /ask <query>. Example: /ask electrician"
                else:
                    matches = await _run_search(
                        session,
                        conversation_id=message.channel.id,
                        query=command[1],
                        provider=provider,
                    )
                    response_text = _format_search_results(matches, command[1])
            elif command_name == "/save":
                if not command[1]:
                    response_text = "Usage: /save <text>. Example: /save buy milk"
                else:
                    save_capture = Capture(
                        platform=PLATFORM,
                        external_message_id=message.id,
                        conversation_id=message.channel.id,
                        sender_id=message.author.id,
                        raw_text=command[1],
                        source_metadata=_source_metadata(message),
                    )
                    session.add(save_capture)
                    response_text = f"Saved: {command[1]}"
            elif command_name == "/retry":
                retry_capture = session.scalar(
                    select(Capture)
                    .where(
                        Capture.platform == PLATFORM,
                        Capture.conversation_id == message.channel.id,
                        Capture.deleted_at.is_(None),
                        Capture.processing_status.in_({"failed", TRANSCRIPTION_FAILED_STATUS}),
                    )
                    .order_by(Capture.created_at.desc(), Capture.id.desc())
                    .limit(1)
                )
                if retry_capture is None:
                    response_text = "No failed captures need retrying."
                elif retry_capture.processing_status == TRANSCRIPTION_FAILED_STATUS:
                    response_text = (
                        f"Retrying transcription for: {_voice_display_name(retry_capture)}"
                    )
                else:
                    response_text = f"Retrying enrichment for: {retry_capture.raw_text}"
            else:
                response_text = (
                    "I don’t recognize that command yet. Send text to save it, "
                    "or use /recent, /ask, /save, /retry, and /undo."
                )
            try:
                session.commit()
                if save_capture is not None:
                    session.refresh(save_capture)
            except IntegrityError:
                session.rollback()
                return
            await _send_ack(message, response_text)
            if save_capture is not None and enrichment_scheduler is not None:
                enrichment_scheduler(save_capture.id)
            if command_name == "/retry" and retry_capture is not None:
                if (
                    retry_capture.processing_status == TRANSCRIPTION_FAILED_STATUS
                    and transcription_scheduler is not None
                ):
                    transcription_scheduler(retry_capture.id)
                elif enrichment_scheduler is not None:
                    enrichment_scheduler(retry_capture.id)
            return

        if _is_natural_query(text):
            matches = await _run_search(
                session,
                conversation_id=message.channel.id,
                query=text,
                provider=provider,
            )
            session.commit()
            await _send_ack(message, _format_natural_query_results(matches, text))
            return

        voice_attachment = _voice_attachment(message)
        if voice_attachment is not None:
            filename = str(getattr(voice_attachment, "filename", "voice-note.ogg"))
            extension = Path(filename).suffix.casefold()
            content_type = (
                (getattr(voice_attachment, "content_type", None) or "")
                .split(";", 1)[0]
                .casefold()
            )
            size = getattr(voice_attachment, "size", None)
            supported_format = extension in SUPPORTED_AUDIO_EXTENSIONS or (
                content_type in SUPPORTED_AUDIO_CONTENT_TYPES
            )
            unsupported_reason: str | None = None
            if not supported_format:
                unsupported_reason = f"unsupported audio format ({filename})"
            elif isinstance(size, int) and size > MAX_DISCORD_ATTACHMENT_BYTES:
                unsupported_reason = "audio attachments must be 25 MB or smaller"

            voice_capture = Capture(
                platform=PLATFORM,
                external_message_id=message.id,
                conversation_id=message.channel.id,
                sender_id=message.author.id,
                source_type="voice",
                raw_text=f"[Voice note: {filename}]",
                source_metadata=_voice_metadata(message, voice_attachment),
                processing_status=(
                    TRANSCRIPTION_UNSUPPORTED_STATUS
                    if unsupported_reason is not None
                    else TRANSCRIPTION_PENDING_STATUS
                ),
                processing_error=unsupported_reason,
            )
            session.add(voice_capture)
            try:
                session.commit()
                session.refresh(voice_capture)
            except IntegrityError:
                session.rollback()
                return

            if unsupported_reason is not None:
                response_text = (
                    f"Voice note saved, but I can’t transcribe it: {unsupported_reason}."
                )
            elif transcription_scheduler is None:
                response_text = "Voice note saved; transcription is waiting for the worker."
            else:
                response_text = "Voice note saved; transcription started in the background."
            await _send_ack(message, response_text)
            if unsupported_reason is None and transcription_scheduler is not None:
                transcription_scheduler(voice_capture.id)
            return

        if not text.strip():
            session.commit()
            await _send_ack(
                message,
                "I can save text messages and voice notes right now. Support for "
                "links and images is coming next.",
            )
            return

        capture = Capture(
            platform=PLATFORM,
            external_message_id=message.id,
            conversation_id=message.channel.id,
            sender_id=message.author.id,
            raw_text=text,
            source_metadata=_source_metadata(message),
        )
        session.add(capture)
        try:
            session.commit()
            session.refresh(capture)
        except IntegrityError:
            session.rollback()
            return

        await _send_ack(message, f"Saved: {text}")
        if enrichment_scheduler is not None:
            enrichment_scheduler(capture.id)
    finally:
        session.close()


class PersonalInboxDiscordClient(discord.Client):
    """Discord Gateway client for private DM capture."""

    def __init__(self, settings: Settings, session_factory: sessionmaker[Session]) -> None:
        # DMs are sufficient for V1 and do not require reading messages from
        # shared servers. We intentionally ignore guild messages in the handler.
        intents = discord.Intents.default()
        intents.dm_messages = True
        super().__init__(intents=intents)
        self.settings = settings
        self.session_factory = session_factory
        self.provider: EnrichmentProvider | None = None
        if settings.openai_api_key is not None and settings.openai_api_key.get_secret_value():
            try:
                self.provider = OpenAIProvider(settings)
            except Exception:
                logger.exception("OpenAI enrichment is unavailable; captures will remain durable")
        self._enrichment_tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._transcription_tasks: dict[uuid.UUID, asyncio.Task[None]] = {}

    def schedule_enrichment(self, capture_id: uuid.UUID) -> None:
        """Queue enrichment in-process while retaining durable DB state."""

        if self.provider is None:
            logger.warning("Skipping enrichment because OPENAI_API_KEY is not configured")
            return
        current_task = self._enrichment_tasks.get(capture_id)
        if current_task is not None and not current_task.done():
            return
        task = asyncio.create_task(
            enrich_capture(
                capture_id,
                settings=self.settings,
                session_factory=self.session_factory,
                provider=self.provider,
            )
        )
        self._enrichment_tasks[capture_id] = task

        def _forget(done_task: asyncio.Task[None]) -> None:
            if self._enrichment_tasks.get(capture_id) is done_task:
                self._enrichment_tasks.pop(capture_id, None)
            if not done_task.cancelled():
                exception = done_task.exception()
                if exception is not None:
                    logger.error(
                        "Enrichment task crashed",
                        exc_info=(type(exception), exception, exception.__traceback__),
                    )

        task.add_done_callback(_forget)

    def schedule_transcription(self, capture_id: uuid.UUID) -> None:
        """Queue voice transcription and continue into normal enrichment."""

        if self.provider is None:
            logger.warning("Skipping transcription because OPENAI_API_KEY is not configured")
            return
        current_task = self._transcription_tasks.get(capture_id)
        if current_task is not None and not current_task.done():
            return
        task = asyncio.create_task(self._transcribe_and_enrich(capture_id))
        self._transcription_tasks[capture_id] = task

        def _forget(done_task: asyncio.Task[None]) -> None:
            if self._transcription_tasks.get(capture_id) is done_task:
                self._transcription_tasks.pop(capture_id, None)
            if not done_task.cancelled():
                exception = done_task.exception()
                if exception is not None:
                    logger.error(
                        "Transcription task crashed",
                        exc_info=(type(exception), exception, exception.__traceback__),
                    )

        task.add_done_callback(_forget)

    async def _transcribe_and_enrich(self, capture_id: uuid.UUID) -> None:
        succeeded = await transcribe_capture(
            capture_id,
            settings=self.settings,
            session_factory=self.session_factory,
            provider=self.provider,
        )
        if succeeded:
            self.schedule_enrichment(capture_id)

    def recover_pending_enrichment(self) -> None:
        """Resume captures left pending by a restart, up to the retry budget."""

        if self.provider is None:
            return
        session = self.session_factory()
        try:
            capture_ids = pending_capture_ids(
                session,
                max_attempts=self.settings.enrichment_max_attempts,
            )
        finally:
            session.close()
        for capture_id in capture_ids:
            self.schedule_enrichment(capture_id)

    def recover_pending_transcription(self) -> None:
        """Resume voice notes left before transcription completed."""

        if self.provider is None:
            return
        session = self.session_factory()
        try:
            capture_ids = pending_transcription_ids(
                session,
                max_attempts=self.settings.enrichment_max_attempts,
            )
        finally:
            session.close()
        for capture_id in capture_ids:
            self.schedule_transcription(capture_id)

    async def on_ready(self) -> None:
        logger.info("Discord bot connected as %s", self.user)
        self.recover_pending_transcription()
        self.recover_pending_enrichment()

    async def on_message(self, message: discord.Message) -> None:
        await process_discord_message(
            message,
            self.settings,
            self.session_factory,
            provider=self.provider,
            enrichment_scheduler=self.schedule_enrichment,
            transcription_scheduler=self.schedule_transcription,
        )
