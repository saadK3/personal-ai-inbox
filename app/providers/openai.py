"""OpenAI-backed enrichment and embedding provider.

The provider is deliberately small so the ingestion and retrieval layers do not
depend on the OpenAI SDK's response objects.  Calls are synchronous; callers
run them in a worker thread so Discord's event loop remains responsive.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from openai import OpenAI

from app.core.config import Settings


class ProviderError(RuntimeError):
    """Raised when an external provider response cannot be used safely."""


@dataclass(frozen=True)
class EnrichmentResult:
    """Structured, retrieval-oriented fields derived from a text capture."""

    normalized_text: str
    summary: str
    inferred_type: str
    topics: list[str]
    entities: dict[str, list[str]]


class EnrichmentProvider(Protocol):
    """Interface used by the worker and Discord adapter."""

    def enrich(self, raw_text: str) -> EnrichmentResult:
        """Extract searchable text and metadata from a capture."""

    def embed(self, text: str) -> list[float]:
        """Create a vector for searchable text."""


ENRICHMENT_INSTRUCTIONS = """You enrich one private personal inbox capture for later search.
Return only JSON matching the supplied schema. Preserve the user's meaning and
do not invent facts. normalized_text should be a concise cleaned version of the
input (not a rewrite); summary should be one short sentence; inferred_type is a
short flexible label such as reminder, idea, recommendation, reference, person,
place, or note. topics should contain a few useful lowercase keywords. entities
should be an object with people, places, organizations, dates, and products
arrays containing exact names or phrases from the input. Use empty arrays when a
category is absent.
"""

ENRICHMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "normalized_text": {"type": "string"},
        "summary": {"type": "string"},
        "inferred_type": {"type": "string"},
        "topics": {"type": "array", "items": {"type": "string"}},
        "entities": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "people": {"type": "array", "items": {"type": "string"}},
                "places": {"type": "array", "items": {"type": "string"}},
                "organizations": {"type": "array", "items": {"type": "string"}},
                "dates": {"type": "array", "items": {"type": "string"}},
                "products": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["people", "places", "organizations", "dates", "products"],
        },
    },
    "required": [
        "normalized_text",
        "summary",
        "inferred_type",
        "topics",
        "entities",
    ],
}


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(f"Enrichment field {field!r} must be a non-empty string")
    return " ".join(value.split())


def _string_list(value: Any, field: str, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        raise ProviderError(f"Enrichment field {field!r} must be an array")
    result: list[str] = []
    for item in value[:limit]:
        if isinstance(item, str) and item.strip():
            normalized = " ".join(item.split())
            if normalized not in result:
                result.append(normalized)
    return result


def _parse_enrichment(payload: Any) -> EnrichmentResult:
    if not isinstance(payload, dict):
        raise ProviderError("Enrichment response must be a JSON object")
    entities_payload = payload.get("entities", {})
    if not isinstance(entities_payload, dict):
        raise ProviderError("Enrichment field 'entities' must be an object")
    entities: dict[str, list[str]] = {}
    for key, value in entities_payload.items():
        if isinstance(key, str) and key.strip():
            entities[key.strip().casefold()] = _string_list(value, f"entities.{key}")
    return EnrichmentResult(
        normalized_text=_string(payload.get("normalized_text"), "normalized_text"),
        summary=_string(payload.get("summary"), "summary"),
        inferred_type=_string(payload.get("inferred_type"), "inferred_type"),
        topics=_string_list(payload.get("topics"), "topics"),
        entities=entities,
    )


class OpenAIProvider:
    """OpenAI implementation using the explicitly configured economical models."""

    def __init__(self, settings: Settings, client: OpenAI | None = None) -> None:
        if settings.openai_api_key is None or not settings.openai_api_key.get_secret_value():
            raise ProviderError("OPENAI_API_KEY is not configured")
        self.settings = settings
        self.client = client or OpenAI(api_key=settings.openai_api_key.get_secret_value())

    def enrich(self, raw_text: str) -> EnrichmentResult:
        if not raw_text.strip():
            raise ProviderError("Cannot enrich empty text")
        response = self.client.responses.create(
            model=self.settings.openai_model,
            instructions=ENRICHMENT_INSTRUCTIONS,
            input=raw_text,
            max_output_tokens=400,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "capture_enrichment",
                    "strict": True,
                    "schema": ENRICHMENT_SCHEMA,
                }
            },
        )
        output_text = getattr(response, "output_text", None)
        if not isinstance(output_text, str) or not output_text.strip():
            raise ProviderError("OpenAI returned no enrichment output")
        try:
            payload = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise ProviderError("OpenAI returned invalid enrichment JSON") from exc
        return _parse_enrichment(payload)

    def embed(self, text: str) -> list[float]:
        if not text.strip():
            raise ProviderError("Cannot embed empty text")
        response = self.client.embeddings.create(
            model=self.settings.openai_embedding_model,
            input=text,
            dimensions=self.settings.openai_embedding_dimensions,
        )
        if not response.data:
            raise ProviderError("OpenAI returned no embedding")
        embedding = response.data[0].embedding
        if len(embedding) != self.settings.openai_embedding_dimensions:
            raise ProviderError(
                "OpenAI returned an unexpected embedding dimension "
                f"({len(embedding)} instead of {self.settings.openai_embedding_dimensions})"
            )
        return [float(value) for value in embedding]
