import logging
import re
from datetime import UTC, datetime
from typing import Any

import discord
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models.capture import Capture, MessageReceipt

logger = logging.getLogger(__name__)
PLATFORM = "discord"
DISCORD_MESSAGE_LIMIT = 2_000
SEARCH_RESULT_LIMIT = 5
TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)


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
        lines.append(f"{index}. [{timestamp}] {text}")
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
            if len(token) > 1
        )
    )


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
        lines.append(f"   Source: {_capture_source(capture)}")
    return _truncate("\n".join(lines))


def _search_captures(
    session: Session,
    conversation_id: int,
    query: str,
) -> list[Capture]:
    """Find active captures with deterministic token-overlap ranking.

    The database performs the case-insensitive lexical prefilter. Ranking is
    then done in Python so the same behavior is covered by the SQLite test
    fixture and PostgreSQL production database.
    """

    tokens = _search_tokens(query)
    if not tokens:
        return []

    candidates = list(
        session.scalars(
            select(Capture).where(
                Capture.platform == PLATFORM,
                Capture.conversation_id == conversation_id,
                Capture.deleted_at.is_(None),
                or_(*(Capture.raw_text.ilike(f"%{token}%") for token in tokens)),
            )
        )
    )

    query_text = " ".join(tokens)
    ranked: list[tuple[int, Capture]] = []
    for capture in candidates:
        capture_tokens = set(_search_tokens(capture.raw_text))
        matched_count = sum(token in capture_tokens for token in tokens)
        if matched_count == 0:
            continue
        normalized_capture = " ".join(_search_tokens(capture.raw_text))
        phrase_bonus = 100 if query_text in normalized_capture else 0
        score = phrase_bonus + (matched_count * 10)
        ranked.append((score, capture))

    ranked.sort(
        key=lambda item: (
            item[0],
            item[1].created_at,
            str(item[1].id),
        ),
        reverse=True,
    )
    return [capture for _, capture in ranked[:SEARCH_RESULT_LIMIT]]


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
            if command_name in {"/start", "/help"}:
                response_text = (
                    "Send me any text and I’ll save it. Use /recent to review "
                    "captures, /ask <query> to search, or /undo to remove the "
                    "latest one."
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
                    matches = _search_captures(
                        session,
                        conversation_id=message.channel.id,
                        query=command[1],
                    )
                    response_text = _format_search_results(matches, command[1])
            else:
                response_text = (
                    "I don’t recognize that command yet. Send text to save it, "
                    "or use /recent and /undo."
                )
            session.commit()
            await _send_ack(message, response_text)
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

    async def on_ready(self) -> None:
        logger.info("Discord bot connected as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        await process_discord_message(message, self.settings, self.session_factory)
