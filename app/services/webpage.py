"""Bounded webpage metadata extraction for URL captures."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

MAX_WEBPAGE_BYTES = 1_000_000
MAX_DESCRIPTION_LENGTH = 1_000
MAX_HEADING_LENGTH = 200
MAX_HEADINGS = 10
MAX_SCRIPT_METADATA_BYTES = 100_000
URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def extract_url(text: str) -> str | None:
    """Return the first valid HTTP(S) URL while removing sentence punctuation."""

    for candidate in URL_PATTERN.findall(text):
        cleaned = candidate.rstrip(".,!?;:)]}")
        parts = urlsplit(cleaned)
        if parts.scheme.casefold() in {"http", "https"} and parts.netloc:
            return cleaned
    return None


def canonicalize_url(url: str) -> str:
    """Normalize only URL identity fields used for duplicate detection."""

    parts = urlsplit(url)
    scheme = parts.scheme.casefold()
    netloc = parts.netloc.casefold()
    if (scheme == "http" and netloc.endswith(":80")) or (
        scheme == "https" and netloc.endswith(":443")
    ):
        netloc = netloc.rsplit(":", 1)[0]
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _clean(value: str | None, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(unescape(value).split())
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1].rstrip()}…"


class _MetadataParser(HTMLParser):
    """Extract selected tags without retaining the page body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.headings: list[str] = []
        self.meta: dict[str, str] = {}
        self.json_ld: list[object] = []
        self._in_title = False
        self._heading_parts: list[str] | None = None
        self._in_json_ld = False
        self._json_ld_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value for key, value in attrs}
        if tag.casefold() == "title":
            self._in_title = True
        elif tag.casefold() in {"h1", "h2", "h3"} and len(self.headings) < MAX_HEADINGS:
            self._heading_parts = []
        elif tag.casefold() == "meta":
            key = attributes.get("property") or attributes.get("name") or attributes.get(
                "itemprop"
            )
            content = attributes.get("content")
            if key and content:
                cleaned_key = key.casefold().strip()
                cleaned_content = _clean(content, MAX_DESCRIPTION_LENGTH)
                if cleaned_content and cleaned_key not in self.meta:
                    self.meta[cleaned_key] = cleaned_content
        elif (
            tag.casefold() == "script"
            and (attributes.get("type") or "").casefold() == "application/ld+json"
        ):
            self._in_json_ld = True
            self._json_ld_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_title and len("".join(self.title_parts)) < MAX_DESCRIPTION_LENGTH:
            self.title_parts.append(data)
        if self._heading_parts is not None:
            current_length = len("".join(self._heading_parts))
            if current_length < MAX_HEADING_LENGTH:
                self._heading_parts.append(data)
        if self._in_json_ld and len("".join(self._json_ld_parts)) < MAX_SCRIPT_METADATA_BYTES:
            self._json_ld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag == "title":
            self._in_title = False
        elif normalized_tag in {"h1", "h2", "h3"} and self._heading_parts is not None:
            heading = _clean(" ".join(self._heading_parts), MAX_HEADING_LENGTH)
            if heading and heading not in self.headings and len(self.headings) < MAX_HEADINGS:
                self.headings.append(heading)
            self._heading_parts = None
        elif normalized_tag == "script" and self._in_json_ld:
            payload = "".join(self._json_ld_parts).strip()
            if payload:
                try:
                    self.json_ld.append(json.loads(payload))
                except json.JSONDecodeError:
                    pass
            self._in_json_ld = False
            self._json_ld_parts = []


def _json_objects(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        objects = [value]
        for nested in value.values():
            objects.extend(_json_objects(nested))
        return objects
    if isinstance(value, list):
        objects: list[dict[str, object]] = []
        for item in value:
            objects.extend(_json_objects(item))
        return objects
    return []


def _json_value(objects: list[dict[str, object]], *keys: str) -> str | None:
    wanted = {key.casefold() for key in keys}
    for obj in objects:
        for key, value in obj.items():
            if key.casefold() not in wanted:
                continue
            if isinstance(value, str):
                return value
            if isinstance(value, dict) and isinstance(value.get("name"), str):
                return value["name"]
    return None


@dataclass(frozen=True)
class WebpageMetadata:
    """Small, bounded metadata record retained for a webpage capture."""

    url: str
    domain: str
    title: str | None = None
    author: str | None = None
    published_date: str | None = None
    description: str | None = None
    headings: list[str] = field(default_factory=list)
    extraction_status: str = "metadata_only"
    resolved_url: str | None = None
    truncated: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "url": self.url,
            "domain": self.domain,
            "title": self.title,
            "author": self.author,
            "published_date": self.published_date,
            "description": self.description,
            "headings": self.headings,
            "extraction_status": self.extraction_status,
            "truncated": self.truncated,
        }
        if self.resolved_url and self.resolved_url != self.url:
            result["resolved_url"] = self.resolved_url
        if self.error:
            result["error"] = self.error
        return result


def extract_metadata_from_html(
    url: str,
    html: str,
    *,
    resolved_url: str | None = None,
    truncated: bool = False,
) -> WebpageMetadata:
    """Extract only bounded metadata and headings from HTML text."""

    parser = _MetadataParser()
    parser.feed(html)
    parser.close()
    json_objects = [obj for payload in parser.json_ld for obj in _json_objects(payload)]
    meta = parser.meta
    title = (
        _clean(meta.get("og:title"), MAX_DESCRIPTION_LENGTH)
        or _clean(meta.get("twitter:title"), MAX_DESCRIPTION_LENGTH)
        or _clean(" ".join(parser.title_parts), MAX_DESCRIPTION_LENGTH)
        or _clean(_json_value(json_objects, "headline", "name"), MAX_DESCRIPTION_LENGTH)
    )
    author = _clean(
        meta.get("author")
        or meta.get("article:author")
        or meta.get("byl")
        or meta.get("dc.creator")
        or _json_value(json_objects, "author", "creator"),
        MAX_DESCRIPTION_LENGTH,
    )
    published_date = _clean(
        meta.get("article:published_time")
        or meta.get("datepublished")
        or meta.get("date")
        or meta.get("pubdate")
        or meta.get("dc.date")
        or _json_value(json_objects, "datePublished", "dateCreated"),
        120,
    )
    description = _clean(
        meta.get("description")
        or meta.get("og:description")
        or meta.get("twitter:description")
        or _json_value(json_objects, "description"),
        MAX_DESCRIPTION_LENGTH,
    )
    return WebpageMetadata(
        url=url,
        domain=urlsplit(url).netloc.casefold(),
        title=title,
        author=author,
        published_date=published_date,
        description=description,
        headings=parser.headings,
        extraction_status="complete"
        if any((title, author, published_date, description, parser.headings))
        else "metadata_only",
        resolved_url=resolved_url,
        truncated=truncated,
    )


def fetch_webpage_metadata(url: str) -> WebpageMetadata:
    """Fetch a bounded HTML prefix and extract metadata without storing the body."""

    request = Request(url, headers={"User-Agent": "personal-ai-inbox/1.0"})
    with urlopen(request, timeout=30) as response:  # noqa: S310  # URL comes from user input.
        content_type = (response.headers.get_content_type() or "").casefold()
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read(MAX_WEBPAGE_BYTES + 1)
        resolved_url = response.geturl()
    truncated = len(payload) > MAX_WEBPAGE_BYTES
    payload = payload[:MAX_WEBPAGE_BYTES]
    if content_type not in {"", "text/html", "application/xhtml+xml"}:
        return WebpageMetadata(
            url=url,
            domain=urlsplit(url).netloc.casefold(),
            extraction_status="metadata_only",
            resolved_url=resolved_url,
            error=f"unsupported content type: {content_type or 'unknown'}",
        )
    try:
        html = payload.decode(charset, errors="replace")
    except LookupError:
        html = payload.decode("utf-8", errors="replace")
    return extract_metadata_from_html(
        url,
        html,
        resolved_url=resolved_url,
        truncated=truncated,
    )
