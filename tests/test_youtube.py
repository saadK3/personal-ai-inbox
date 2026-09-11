import pytest

from app.services.webpage import WebpageMetadata
from app.services.youtube import (
    YouTubeMetadata,
    fetch_youtube_metadata,
)


def test_fetch_youtube_metadata_combines_bounded_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = WebpageMetadata(
        url="https://www.youtube.com/watch?v=abc123XYZ89",
        domain="www.youtube.com",
        title="Page title",
        published_date="2026-09-10",
        description="A bounded description.",
        extraction_status="complete",
    )
    monkeypatch.setattr(
        "app.services.youtube._fetch_oembed",
        lambda url: {"title": "Video title", "author_name": "Example Channel"},
    )
    monkeypatch.setattr("app.services.youtube.fetch_webpage_metadata", lambda url: page)

    metadata = fetch_youtube_metadata("https://youtu.be/abc123XYZ89?t=42")

    assert metadata == YouTubeMetadata(
        url="https://youtu.be/abc123XYZ89?t=42",
        canonical_url="https://www.youtube.com/watch?v=abc123XYZ89",
        video_id="abc123XYZ89",
        title="Video title",
        channel="Example Channel",
        published_date="2026-09-10",
        description="A bounded description.",
        extraction_status="complete",
    )


def test_fetch_youtube_metadata_uses_page_fallback_when_oembed_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = WebpageMetadata(
        url="https://www.youtube.com/watch?v=abc123XYZ89",
        domain="www.youtube.com",
        title="Fallback title",
        author="Fallback channel",
        extraction_status="complete",
    )

    def fail(_: str) -> dict[str, object]:
        raise RuntimeError("oEmbed blocked")

    monkeypatch.setattr("app.services.youtube._fetch_oembed", fail)
    monkeypatch.setattr("app.services.youtube.fetch_webpage_metadata", lambda url: page)

    metadata = fetch_youtube_metadata("https://www.youtube.com/watch?v=abc123XYZ89")

    assert metadata.title == "Fallback title"
    assert metadata.channel == "Fallback channel"
    assert metadata.extraction_status == "complete"
    assert metadata.error == "oEmbed: oEmbed blocked"


def test_fetch_youtube_metadata_rejects_malformed_video_urls() -> None:
    with pytest.raises(ValueError, match="Malformed YouTube URL"):
        fetch_youtube_metadata("https://www.youtube.com/watch")
