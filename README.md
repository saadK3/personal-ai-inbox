# Personal AI Inbox

A private Discord DM inbox for capturing miscellaneous parts of everyday life and retrieving them later with natural language.

The product requirements are in [PRD.md](PRD.md), the implementation plan is in
[VERTICAL_SLICES.md](VERTICAL_SLICES.md), and the end-to-end QA checklist is in
[TEST_CASES.md](TEST_CASES.md).

## Current status

The repository contains the development scaffold and all twelve V1 product slices. It includes:

- FastAPI application with liveness and database-readiness endpoints
- Discord Gateway bot for private DM capture
- PostgreSQL 16 with the `pgvector` extension through Docker Compose
- SQLAlchemy and Alembic configuration
- Environment-variable configuration with a safe example file
- Pytest and Ruff configuration
- A first migration that enables pgvector

Vertical Slices 1 and 2—private Discord text capture and exact text retrieval—
are implemented, Slice 3 adds semantic memory and hybrid retrieval, Slice 4 adds
natural save/query routing, Slice 5 adds voice-note memory, Slice 6 adds webpage
memory, Slice 7 adds YouTube memory, Slice 8 adds GitHub repository memory, Slice 9
adds image/screenshot memory, Slice 10 adds user-controlled management and
corrections, Slice 11 adds conservative related-memory discovery, and Slice 12
adds export and recovery. Text captures, voice-note transcripts,
webpage metadata, YouTube metadata, GitHub metadata, and image descriptions/OCR are
acknowledged first, then processed asynchronously. Text, voice, and webpage captures
also receive normalized text, a summary, type, topics, entities, and an embedding;
YouTube, GitHub, and image captures use their bounded source metadata/context for
search and intentionally do not store a separate generated summary. Webpage captures retain the submitted
URL and accompanying note, plus bounded metadata (title, author, publication date,
description, domain, and main headings); the HTML body, scripts, and styles are
never stored. GitHub captures retain only the owner/repository identity,
description, and topics; README, source, issues, history, languages, and license
are not fetched. Image captures retain the Discord attachment reference and a
durable local copy, then store one factual description, legible visible text, and
uncertainty when useful; the system does not identify people or infer private
attributes. Clear questions about saved memories can be asked without `/ask`;
uncertain or idea-shaped questions remain captures. Voice attachments are saved
with their Discord metadata and a local copy before transcription. The `/save
<text>` command provides an explicit recovery path when a message should be
saved. The original capture is retained when a provider fails, and metadata/vision
extraction can be retried with `/retry`.

## Requirements

- Python 3.12+
- Docker Desktop (or another Docker Compose implementation)

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
docker compose up -d db
alembic upgrade head
```

If Docker Compose is installed as the standalone command on macOS, use
`docker-compose` in place of `docker compose`.

Create a Discord application and bot in the [Discord Developer
Portal](https://discord.com/developers/applications). Copy the bot token from
the Bot page and set it as `DISCORD_BOT_TOKEN`. Copy your own Discord user ID
with Developer Mode enabled and set it as `DISCORD_ALLOWED_USER_ID`.

For this Gateway-based V1, do not configure an Application ID, Public Key, or
Interactions Endpoint URL in `.env`. The Application ID is only needed for
install links and future slash-command registration; the Public Key is used
when receiving HTTP interactions. This bot receives messages over Discord's
Gateway instead, so the interactions endpoint can remain blank.

Install the bot into a private server using the application's Installation or
OAuth2 install link, then open a direct message with the bot. V1 ignores
messages posted in server channels. Keep the bot token private.

For semantic enrichment, create an OpenAI API key and set `OPENAI_API_KEY` in
`.env`. The defaults are intentionally explicit and economical:

```text
OPENAI_MODEL=gpt-5.6-luna
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
OPENAI_EMBEDDING_DIMENSIONS=1536
OPENAI_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe
OPENAI_VISION_MODEL=gpt-5.6-luna
```

The model settings may be overridden for experiments, but the database schema
expects 1,536-dimensional embeddings. If the API key is unavailable, captures
are still saved and `/ask` plus natural memory questions fall back to exact
lexical search.

Start the API (optional health/readiness server):

```bash
uvicorn app.main:app --reload
```

Verify it:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/ready
```

Start the Discord bot in a separate terminal:

```bash
make bot
```

The bot maintains a Discord Gateway connection, so it does not require a
public HTTPS webhook URL. Send it a direct message from the allowlisted
Discord account. V1 ignores messages posted in server channels.

Use `/recent` to inspect processing states. A failed item can be retried with
`/retry`; pending captures are also resumed automatically when the bot restarts.
Use `/ask <query>` for explicit retrieval, ask a clear memory question in plain
language, or use `/save <text>` to force a message to be stored. Send a webpage
or YouTube URL (optionally with a note) to save bounded metadata and make it
searchable; the original URL is shown in results. YouTube links use only bounded metadata
(title, channel, publication date, and a short description); V1 does not download
video bytes or retrieve transcripts, chapters, summaries, or key takeaways. You
can also send a short audio attachment;
the bot will acknowledge it immediately, retain the original attachment reference
and local copy, then transcribe and enrich it in the background. `/recent` shows
transcription, metadata, vision, or enrichment failures, and `/retry` retries
the latest failed stage. GitHub repository links use only the repository identity,
description, and topics. Image attachments are copied locally and analyzed for a
factual description and visible text; unsupported formats are saved with a clear
status and can be retrieved by their original attachment URL. `/recent` shows
numbered items and short IDs for management commands: `/inspect <item>` displays
the original context and source, `/delete <item>` soft-deletes an item, `/complete
<item>` marks it complete, and `/correct <item> <summary|type|meaning> <value>`
updates derived fields while preserving the original capture. `/undo` safely
removes the latest capture and will not remove an older item on a second attempt.
After a strong match is found, the bot may send one conservative related-memory notice. Use
`/related [item]` to review stored connections and `/feedback <relation-id> useful` or
`/feedback <relation-id> not useful` to teach it what is helpful. `/failures` lists failed
processing jobs and `/retry <item>` retries a selected failure. `/export` creates a ZIP export
and sends it as a Discord attachment. The full backup, restore, media, and provider-privacy
policy is documented in [BACKUP_RECOVERY.md](BACKUP_RECOVERY.md); the command-line equivalents
are `make export` and `make restore ARCHIVE=/path/to/export.zip`.

Run checks:

```bash
pytest
ruff check .
```

Or use the Makefile shortcuts:

```bash
make install
make db-up
make migrate
make dev
make bot
make test
```

## Configuration

Copy `.env.example` to `.env` for local development. Do not commit `.env` or any real provider tokens. Discord and model-provider settings are required only when running the corresponding integrations.

## Project structure

```text
app/
├── api/              # HTTP routes
├── channels/         # Discord and future chat-channel adapters
├── core/             # Settings and cross-cutting application concerns
├── models/           # SQLAlchemy domain models
├── providers/        # External provider interfaces and adapters
├── services/         # Application and domain services
├── workers/          # Background processing entry points
├── db.py             # Database engine and session helpers
└── main.py           # FastAPI application factory
migrations/           # Alembic configuration and revisions
tests/                # Automated tests
```

Technical components should be added as part of the vertical slice that first needs them. See `VERTICAL_SLICES.md` for the sequencing rule.
