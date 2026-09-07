import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.channels.discord import _search_captures, process_discord_message
from app.core.config import Settings
from app.models import Capture, MessageReceipt
from app.providers.openai import EnrichmentResult, OpenAIProvider
from app.workers.enrichment import enrich_capture


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


class FakeProvider:
    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.enrich_calls: list[str] = []
        self.embed_calls: list[str] = []

    def enrich(self, raw_text: str) -> EnrichmentResult:
        self.enrich_calls.append(raw_text)
        if self.should_fail:
            raise RuntimeError("provider unavailable")
        return EnrichmentResult(
            normalized_text=raw_text.lower(),
            summary=f"Summary: {raw_text}",
            inferred_type="note",
            topics=["personal"],
            entities={"people": [], "places": [], "organizations": [], "dates": [], "products": []},
        )

    def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return [1.0, 0.0]


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


def test_ask_returns_ranked_matches_with_source_and_excludes_deleted(
    session_factory: sessionmaker[Session],
) -> None:
    first = FakeMessage(30, "Call the electrician about the kitchen lights")
    second = FakeMessage(31, "Electrician recommended a good hardware store")
    unrelated = FakeMessage(32, "Book a dentist appointment")
    query = FakeMessage(33, "/ask electrician")

    run_message(first, session_factory)
    run_message(second, session_factory)
    run_message(unrelated, session_factory)
    run_message(query, session_factory)

    response = query.channel.sent_messages[-1]
    assert 'Matches for "electrician":' in response
    assert "Call the electrician about the kitchen lights" in response
    assert "Electrician recommended a good hardware store" in response
    assert "Book a dentist appointment" not in response
    assert "https://discord.com/channels/@me/456/30" in response

    with session_factory() as session:
        capture = session.scalar(select(Capture).where(Capture.external_message_id == 30))
        assert capture is not None
        capture.deleted_at = capture.created_at
        session.commit()

    deleted_query = FakeMessage(34, "/ask electrician")
    run_message(deleted_query, session_factory)
    deleted_response = deleted_query.channel.sent_messages[-1]
    assert "Call the electrician about the kitchen lights" not in deleted_response
    assert "Electrician recommended a good hardware store" in deleted_response


def test_ask_handles_no_results_and_missing_query(
    session_factory: sessionmaker[Session],
) -> None:
    no_result = FakeMessage(40, "/ask something I never saved")
    missing_query = FakeMessage(41, "/ask")

    run_message(no_result, session_factory)
    run_message(missing_query, session_factory)

    assert no_result.channel.sent_messages == [
        'No saved captures matched "something I never saved".'
    ]
    assert missing_query.channel.sent_messages == [
        "Usage: /ask <query>. Example: /ask electrician"
    ]


def test_enrichment_persists_derived_fields_and_embedding(
    session_factory: sessionmaker[Session],
) -> None:
    message = FakeMessage(50, "Ahmed recommended a ramen place in F7")
    run_message(message, session_factory)
    with session_factory() as session:
        capture = session.scalar(select(Capture).where(Capture.external_message_id == 50))
        assert capture is not None
        capture_id = capture.id

    provider = FakeProvider()
    succeeded = asyncio.run(
        enrich_capture(
            capture_id,
            Settings(discord_allowed_user_id=123),
            session_factory,
            provider=provider,
        )
    )

    assert succeeded is True
    assert provider.enrich_calls == ["Ahmed recommended a ramen place in F7"]
    with session_factory() as session:
        capture = session.get(Capture, capture_id)
        assert capture is not None
        assert capture.processing_status == "processed"
        assert capture.processing_error is None
        assert capture.enrichment_attempts == 1
        assert capture.normalized_text == "ahmed recommended a ramen place in f7"
        assert capture.summary == "Summary: Ahmed recommended a ramen place in F7"
        assert capture.inferred_type == "note"
        assert capture.topics == ["personal"]
        assert capture.embedding == [1.0, 0.0]
        assert capture.embedding_model == "text-embedding-3-small"
        assert capture.processed_at is not None


def test_provider_failure_preserves_capture_and_retry_can_succeed(
    session_factory: sessionmaker[Session],
) -> None:
    message = FakeMessage(51, "Remember to renew the passport")
    run_message(message, session_factory)
    with session_factory() as session:
        capture = session.scalar(select(Capture).where(Capture.external_message_id == 51))
        assert capture is not None
        capture_id = capture.id

    settings = Settings(discord_allowed_user_id=123)
    failed = asyncio.run(
        enrich_capture(
            capture_id,
            settings,
            session_factory,
            provider=FakeProvider(should_fail=True),
        )
    )
    assert failed is False
    with session_factory() as session:
        capture = session.get(Capture, capture_id)
        assert capture is not None
        assert capture.raw_text == "Remember to renew the passport"
        assert capture.processing_status == "failed"
        assert capture.processing_error == "provider unavailable"
        assert capture.enrichment_attempts == 1

    retried = asyncio.run(
        enrich_capture(capture_id, settings, session_factory, provider=FakeProvider())
    )
    assert retried is True
    with session_factory() as session:
        capture = session.get(Capture, capture_id)
        assert capture is not None
        assert capture.processing_status == "processed"
        assert capture.enrichment_attempts == 2


def test_retry_command_schedules_latest_failed_capture(
    session_factory: sessionmaker[Session],
) -> None:
    message = FakeMessage(55, "Follow up with the accountant")
    run_message(message, session_factory)
    with session_factory() as session:
        capture = session.scalar(select(Capture).where(Capture.external_message_id == 55))
        assert capture is not None
        capture.processing_status = "failed"
        capture.processing_error = "temporary outage"
        capture_id = capture.id
        session.commit()

    retry = FakeMessage(56, "/retry")
    scheduled: list[object] = []
    asyncio.run(
        process_discord_message(
            retry,
            Settings(discord_allowed_user_id=123),
            session_factory,
            enrichment_scheduler=scheduled.append,
        )
    )

    assert retry.channel.sent_messages == ["Retrying enrichment for: Follow up with the accountant"]
    assert scheduled == [capture_id]


def test_ask_uses_semantic_matches_and_keeps_exact_matches_strong(
    session_factory: sessionmaker[Session],
) -> None:
    first = FakeMessage(60, "Ahmed recommended a ramen place in F7")
    exact = FakeMessage(61, "Ramen restaurant to try this weekend")
    run_message(first, session_factory)
    run_message(exact, session_factory)
    with session_factory() as session:
        captures = list(session.scalars(select(Capture).order_by(Capture.external_message_id)))
        captures[0].normalized_text = "friend suggested Japanese noodles"
        captures[0].embedding = [0.7, 0.7]
        captures[0].summary = "A friend recommended an Asian restaurant"
        captures[1].normalized_text = "ramen restaurant to try"
        captures[1].embedding = [1.0, 0.0]
        session.commit()

    query = FakeMessage(62, "/ask Asian food my friend suggested")
    run_message(query, session_factory, Settings(discord_allowed_user_id=123))
    # This direct call mirrors the provider-backed path used by the Discord client.
    with session_factory() as session:
        matches = _search_captures(
            session,
            conversation_id=456,
            query="Asian food my friend suggested",
            query_embedding=[1.0, 0.0],
        )
    assert [capture.external_message_id for capture in matches] == [61, 60]


def test_ask_excludes_generic_saved_questions_from_semantic_results(
    session_factory: sessionmaker[Session],
) -> None:
    recommendation = FakeMessage(65, "A friend recommended a ramen restaurant near F7")
    generic_question = FakeMessage(66, "What have I saved?")
    run_message(recommendation, session_factory)
    run_message(generic_question, session_factory)
    with session_factory() as session:
        first = session.scalar(select(Capture).where(Capture.external_message_id == 65))
        second = session.scalar(select(Capture).where(Capture.external_message_id == 66))
        assert first is not None and second is not None
        first.normalized_text = "friend recommended Japanese noodles"
        first.summary = "Recommendation for a ramen restaurant near F7 from a friend."
        first.embedding = [0.95, 0.31]
        second.normalized_text = "question about saved items"
        second.summary = "A question asking to review saved items."
        second.embedding = [0.93, 0.37]
        session.commit()

        matches = _search_captures(
            session,
            conversation_id=456,
            query="what did my friend recommend?",
            query_embedding=[1.0, 0.0],
        )

    assert [capture.external_message_id for capture in matches] == [65]


def test_date_sensitive_search_filters_by_saved_date(
    session_factory: sessionmaker[Session],
) -> None:
    today = FakeMessage(70, "Restaurant recommendation for today")
    yesterday = FakeMessage(71, "Restaurant recommendation from yesterday")
    run_message(today, session_factory)
    run_message(yesterday, session_factory)
    now = datetime.now(UTC)
    with session_factory() as session:
        current = session.scalar(select(Capture).where(Capture.external_message_id == 70))
        previous = session.scalar(select(Capture).where(Capture.external_message_id == 71))
        assert current is not None and previous is not None
        current.created_at = now
        previous.created_at = now - timedelta(days=1)
        session.commit()
        matches = _search_captures(session, 456, "today restaurant")
    assert [capture.external_message_id for capture in matches] == [70]


def test_openai_provider_passes_explicit_economical_models() -> None:
    class FakeResponses:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def create(self, **kwargs: object) -> SimpleNamespace:
            self.kwargs = kwargs
            return SimpleNamespace(
                output_text=json.dumps(
                    {
                        "normalized_text": "short note",
                        "summary": "A short note.",
                        "inferred_type": "note",
                        "topics": ["personal"],
                        "entities": {
                            "people": [],
                            "places": [],
                            "organizations": [],
                            "dates": [],
                            "products": [],
                        },
                    }
                )
            )

    class FakeEmbeddings:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def create(self, **kwargs: object) -> SimpleNamespace:
            self.kwargs = kwargs
            return SimpleNamespace(data=[SimpleNamespace(embedding=[0.0] * 1536)])

    responses = FakeResponses()
    embeddings = FakeEmbeddings()
    provider = OpenAIProvider(
        Settings(openai_api_key="test-key"),
        client=SimpleNamespace(responses=responses, embeddings=embeddings),
    )
    provider.enrich("short note")
    provider.embed("short note")

    assert responses.kwargs["model"] == "gpt-5.6-luna"
    assert embeddings.kwargs["model"] == "text-embedding-3-small"
    assert embeddings.kwargs["dimensions"] == 1536
