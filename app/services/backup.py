"""Portable inbox exports and idempotent recovery helpers."""

from __future__ import annotations

import json
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models.capture import Capture, CaptureCorrection, CaptureRelation

EXPORT_FORMAT_VERSION = 1
EXPORT_FILENAME_PREFIX = "personal-ai-inbox-export"
MEDIA_FIELDS = ("audio", "image")


@dataclass(frozen=True)
class RestoreResult:
    """Counts from one idempotent archive restore."""

    captures_created: int
    captures_updated: int
    corrections_restored: int
    relationships_restored: int
    media_restored: int


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, uuid.UUID, Path)):
        return str(value)
    return str(value)


def _model_payload(instance: Any, model: Any) -> dict[str, Any]:
    return {
        column.name: getattr(instance, column.name)
        for column in model.__table__.columns
    }


def _media_entries(captures: list[Capture], storage_dir: Path) -> list[dict[str, Any]]:
    root = storage_dir.expanduser().resolve()
    entries: list[dict[str, Any]] = []
    seen_archive_paths: set[str] = set()
    for capture in captures:
        metadata = capture.source_metadata or {}
        for field in MEDIA_FIELDS:
            value = metadata.get(field)
            if not isinstance(value, dict):
                continue
            local_path = value.get("local_path")
            if not isinstance(local_path, str) or not local_path.strip():
                continue
            path = Path(local_path).expanduser()
            try:
                resolved = path.resolve()
                relative = resolved.relative_to(root)
            except (OSError, ValueError):
                entries.append(
                    {
                        "capture_id": capture.id,
                        "field": field,
                        "original_path": local_path,
                        "backed_up": False,
                        "reason": "file is outside STORAGE_DIR",
                    }
                )
                continue
            archive_path = f"media/{relative.as_posix()}"
            backed_up = resolved.is_file() and archive_path not in seen_archive_paths
            if backed_up:
                seen_archive_paths.add(archive_path)
            entries.append(
                {
                    "capture_id": capture.id,
                    "field": field,
                    "original_path": local_path,
                    "archive_path": archive_path,
                    "backed_up": backed_up,
                    **({} if backed_up else {"reason": "file is missing or already listed"}),
                }
            )
    return entries


def _export_payload(
    session: Session,
    storage_dir: Path,
) -> tuple[dict[str, Any], list[tuple[Path, str]]]:
    captures = list(
        session.scalars(select(Capture).order_by(Capture.created_at.asc(), Capture.id.asc()))
    )
    corrections = list(
        session.scalars(
            select(CaptureCorrection).order_by(
                CaptureCorrection.created_at.asc(), CaptureCorrection.id.asc()
            )
        )
    )
    relationships = list(
        session.scalars(
            select(CaptureRelation).order_by(
                CaptureRelation.created_at.asc(), CaptureRelation.id.asc()
            )
        )
    )
    media = _media_entries(captures, storage_dir)
    files: list[tuple[Path, str]] = []
    for entry in media:
        if not entry.get("backed_up"):
            continue
        source = Path(str(entry["original_path"])).expanduser()
        files.append((source, str(entry["archive_path"])))
    payload = {
        "format_version": EXPORT_FORMAT_VERSION,
        "exported_at": datetime.now(UTC),
        "captures": [_model_payload(capture, Capture) for capture in captures],
        "corrections": [
            _model_payload(correction, CaptureCorrection) for correction in corrections
        ],
        "relationships": [_model_payload(relation, CaptureRelation) for relation in relationships],
        "media": media,
    }
    return payload, files


def export_inbox(
    session_factory: sessionmaker[Session],
    storage_dir: Path,
) -> Path:
    """Write all captures, derived fields, relationships, and available media to a ZIP."""

    storage_dir = storage_dir.expanduser()
    export_dir = storage_dir / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    filename = (
        f"{EXPORT_FILENAME_PREFIX}-"
        f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.zip"
    )
    destination = export_dir / filename
    session = session_factory()
    try:
        payload, files = _export_payload(session, storage_dir)
    finally:
        session.close()

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "inbox.json",
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        )
        for source, archive_path in files:
            archive.write(source, archive_path)
    return destination


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _capture_from_payload(session: Session, payload: dict[str, Any]) -> tuple[Capture, bool]:
    capture_id = uuid.UUID(str(payload["id"]))
    capture = session.get(Capture, capture_id)
    created = False
    if capture is None:
        capture = session.scalar(
            select(Capture).where(
                Capture.platform == payload.get("platform", "discord"),
                Capture.external_message_id == int(payload["external_message_id"]),
            )
        )
    if capture is None:
        capture = Capture(id=capture_id)
        session.add(capture)
        created = True

    for column in Capture.__table__.columns:
        name = column.name
        if name == "id" or name not in payload:
            continue
        value = payload[name]
        if name in {"created_at", "deleted_at", "processed_at", "completed_at"}:
            value = _parse_datetime(value)
        if name == "embedding" and value is not None:
            value = [float(item) for item in value]
        setattr(capture, name, value)
    return capture, created


def _safe_extract_media(
    archive: zipfile.ZipFile,
    payload: dict[str, Any],
    storage_dir: Path | None,
) -> int:
    if storage_dir is None:
        return 0
    root = storage_dir.expanduser().resolve()
    restored = 0
    for entry in payload.get("media", []):
        if not isinstance(entry, dict) or not entry.get("backed_up"):
            continue
        archive_path = entry.get("archive_path")
        if not isinstance(archive_path, str) or not archive_path.startswith("media/"):
            continue
        relative = Path(archive_path.removeprefix("media/"))
        destination = (root / relative).resolve()
        try:
            destination.relative_to(root)
        except ValueError:
            continue
        if archive_path not in archive.namelist():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(archive_path) as source, destination.open("wb") as target:
            target.write(source.read())
        entry["restored_path"] = str(destination)
        restored += 1
    return restored


def restore_inbox(
    session_factory: sessionmaker[Session],
    archive_path: Path,
    *,
    storage_dir: Path | None = None,
) -> RestoreResult:
    """Restore a ZIP export idempotently into a database and optional media directory."""

    with zipfile.ZipFile(archive_path) as archive:
        payload = json.loads(archive.read("inbox.json"))
        if payload.get("format_version") != EXPORT_FORMAT_VERSION:
            raise ValueError("Unsupported inbox export format")
        media_restored = _safe_extract_media(archive, payload, storage_dir)

    session = session_factory()
    captures_created = 0
    captures_updated = 0
    corrections_restored = 0
    relationships_restored = 0
    try:
        capture_map: dict[uuid.UUID, Capture] = {}
        for item in payload.get("captures", []):
            if not isinstance(item, dict):
                continue
            capture, created = _capture_from_payload(session, item)
            capture_map[uuid.UUID(str(item["id"]))] = capture
            if created:
                captures_created += 1
            else:
                captures_updated += 1
        session.flush()

        for entry in payload.get("media", []):
            if not isinstance(entry, dict) or "restored_path" not in entry:
                continue
            capture = capture_map.get(uuid.UUID(str(entry["capture_id"])))
            if capture is None:
                continue
            metadata = dict(capture.source_metadata or {})
            media = dict(metadata.get(str(entry["field"])) or {})
            media["local_path"] = entry["restored_path"]
            metadata[str(entry["field"])] = media
            capture.source_metadata = metadata

        for item in payload.get("corrections", []):
            if not isinstance(item, dict):
                continue
            correction_id = uuid.UUID(str(item["id"]))
            if session.get(CaptureCorrection, correction_id) is not None:
                continue
            capture_id = uuid.UUID(str(item["capture_id"]))
            if capture_id not in capture_map:
                continue
            session.add(
                CaptureCorrection(
                    id=correction_id,
                    capture_id=capture_id,
                    field=str(item["field"]),
                    old_value=item.get("old_value"),
                    new_value=str(item["new_value"]),
                    created_at=_parse_datetime(item.get("created_at")) or datetime.now(UTC),
                )
            )
            corrections_restored += 1

        for item in payload.get("relationships", []):
            if not isinstance(item, dict):
                continue
            source_id = uuid.UUID(str(item["source_capture_id"]))
            target_id = uuid.UUID(str(item["target_capture_id"]))
            if source_id not in capture_map or target_id not in capture_map:
                continue
            existing = session.scalar(
                select(CaptureRelation).where(
                    CaptureRelation.source_capture_id == source_id,
                    CaptureRelation.target_capture_id == target_id,
                )
            )
            if existing is not None:
                continue
            session.add(
                CaptureRelation(
                    id=uuid.UUID(str(item["id"])),
                    source_capture_id=source_id,
                    target_capture_id=target_id,
                    relationship=str(item.get("relationship", "related")),
                    similarity=float(item["similarity"]),
                    explanation=str(item["explanation"]),
                    active=bool(item.get("active", True)),
                    feedback=item.get("feedback"),
                    notified_at=_parse_datetime(item.get("notified_at")),
                    created_at=_parse_datetime(item.get("created_at")) or datetime.now(UTC),
                )
            )
            relationships_restored += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return RestoreResult(
        captures_created=captures_created,
        captures_updated=captures_updated,
        corrections_restored=corrections_restored,
        relationships_restored=relationships_restored,
        media_restored=media_restored,
    )
