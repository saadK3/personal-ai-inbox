"""Bounded YouTube URL recognition and metadata extraction."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from app.services.webpage import (
    WebpageMetadata,
    canonicalize_url,
    extract_url,
    fetch_webpage_metadata,
)

YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
MAX_OEMBED_BYTES = 64 * 1024
MAX_TITLE_LENGTH = 300
MAX_CHANNEL_LENGTH = 300
MAX_DESCRIPTION_LENGTH = 1_000
MAX_DATE_LENGTH = 120


def _clean(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(unescape(value).split())
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1].rstrip()}…"


@dataclass(frozen=True)
class YouTubeUrl:
    """Parsed identity for a YouTube URL."""

    url: str
    video_id: str | None

    @property
    def canonical_url(self) -> str:
        if self.video_id:
            return f"https://www.youtube.com/watch?v={self.video_id}"
        return canonicalize_url(self.url)


def parse_youtube_url(url: str) -> YouTubeUrl | None:
    """Parse supported YouTube hosts and video URL forms."""

    try:
        parts = urlsplit(url)
        hostname = (parts.hostname or "").casefold()
    except ValueError:
        return None
    if hostname not in YOUTUBE_HOSTS:
        return None

    path_parts = [part for part in parts.path.split("/") if part]
    video_id: str | None = None
    if hostname == "youtu.be":
        video_id = path_parts[0] if path_parts else None
    elif parts.path.casefold() == "/watch":
        video_id = parse_qs(parts.query).get("v", [None])[0]
    elif path_parts and path_parts[0].casefold() in {"embed", "live", "shorts"}:
        video_id = path_parts[1] if len(path_parts) > 1 else None
    if video_id is not None and not VIDEO_ID_PATTERN.fullmatch(video_id):
        video_id = None
    return YouTubeUrl(url=url, video_id=video_id)


def extract_youtube_url(text: str) -> str | None:
    """Return the first YouTube URL, including malformed video paths for safe handling."""

    url = extract_url(text)
    if url is None or parse_youtube_url(url) is None:
        return None
    return url


def canonicalize_youtube_url(url: str) -> str:
    """Use video identity so watch and youtu.be forms de-duplicate."""

    parsed = parse_youtube_url(url)
    return parsed.canonical_url if parsed is not None else canonicalize_url(url)


@dataclass(frozen=True)
class YouTubeMetadata:
    """Small metadata record retained for a YouTube capture."""

    url: str
    canonical_url: str
    video_id: str
    title: str | None = None
    channel: str | None = None
    published_date: str | None = None
    description: str | None = None
    extraction_status: str = "metadata_only"
    resolved_url: str | None = None
    truncated: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "url": self.url,
            "canonical_url": self.canonical_url,
            "video_id": self.video_id,
            "title": self.title,
            "channel": self.channel,
            "published_date": self.published_date,
            "description": self.description,
            "extraction_status": self.extraction_status,
            "truncated": self.truncated,
        }
        if self.resolved_url and self.resolved_url != self.url:
            result["resolved_url"] = self.resolved_url
        if self.error:
            result["error"] = self.error
        return result


def _fetch_oembed(canonical_url: str) -> dict[str, object]:
    endpoint = "https://www.youtube.com/oembed?" + urlencode(
        {"url": canonical_url, "format": "json"}
    )
    request = Request(endpoint, headers={"User-Agent": "personal-ai-inbox/1.0"})
    with urlopen(request, timeout=30) as response:  # noqa: S310  # URL is derived from a video ID.
        payload = response.read(MAX_OEMBED_BYTES + 1)
    if len(payload) > MAX_OEMBED_BYTES:
        raise RuntimeError("YouTube metadata response exceeded the size limit")
    decoded = json.loads(payload.decode("utf-8", errors="replace"))
    if not isinstance(decoded, dict):
        raise RuntimeError("YouTube metadata response was not an object")
    return decoded


def _error_text(errors: list[tuple[str, Exception]]) -> str | None:
    if not errors:
        return None
    messages = [
        f"{label}: {str(error).strip() or error.__class__.__name__}"
        for label, error in errors
    ]
    return "; ".join(messages)[:1_000]


def fetch_youtube_metadata(url: str) -> YouTubeMetadata:
    """Fetch only oEmbed and bounded page metadata, never video bytes or transcripts."""

    parsed = parse_youtube_url(url)
    if parsed is None or parsed.video_id is None:
        raise ValueError("Malformed YouTube URL")

    errors: list[tuple[str, Exception]] = []
    oembed: dict[str, object] | None = None
    page: WebpageMetadata | None = None
    try:
        oembed = _fetch_oembed(parsed.canonical_url)
    except Exception as exc:
        errors.append(("oEmbed", exc))
    try:
        page = fetch_webpage_metadata(parsed.canonical_url)
        if page.error:
            errors.append(("page metadata", RuntimeError(page.error)))
    except Exception as exc:
        errors.append(("page metadata", exc))
    if oembed is None and page is None:
        raise RuntimeError(_error_text(errors) or "YouTube metadata unavailable")

    page_title = _clean(page.title, MAX_TITLE_LENGTH) if page is not None else None
    page_channel = _clean(page.author, MAX_CHANNEL_LENGTH) if page is not None else None
    page_date = _clean(page.published_date, MAX_DATE_LENGTH) if page is not None else None
    page_description = (
        _clean(page.description, MAX_DESCRIPTION_LENGTH) if page is not None else None
    )
    title = _clean((oembed or {}).get("title"), MAX_TITLE_LENGTH) or page_title
    channel = _clean((oembed or {}).get("author_name"), MAX_CHANNEL_LENGTH) or page_channel
    published_date = page_date
    description = page_description
    truncated = page.truncated if page is not None else False
    resolved_url = page.resolved_url if page is not None else None
    status = "complete" if any((title, channel, published_date, description)) else "metadata_only"
    return YouTubeMetadata(
        url=url,
        canonical_url=parsed.canonical_url,
        video_id=parsed.video_id,
        title=title,
        channel=channel,
        published_date=published_date,
        description=description,
        extraction_status=status,
        resolved_url=resolved_url,
        truncated=truncated,
        error=_error_text(errors),
    )
