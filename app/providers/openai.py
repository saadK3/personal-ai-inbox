"""OpenAI-backed enrichment and embedding provider.

The provider is deliberately small so the ingestion and retrieval layers do not
depend on the OpenAI SDK's response objects.  Calls are synchronous; callers
run them in a worker thread so Discord's event loop remains responsive.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from mimetypes import guess_type
from pathlib import Path
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


@dataclass(frozen=True)
class VisionResult:
    """Factual, retrieval-oriented output from one image."""

    description: str
    ocr_text: str | None = None
    uncertainty: str | None = None


class EnrichmentProvider(Protocol):
    """Interface used by the worker and Discord adapter."""

    def enrich(self, raw_text: str) -> EnrichmentResult:
        """Extract searchable text and metadata from a capture."""

    def embed(self, text: str) -> list[float]:
        """Create a vector for searchable text."""

    def transcribe(self, audio_path: Path) -> str:
        """Transcribe a durable audio file."""


class VisionProvider(Protocol):
    """Interface for factual image description and OCR."""

    def describe_image(
        self,
        image_path: Path,
        mime_type: str,
        user_context: str,
    ) -> VisionResult:
        """Describe visible content and transcribe legible text without guessing."""


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

VISION_INSTRUCTIONS = """Analyze one personal image for later retrieval. Return only JSON matching
the supplied schema. description must be one concise, factual sentence about
what is visibly present. ocr_text should contain only legible visible text,
or an empty string when there is none. uncertainty should briefly explain any
material ambiguity, blur, occlusion, or low quality, or be an empty string.
Do not identify people, infer private or sensitive attributes, guess intent,
or describe anything that is not visibly supported by the image.
"""

VISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "description": {"type": "string"},
        "ocr_text": {"type": "string"},
        "uncertainty": {"type": "string"},
    },
    "required": ["description", "ocr_text", "uncertainty"],
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


def _optional_string(value: Any, field: str, limit: int = 2_000) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProviderError(f"Vision field {field!r} must be a string")
    normalized = " ".join(value.split())
    if not normalized:
        return None
    return normalized[:limit]


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

    def transcribe(self, audio_path: Path) -> str:
        """Transcribe one supported audio file using the configured model."""

        if not audio_path.is_file() or audio_path.stat().st_size == 0:
            raise ProviderError("Cannot transcribe a missing or empty audio file")
        try:
            with audio_path.open("rb") as audio_file:
                response = self.client.audio.transcriptions.create(
                    model=self.settings.openai_transcription_model,
                    file=audio_file,
                    response_format="text",
                )
        except Exception as exc:
            raise ProviderError(f"Audio transcription failed: {exc}") from exc
        text = getattr(response, "text", response if isinstance(response, str) else None)
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("OpenAI returned an empty transcription")
        return " ".join(text.split())

    def describe_image(
        self,
        image_path: Path,
        mime_type: str,
        user_context: str,
    ) -> VisionResult:
        """Describe a local image using a bounded Responses API vision request."""

        if not image_path.is_file() or image_path.stat().st_size == 0:
            raise ProviderError("Cannot analyze a missing or empty image file")
        normalized_mime = mime_type.split(";", 1)[0].casefold()
        if not normalized_mime.startswith("image/"):
            normalized_mime = guess_type(image_path.name)[0] or "image/jpeg"
        image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
        context = "User context: " + (" ".join(user_context.split()) or "(none)")
        response = self.client.responses.create(
            model=self.settings.openai_vision_model,
            instructions=VISION_INSTRUCTIONS,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": context},
                        {
                            "type": "input_image",
                            "image_url": f"data:{normalized_mime};base64,{image_data}",
                            "detail": "low",
                        },
                    ],
                }
            ],
            max_output_tokens=300,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "image_description",
                    "strict": True,
                    "schema": VISION_SCHEMA,
                }
            },
        )
        output_text = getattr(response, "output_text", None)
        if not isinstance(output_text, str) or not output_text.strip():
            raise ProviderError("OpenAI returned no image analysis output")
        try:
            payload = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise ProviderError("OpenAI returned invalid image analysis JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderError("OpenAI image analysis response must be an object")
        description = _string(payload.get("description"), "description")
        return VisionResult(
            description=description,
            ocr_text=_optional_string(payload.get("ocr_text"), "ocr_text"),
            uncertainty=_optional_string(payload.get("uncertainty"), "uncertainty"),
        )
