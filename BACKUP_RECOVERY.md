# Backup and recovery

Issue 12 adds a portable export and a documented recovery path for the personal inbox.

## Portable export

Run this from the repository with the virtual environment active:

```bash
python -m app.backup export
```

The command writes a timestamped ZIP under `STORAGE_DIR/exports/`. The archive contains:

- `inbox.json` with every original capture, derived searchable fields, processing state,
  corrections, and related-memory feedback
- A media manifest showing which local audio/image files were available
- Local audio and image copies that live inside `STORAGE_DIR`

Deleted captures are included so the export remains an audit and recovery record. The export
does not include secrets or the database password.

## Restore

Point `DATABASE_URL` at the destination database and run:

```bash
python -m app.backup restore /path/to/personal-ai-inbox-export-YYYYMMDDTHHMMSSZ-XXXXXXXX.zip
```

The restore is idempotent: running it again does not create duplicate captures, corrections, or
relationships. If `STORAGE_DIR` is configured, media files included in the archive are restored
under that directory and their local references are updated.

For a fresh database, apply the schema first:

```bash
alembic upgrade head
```

## PostgreSQL backup and restore

The ZIP export is the application-level backup. For a complete database backup, including
indexes and the `pgvector` data, use PostgreSQL's native tools:

```bash
pg_dump --format=custom --file=personal-ai-inbox.dump "$DATABASE_URL"
createdb personal_inbox_recovered
pg_restore --clean --if-exists --dbname="$RECOVERY_DATABASE_URL" personal-ai-inbox.dump
```

Run `alembic upgrade head` against a newly created database when restoring into a schema that
was not included in the dump. Keep the dump and ZIP export in a private location.

## Failed processing and retry

In Discord, use:

- `/failures` to list active captures whose processing failed, with the error and short ID
- `/retry <short-id>` to retry one failed capture
- `/retry` to retry the newest failed capture
- `/inspect <short-id>` to inspect the original context and current processing state

The original capture is committed before any provider call. A provider outage therefore leaves a
recoverable record rather than losing the message.

## Media and provider privacy policy

Discord CDN URLs are retained as provenance, but they may expire. Voice notes and images are
copied to `STORAGE_DIR/audio/` and `STORAGE_DIR/images/` when downloaded; those local copies are
included in a portable export when present. Files outside `STORAGE_DIR` are listed in the media
manifest but are not copied automatically.

Depending on the capture type, personal content may be sent to:

- OpenAI for text enrichment, embeddings, transcription, or image description/OCR
- The public webpage, YouTube, or GitHub endpoint for bounded metadata extraction

Do not commit `.env`, `data/`, exports, database dumps, or provider tokens. Review an export
before moving it to another machine.
