import asyncio
import logging
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models.capture import Capture, MessageReceipt
from app.providers.openai import EnrichmentProvider, OpenAIProvider
from app.workers.enrichment import enrich_capture, pending_capture_ids

logger = logging.getLogger(__name__)
PLATFORM = "discord"
DISCORD_MESSAGE_LIMIT = 2_000
SEARCH_RESULT_LIMIT = 5
TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
ISO_DATE_PATTERN = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
SEMANTIC_MIN_SIMILARITY = 0.30
EnrichmentScheduler = Callable[[uuid.UUID], None]
SEARCH_STOPWORDS = {
    "a",
    "an",
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


def _parse_command(text: str) -> tuple[str, str] | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    command, _, argument = stripped.partition(" ")
    return command.lower(), argument.strip()


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
        matched_count = sum(token in capture_tokens for token in tokens)
        semantic_score = _cosine_similarity(query_embedding, capture.embedding)
        if query_embedding is None and matched_count == 0:
            continue
        if query_embedding is not None and matched_count == 0:
            if semantic_score is None or semantic_score < SEMANTIC_MIN_SIMILARITY:
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
            if command_name in {"/start", "/help"}:
                response_text = (
                    "Send me any text and I’ll save it. Use /recent to review "
                    "captures, /ask <query> to search, /retry to reprocess a "
                    "failed capture, or /undo to remove the latest one."
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
                    query_embedding: list[float] | None = None
                    if provider is not None:
                        try:
                            query_embedding = await asyncio.to_thread(provider.embed, command[1])
                        except Exception:
                            logger.exception(
                                "Semantic query embedding failed; using lexical search"
                            )
                    matches = _search_captures(
                        session,
                        conversation_id=message.channel.id,
                        query=command[1],
                        query_embedding=query_embedding,
                    )
                    response_text = _format_search_results(matches, command[1])
            elif command_name == "/retry":
                retry_capture = session.scalar(
                    select(Capture)
                    .where(
                        Capture.platform == PLATFORM,
                        Capture.conversation_id == message.channel.id,
                        Capture.deleted_at.is_(None),
                        Capture.processing_status == "failed",
                    )
                    .order_by(Capture.created_at.desc(), Capture.id.desc())
                    .limit(1)
                )
                if retry_capture is None:
                    response_text = "No failed captures need retrying."
                else:
                    response_text = f"Retrying enrichment for: {retry_capture.raw_text}"
            else:
                response_text = (
                    "I don’t recognize that command yet. Send text to save it, "
                    "or use /recent, /ask, /retry, and /undo."
                )
            session.commit()
            await _send_ack(message, response_text)
            if command_name == "/retry" and retry_capture is not None:
                if enrichment_scheduler is not None:
                    enrichment_scheduler(retry_capture.id)
            return

        if not text.strip():
            session.commit()
            await _send_ack(
                message,
                "I can save text messages right now. Support for voice notes, "
                "links, and images is coming next.",
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

    async def on_ready(self) -> None:
        logger.info("Discord bot connected as %s", self.user)
        self.recover_pending_enrichment()

    async def on_message(self, message: discord.Message) -> None:
        await process_discord_message(
            message,
            self.settings,
            self.session_factory,
            provider=self.provider,
            enrichment_scheduler=self.schedule_enrichment,
        )
