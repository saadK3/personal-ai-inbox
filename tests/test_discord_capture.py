import asyncio
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.channels.discord import process_discord_message
from app.core.config import Settings
from app.models import Capture, MessageReceipt


@dataclass
class FakeAuthor:
    id: int = 123
    bot: bool = False


@dataclass
class FakeChannel:
    id: int = 456
    sent_messages: list[str] = field(default_factory=list)

    async def send(self, text: str) -> None:
        self.sent_messages.append(text)


@dataclass
class FakeMessage:
    id: int
    content: str
    author: FakeAuthor = field(default_factory=FakeAuthor)
    channel: FakeChannel = field(default_factory=FakeChannel)
    guild: object | None = None
    attachments: list[object] = field(default_factory=list)


def run_message(
    message: FakeMessage,
    session_factory: sessionmaker[Session],
    settings: Settings | None = None,
) -> None:
    asyncio.run(
        process_discord_message(
            message,
            settings or Settings(discord_allowed_user_id=123),
            session_factory,
        )
    )


def test_text_is_saved_acknowledged_and_idempotent(
    session_factory: sessionmaker[Session],
) -> None:
    message = FakeMessage(1, "Need to buy bread and eggs.")

    run_message(message, session_factory)
    run_message(message, session_factory)

    assert message.channel.sent_messages == ["Saved: Need to buy bread and eggs."]
    with session_factory() as session:
        captures = list(session.scalars(select(Capture)))
        receipts = list(session.scalars(select(MessageReceipt)))
        assert len(captures) == 1
        assert len(receipts) == 1
        assert captures[0].raw_text == "Need to buy bread and eggs."
        assert captures[0].platform == "discord"
        assert captures[0].external_message_id == 1
        assert captures[0].conversation_id == 456
        assert captures[0].sender_id == 123
        assert captures[0].source_metadata["platform"] == "discord"


def test_unauthorized_user_and_server_messages_are_ignored(
    session_factory: sessionmaker[Session],
) -> None:
    unauthorized = FakeMessage(2, "Private note", author=FakeAuthor(id=999))
    server_message = FakeMessage(3, "Shared channel note", guild=object())

    run_message(unauthorized, session_factory)
    run_message(server_message, session_factory)

    assert unauthorized.channel.sent_messages == []
    assert server_message.channel.sent_messages == []
    with session_factory() as session:
        assert session.scalar(select(Capture.id)) is None
        assert session.scalar(select(MessageReceipt.platform)) is None


def test_recent_and_duplicate_undo_are_safe(
    session_factory: sessionmaker[Session],
) -> None:
    first = FakeMessage(10, "First note")
    second = FakeMessage(11, "Second note")
    recent = FakeMessage(12, "/recent")
    undo = FakeMessage(13, "/undo")

    run_message(first, session_factory)
    run_message(second, session_factory)
    run_message(recent, session_factory)
    run_message(undo, session_factory)
    run_message(undo, session_factory)

    assert "First note" in recent.channel.sent_messages[-1]
    assert "Second note" in recent.channel.sent_messages[-1]
    assert undo.channel.sent_messages == ["Undid: Second note"]
    with session_factory() as session:
        active = list(
            session.scalars(
                select(Capture).where(Capture.deleted_at.is_(None)).order_by(Capture.created_at)
            )
        )
        assert [capture.raw_text for capture in active] == ["First note"]


def test_commands_and_unsupported_messages_are_not_saved(
    session_factory: sessionmaker[Session],
) -> None:
    channel = FakeChannel()
    for message_id, content in ((20, "/ask restaurants"), (21, "/unknown"), (22, "")):
        run_message(FakeMessage(message_id, content, channel=channel), session_factory)

    with session_factory() as session:
        assert session.scalar(select(Capture.id)) is None
        assert len(list(session.scalars(select(MessageReceipt)))) == 3
    assert len(channel.sent_messages) == 3
