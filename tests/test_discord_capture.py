import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.channels.discord import _route_message, _search_captures, process_discord_message
from app.core.config import Settings
from app.models import Capture, MessageReceipt
from app.providers.openai import EnrichmentResult, OpenAIProvider
from app.services.webpage import WebpageMetadata
from app.workers.enrichment import (
    TRANSCRIPTION_FAILED_STATUS,
    TRANSCRIPTION_PENDING_STATUS,
    TRANSCRIPTION_UNSUPPORTED_STATUS,
    enrich_capture,
    transcribe_capture,
)
from app.workers.webpage import (
    WEB_EXTRACTION_FAILED_STATUS,
    WEB_EXTRACTION_PENDING_STATUS,
    extract_webpage_capture,
)


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


@dataclass
class FakeAttachment:
    id: int = 900
    filename: str = "voice-note.ogg"
    content_type: str | None = "audio/ogg"
    size: int = 128
    url: str = "https://cdn.discordapp.com/attachments/900/voice-note.ogg"


class FakeProvider:
    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.transcription_should_fail = False
        self.transcript = "Remember to book the dentist next Tuesday"
        self.enrich_calls: list[str] = []
        self.embed_calls: list[str] = []
        self.transcribe_calls: list[Path] = []

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

    def transcribe(self, audio_path: Path) -> str:
        self.transcribe_calls.append(audio_path)
        if self.transcription_should_fail:
            raise RuntimeError("transcription provider unavailable")
        return self.transcript


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


def test_natural_router_keeps_ambiguous_and_idea_questions_safe() -> None:
    assert _route_message("/ask electrician") == "command"
    assert _route_message("What restaurant did my friend recommend?") == "query"
    assert _route_message("Did I save anything about Norway?") == "query"
    assert _route_message("What if I built an AI inbox?") == "capture"
    assert _route_message("What should I eat tonight?") == "capture"
    assert _route_message("What do you recommend?") == "capture"
    assert _route_message("Remember to buy milk") == "capture"


def test_voice_note_is_persisted_before_transcription_and_scheduled(
    session_factory: sessionmaker[Session],
) -> None:
    message = FakeMessage(23, "", attachments=[FakeAttachment()])
    scheduled: list[object] = []
    asyncio.run(
        process_discord_message(
            message,
            Settings(discord_allowed_user_id=123),
            session_factory,
            transcription_scheduler=scheduled.append,
        )
    )

    assert message.channel.sent_messages == [
        "Voice note saved; transcription started in the background."
    ]
    with session_factory() as session:
        capture = session.scalar(select(Capture).where(Capture.external_message_id == 23))
        assert capture is not None
        assert capture.source_type == "voice"
        assert capture.raw_text == "[Voice note: voice-note.ogg]"
        assert capture.raw_transcription is None
        assert capture.processing_status == TRANSCRIPTION_PENDING_STATUS
        assert capture.source_metadata["audio"]["url"].endswith("voice-note.ogg")
        assert scheduled == [capture.id]


def test_unsupported_voice_format_is_saved_with_clear_status(
    session_factory: sessionmaker[Session],
) -> None:
    message = FakeMessage(
        24,
        "",
        attachments=[FakeAttachment(filename="recording.xyz", content_type="audio/x-unknown")],
    )
    scheduled: list[object] = []
    asyncio.run(
        process_discord_message(
            message,
            Settings(discord_allowed_user_id=123),
            session_factory,
            transcription_scheduler=scheduled.append,
        )
    )

    assert message.channel.sent_messages == [
        "Voice note saved, but I can’t transcribe it: unsupported audio format (recording.xyz)."
    ]
    with session_factory() as session:
        capture = session.scalar(select(Capture).where(Capture.external_message_id == 24))
        assert capture is not None
        assert capture.processing_status == TRANSCRIPTION_UNSUPPORTED_STATUS
        assert capture.processing_error == "unsupported audio format (recording.xyz)"
        assert scheduled == []


def _add_voice_capture(
    session_factory: sessionmaker[Session],
    *,
    message_id: int,
    audio_path: Path,
) -> object:
    with session_factory() as session:
        capture = Capture(
            platform="discord",
            external_message_id=message_id,
            conversation_id=456,
            sender_id=123,
            source_type="voice",
            raw_text="[Voice note: voice-note.ogg]",
            source_metadata={
                "audio": {
                    "filename": "voice-note.ogg",
                    "content_type": "audio/ogg",
                    "url": "https://cdn.discordapp.com/voice-note.ogg",
                    "local_path": str(audio_path),
                }
            },
            processing_status=TRANSCRIPTION_PENDING_STATUS,
        )
        session.add(capture)
        session.commit()
        return capture.id


def test_voice_transcription_is_retained_and_searchable(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "voice-note.ogg"
    audio_path.write_bytes(b"fake audio")
    capture_id = _add_voice_capture(session_factory, message_id=26, audio_path=audio_path)
    provider = FakeProvider()
    settings = Settings(discord_allowed_user_id=123, storage_dir=tmp_path)

    assert asyncio.run(transcribe_capture(capture_id, settings, session_factory, provider)) is True
    with session_factory() as session:
        capture = session.get(Capture, capture_id)
        assert capture is not None
        assert capture.processing_status == "captured"
        assert capture.raw_text == provider.transcript
        assert capture.raw_transcription == provider.transcript
        assert capture.transcription_attempts == 1
        assert provider.transcribe_calls == [audio_path]

    assert asyncio.run(enrich_capture(capture_id, settings, session_factory, provider)) is True
    query = FakeMessage(36, "Did I save anything about the dentist?")
    asyncio.run(
        process_discord_message(
            query,
            settings,
            session_factory,
            provider=provider,
        )
    )
    assert provider.embed_calls[-1] == "Did I save anything about the dentist?"
    assert provider.transcript in query.channel.sent_messages[-1]


def test_failed_voice_transcription_can_retry_without_resending(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "voice-note.ogg"
    audio_path.write_bytes(b"fake audio")
    capture_id = _add_voice_capture(session_factory, message_id=37, audio_path=audio_path)
    settings = Settings(discord_allowed_user_id=123, storage_dir=tmp_path)
    failing_provider = FakeProvider()
    failing_provider.transcription_should_fail = True

    assert (
        asyncio.run(
            transcribe_capture(capture_id, settings, session_factory, failing_provider)
        )
        is False
    )
    with session_factory() as session:
        failed = session.get(Capture, capture_id)
        assert failed is not None
        assert failed.processing_status == TRANSCRIPTION_FAILED_STATUS
        assert failed.processing_error == "transcription provider unavailable"
        assert failed.transcription_attempts == 1

    scheduled: list[object] = []
    retry = FakeMessage(38, "/retry")
    asyncio.run(
        process_discord_message(
            retry,
            settings,
            session_factory,
            transcription_scheduler=scheduled.append,
        )
    )
    assert retry.channel.sent_messages == ["Retrying transcription for: voice-note.ogg"]
    assert scheduled == [capture_id]

    assert (
        asyncio.run(transcribe_capture(capture_id, settings, session_factory, FakeProvider()))
        is True
    )
    with session_factory() as session:
        retried = session.get(Capture, capture_id)
        assert retried is not None
        assert retried.processing_status == "captured"
        assert retried.transcription_attempts == 2


def test_empty_voice_file_reports_a_transcription_failure(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "empty.ogg"
    audio_path.touch()
    capture_id = _add_voice_capture(session_factory, message_id=39, audio_path=audio_path)
    settings = Settings(discord_allowed_user_id=123, storage_dir=tmp_path)

    assert (
        asyncio.run(transcribe_capture(capture_id, settings, session_factory, FakeProvider()))
        is False
    )
    with session_factory() as session:
        capture = session.get(Capture, capture_id)
        assert capture is not None
        assert capture.processing_status == TRANSCRIPTION_FAILED_STATUS
        assert capture.processing_error == "The voice attachment was empty"


def test_webpage_url_is_persisted_before_extraction_and_duplicate_provenance(
    session_factory: sessionmaker[Session],
) -> None:
    first = FakeMessage(42, "Read this later: https://Example.com/article/.")
    second = FakeMessage(43, "Same page again: https://example.com/article")
    scheduled: list[object] = []

    for message in (first, second):
        asyncio.run(
            process_discord_message(
                message,
                Settings(discord_allowed_user_id=123),
                session_factory,
                web_extraction_scheduler=scheduled.append,
            )
        )

    assert first.channel.sent_messages == [
        "Saved webpage; metadata extraction started in the background."
    ]
    assert second.channel.sent_messages == [
        "Saved webpage; this URL was already captured, so I kept this submission as a new "
        "provenance record. metadata extraction started in the background."
    ]
    with session_factory() as session:
        captures = list(
            session.scalars(select(Capture).where(Capture.source_type == "webpage"))
        )
        assert len(captures) == 2
        assert captures[0].raw_text == first.content
        assert captures[0].source_metadata["url"] == "https://Example.com/article/"
        assert captures[1].source_metadata["duplicate_of"] == str(captures[0].id)
        assert scheduled == [captures[0].id, captures[1].id]


def _add_webpage_capture(
    session_factory: sessionmaker[Session],
    *,
    message_id: int,
    text: str = "Save this article for later: https://example.com/article",
) -> object:
    with session_factory() as session:
        capture = Capture(
            platform="discord",
            external_message_id=message_id,
            conversation_id=456,
            sender_id=123,
            source_type="webpage",
            raw_text=text,
            source_metadata={
                "url": "https://example.com/article",
                "canonical_url": "https://example.com/article",
                "submitted_text": text,
            },
            processing_status=WEB_EXTRACTION_PENDING_STATUS,
        )
        session.add(capture)
        session.commit()
        return capture.id


def test_webpage_extraction_stores_bounded_metadata_and_is_searchable(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_id = _add_webpage_capture(session_factory, message_id=44)
    webpage = WebpageMetadata(
        url="https://example.com/article",
        domain="example.com",
        title="A practical machine learning article",
        author="Ada Example",
        published_date="2026-09-10",
        description="A short description.",
        headings=["Introduction", "Evaluation"],
        extraction_status="complete",
    )
    monkeypatch.setattr("app.workers.webpage.fetch_webpage_metadata", lambda url: webpage)
    settings = Settings(discord_allowed_user_id=123)

    assert asyncio.run(extract_webpage_capture(capture_id, settings, session_factory)) is True
    with session_factory() as session:
        capture = session.get(Capture, capture_id)
        assert capture is not None
        assert capture.processing_status == "captured"
        assert capture.extraction_attempts == 1
        assert capture.source_metadata["web"]["title"] == webpage.title
        assert "A practical machine learning article" in capture.raw_text
        assert "Introduction" in capture.raw_text
        assert "full HTML" not in capture.raw_text

    provider = FakeProvider()
    assert asyncio.run(enrich_capture(capture_id, settings, session_factory, provider)) is True
    query = FakeMessage(45, "Did I save anything about machine learning?")
    asyncio.run(
        process_discord_message(
            query,
            settings,
            session_factory,
            provider=provider,
        )
    )
    response = query.channel.sent_messages[-1]
    assert "A practical machine learning article" in response
    assert "https://example.com/article" in response
    assert "Type: webpage" in response


def test_webpage_extraction_failure_is_visible_and_retryable(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_id = _add_webpage_capture(session_factory, message_id=46)
    settings = Settings(discord_allowed_user_id=123)

    def fail(_: str) -> WebpageMetadata:
        raise RuntimeError("page blocked")

    monkeypatch.setattr("app.workers.webpage.fetch_webpage_metadata", fail)
    assert asyncio.run(extract_webpage_capture(capture_id, settings, session_factory)) is False
    with session_factory() as session:
        failed = session.get(Capture, capture_id)
        assert failed is not None
        assert failed.processing_status == WEB_EXTRACTION_FAILED_STATUS
        assert failed.processing_error == "page blocked"
        assert failed.extraction_attempts == 1

    retry = FakeMessage(47, "/retry")
    scheduled: list[object] = []
    asyncio.run(
        process_discord_message(
            retry,
            settings,
            session_factory,
            web_extraction_scheduler=scheduled.append,
        )
    )
    assert retry.channel.sent_messages == [
        "Retrying webpage extraction for: https://example.com/article"
    ]
    assert scheduled == [capture_id]

    webpage = WebpageMetadata(
        url="https://example.com/article",
        domain="example.com",
        title="Recovered article",
        extraction_status="metadata_only",
    )
    monkeypatch.setattr("app.workers.webpage.fetch_webpage_metadata", lambda url: webpage)
    assert asyncio.run(extract_webpage_capture(capture_id, settings, session_factory)) is True
    with session_factory() as session:
        recovered = session.get(Capture, capture_id)
        assert recovered is not None
        assert recovered.processing_status == "captured"
        assert recovered.processing_error is None
        assert recovered.extraction_attempts == 2


def test_natural_query_searches_without_saving_the_question(
    session_factory: sessionmaker[Session],
) -> None:
    saved = FakeMessage(25, "A friend recommended a ramen restaurant near F7")
    run_message(saved, session_factory)
    with session_factory() as session:
        capture = session.scalar(select(Capture).where(Capture.external_message_id == 25))
        assert capture is not None
        capture.normalized_text = "friend recommended Japanese noodles"
        capture.summary = "Recommendation for a ramen restaurant near F7."
        capture.embedding = [1.0, 0.0]
        session.commit()

    natural_query = FakeMessage(26, "What restaurant did my friend recommend?")
    asyncio.run(
        process_discord_message(
            natural_query,
            Settings(discord_allowed_user_id=123),
            session_factory,
            provider=FakeProvider(),
        )
    )

    response = natural_query.channel.sent_messages[-1]
    assert response.startswith("Search only — this message was not saved.")
    assert "A friend recommended a ramen restaurant near F7" in response
    with session_factory() as session:
        assert len(list(session.scalars(select(Capture)))) == 1
        assert len(list(session.scalars(select(MessageReceipt)))) == 2


def test_idea_question_is_saved_and_save_command_provides_recovery(
    session_factory: sessionmaker[Session],
) -> None:
    idea = FakeMessage(27, "What if I built an AI inbox?")
    run_message(idea, session_factory)
    assert idea.channel.sent_messages == ["Saved: What if I built an AI inbox?"]

    question = FakeMessage(28, "What restaurant did my friend recommend?")
    run_message(question, session_factory)
    assert question.channel.sent_messages[-1].startswith(
        "Search only — this message was not saved."
    )

    recovered = FakeMessage(29, "/save What restaurant did my friend recommend?")
    run_message(recovered, session_factory)
    assert recovered.channel.sent_messages == [
        "Saved: What restaurant did my friend recommend?"
    ]
    with session_factory() as session:
        raw_texts = list(session.scalars(select(Capture.raw_text).order_by(Capture.created_at)))
        assert raw_texts == [
            "What if I built an AI inbox?",
            "What restaurant did my friend recommend?",
        ]


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


def test_short_natural_queries_reject_weak_semantic_false_positives(
    session_factory: sessionmaker[Session],
) -> None:
    football = FakeMessage(63, "I need to check prices of footballs around my area.")
    electrician = FakeMessage(64, "Call the electrician about the kitchen lights")
    run_message(football, session_factory)
    run_message(electrician, session_factory)
    with session_factory() as session:
        football_capture = session.scalar(
            select(Capture).where(Capture.external_message_id == 63)
        )
        electrician_capture = session.scalar(
            select(Capture).where(Capture.external_message_id == 64)
        )
        assert football_capture is not None and electrician_capture is not None
        football_capture.normalized_text = "check local football prices"
        football_capture.summary = "Need to compare local football prices."
        football_capture.topics = ["shopping", "footballs", "prices", "local"]
        football_capture.embedding = [1.0, 0.0]
        electrician_capture.normalized_text = "call electrician kitchen lights"
        electrician_capture.summary = "Contact the electrician about the kitchen lights."
        electrician_capture.embedding = [0.4, 0.916515]
        session.commit()

    provider = FakeProvider()
    for message_id, query in (
        (67, "Did I save anything about prices?"),
        (68, "Did I save anything about football?"),
    ):
        query_message = FakeMessage(message_id, query)
        asyncio.run(
            process_discord_message(
                query_message,
                Settings(discord_allowed_user_id=123),
                session_factory,
                provider=provider,
            )
        )
        response = query_message.channel.sent_messages[-1]
        assert "I need to check prices of footballs around my area." in response
        assert "Call the electrician about the kitchen lights" not in response


def test_ask_excludes_generic_saved_questions_from_semantic_results(
    session_factory: sessionmaker[Session],
) -> None:
    recommendation = FakeMessage(65, "A friend recommended a ramen restaurant near F7")
    generic_question = FakeMessage(66, "/save What have I saved?")
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


def test_openai_provider_passes_explicit_economical_models(tmp_path: Path) -> None:
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

    class FakeTranscriptions:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def create(self, **kwargs: object) -> SimpleNamespace:
            self.kwargs = kwargs
            return SimpleNamespace(text="A transcribed note")

    responses = FakeResponses()
    embeddings = FakeEmbeddings()
    transcriptions = FakeTranscriptions()
    provider = OpenAIProvider(
        Settings(openai_api_key="test-key"),
        client=SimpleNamespace(
            responses=responses,
            embeddings=embeddings,
            audio=SimpleNamespace(transcriptions=transcriptions),
        ),
    )
    provider.enrich("short note")
    provider.embed("short note")
    audio_path = tmp_path / "note.ogg"
    audio_path.write_bytes(b"fake audio")
    assert provider.transcribe(audio_path) == "A transcribed note"

    assert responses.kwargs["model"] == "gpt-5.6-luna"
    assert embeddings.kwargs["model"] == "text-embedding-3-small"
    assert embeddings.kwargs["dimensions"] == 1536
    assert transcriptions.kwargs["model"] == "gpt-4o-mini-transcribe"
    assert transcriptions.kwargs["response_format"] == "text"
