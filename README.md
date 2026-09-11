# Personal AI Inbox

A private Discord DM inbox for capturing miscellaneous parts of everyday life and retrieving them later with natural language.

The product requirements are in [PRD.md](PRD.md), and the implementation plan is in [VERTICAL_SLICES.md](VERTICAL_SLICES.md).

## Current status

The repository contains the development scaffold and the first six product slices. It includes:

- FastAPI application with liveness and database-readiness endpoints
- Discord Gateway bot for private DM capture
- PostgreSQL 16 with the `pgvector` extension through Docker Compose
- SQLAlchemy and Alembic configuration
- Environment-variable configuration with a safe example file
- Pytest and Ruff configuration
- A first migration that enables pgvector

Vertical Slices 1 and 2—private Discord text capture and exact text retrieval—
are implemented, Slice 3 adds semantic memory and hybrid retrieval, Slice 4 adds
natural save/query routing, Slice 5 adds voice-note memory, and Slice 6 adds
webpage memory. Text captures, voice-note transcripts, and webpage metadata are
acknowledged first, then enriched asynchronously with normalized text, a summary,
type, topics, entities, and an embedding. Webpage captures retain the submitted
URL and accompanying note, plus bounded metadata (title, author, publication date,
description, domain, and main headings); the HTML body, scripts, and styles are
never stored. Clear questions about saved memories can be asked without `/ask`;
uncertain or idea-shaped questions remain captures. Voice attachments are saved
with their Discord metadata and a local copy before transcription. The `/save
<text>` command provides an explicit recovery path when a message should be
saved. The original capture is retained when a provider fails, and webpage
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
URL (optionally with a note) to save its bounded metadata and make it searchable;
the original URL is shown in results. You can also send a short audio attachment;
the bot will acknowledge it immediately, retain the original attachment reference
and local copy, then transcribe and enrich it in the background. `/recent` shows
transcription, webpage-extraction, or enrichment failures, and `/retry` retries
the latest failed stage.

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
