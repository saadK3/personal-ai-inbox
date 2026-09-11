"""Bounded GitHub repository identity and metadata extraction."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from app.services.webpage import extract_url

GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
MAX_RESPONSE_BYTES = 256 * 1024
MAX_OWNER_LENGTH = 200
MAX_REPOSITORY_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 1_000
MAX_TOPIC_LENGTH = 100
MAX_TOPICS = 20
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def _clean(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1].rstrip()}…"


@dataclass(frozen=True)
class GitHubRepoIdentity:
    """Canonical owner/repository identity parsed from a GitHub URL."""

    url: str
    canonical_url: str
    owner: str
    repository: str


def parse_github_url(url: str) -> GitHubRepoIdentity | None:
    """Parse a GitHub repository URL, rejecting non-repository paths."""

    try:
        parts = urlsplit(url)
        hostname = (parts.hostname or "").casefold()
    except ValueError:
        return None
    if hostname not in GITHUB_HOSTS:
        return None
    path_parts = [part for part in parts.path.split("/") if part]
    if len(path_parts) != 2:
        return None
    owner, repository = path_parts
    if repository.casefold().endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository:
        return None
    if not _NAME_PATTERN.fullmatch(owner) or not _NAME_PATTERN.fullmatch(repository):
        return None
    canonical_url = f"https://github.com/{owner.casefold()}/{repository.casefold()}"
    return GitHubRepoIdentity(
        url=url,
        canonical_url=canonical_url,
        owner=owner,
        repository=repository,
    )


def extract_github_url(text: str) -> str | None:
    """Return a GitHub URL, including malformed repository paths for safe capture."""

    url = extract_url(text)
    if url is None:
        return None
    try:
        hostname = (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return None
    return url if hostname in GITHUB_HOSTS else None


def canonicalize_github_url(url: str) -> str:
    """Normalize owner/repository identity for duplicate detection."""

    parsed = parse_github_url(url)
    if parsed is not None:
        return parsed.canonical_url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), "", "")
    )


@dataclass(frozen=True)
class GitHubMetadata:
    """Small metadata record intentionally limited to repo identity, description and topics."""

    url: str
    canonical_url: str
    owner: str
    repository: str
    description: str | None = None
    topics: list[str] = field(default_factory=list)
    extraction_status: str = "metadata_only"
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "url": self.url,
            "canonical_url": self.canonical_url,
            "owner": self.owner,
            "repository": self.repository,
            "description": self.description,
            "topics": self.topics,
            "extraction_status": self.extraction_status,
        }
        if self.error:
            result["error"] = self.error
        return result


def _fetch_repository_api(identity: GitHubRepoIdentity) -> dict[str, object]:
    endpoint = f"https://api.github.com/repos/{identity.owner}/{identity.repository}"
    request = Request(
        endpoint,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "personal-ai-inbox/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310  # endpoint is fixed GitHub API.
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise RuntimeError("GitHub metadata response exceeded the size limit")
    decoded = json.loads(payload.decode("utf-8", errors="replace"))
    if not isinstance(decoded, dict):
        raise RuntimeError("GitHub metadata response was not an object")
    return decoded


def _topics(payload: dict[str, object]) -> list[str]:
    raw_topics = payload.get("topics")
    if not isinstance(raw_topics, list):
        return []
    result: list[str] = []
    for topic in raw_topics[:MAX_TOPICS]:
        cleaned = _clean(topic, MAX_TOPIC_LENGTH)
        if cleaned and cleaned.casefold() not in {item.casefold() for item in result}:
            result.append(cleaned)
    return result


def fetch_github_metadata(url: str) -> GitHubMetadata:
    """Fetch only the repository endpoint; never README, source, history or issue data."""

    identity = parse_github_url(url)
    if identity is None:
        raise ValueError("Malformed GitHub repository URL")
    try:
        payload = _fetch_repository_api(identity)
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        raise RuntimeError(f"GitHub metadata unavailable: {message[:900]}") from exc

    owner_payload = payload.get("owner")
    owner = (
        _clean(owner_payload.get("login"), MAX_OWNER_LENGTH)
        if isinstance(owner_payload, dict)
        else None
    ) or identity.owner
    repository = _clean(payload.get("name"), MAX_REPOSITORY_LENGTH) or identity.repository
    description = _clean(payload.get("description"), MAX_DESCRIPTION_LENGTH)
    topics = _topics(payload)
    status = "complete" if description or topics else "metadata_only"
    return GitHubMetadata(
        url=url,
        canonical_url=identity.canonical_url,
        owner=owner,
        repository=repository,
        description=description,
        topics=topics,
        extraction_status=status,
    )
