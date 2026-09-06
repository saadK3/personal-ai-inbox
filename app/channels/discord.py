import logging
from datetime import UTC, datetime
from typing import Any

import discord
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models.capture import Capture, MessageReceipt

logger = logging.getLogger(__name__)
PLATFORM = "discord"
DISCORD_MESSAGE_LIMIT = 2_000


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
                    "captures or /undo to remove the latest one."
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
                response_text = (
                    "Search is coming in the next slice. For now, send text "
                    "normally and I’ll save it."
                )
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
