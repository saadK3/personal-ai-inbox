# Personal AI Inbox — V1 Product Requirements Document

**Status:** Draft for implementation  
**Audience:** Project owner and Codex  
**Initial user:** One person (the project owner)  
**Primary interface:** Discord direct messages
**Last updated:** September 6, 2026

## 1. Product summary

Personal AI Inbox is a private Discord DM-based inbox for capturing miscellaneous parts of everyday life without organizing them at the time of capture.

The user can send text, voice notes, URLs, YouTube links, GitHub repositories, images, and screenshots to a Discord bot. The system safely stores the original submission, extracts useful information, creates a searchable semantic representation, and later retrieves relevant items through natural-language questions.

The core principle is:

> **Inbox first. Structure later.**

V1 is a personal tool and an experiment. It is not intended to be a multi-user SaaS product. The objective is to create something useful enough for daily personal use, learn from real captured data, and decide later whether the product should expand.

## 2. Problem

Useful information is currently scattered across messaging apps, notes, bookmarks, screenshots, saved videos, voice recordings, and memory. Capturing something often requires deciding where it belongs and how it should be categorized. That friction causes information to be saved inconsistently or not at all.

Even when something is saved, it is often difficult to retrieve because the user remembers its meaning but not its exact title, wording, application, or location.

The product should remove the need to organize information during capture and make later retrieval possible using ordinary language.

## 3. V1 objective

V1 must prove one core experience:

> The user can send random parts of their life to one private inbox and later find them naturally, while the system begins to identify useful connections between related items.

V1 is successful when the user begins to trust the inbox as the default place to save things and can reliably recover items they only vaguely remember.

## 4. Target user

V1 has one authorized user: the project owner.

The system may assume a single-user environment, but access to the Discord bot must still be restricted by an allowlisted Discord user ID. V1 processes direct messages only; messages from other users or shared server channels must not be ingested.

Multi-user accounts, teams, onboarding, subscriptions, and public availability are outside V1.

## 5. Core user experience

### 5.1 Capture

The user can send the bot:

- Plain text
- Voice notes
- General URLs and articles
- YouTube links
- GitHub repository links
- Images and screenshots

The user does not have to select a folder, category, or tag.

For every accepted submission, the system must:

1. Save the original input and source metadata before depending on AI processing.
2. Send a prompt acknowledgement.
3. Process the content into searchable text and metadata.
4. Generate a short summary, flexible inferred type, topics, and embedding.
5. Record any processing failure so it can be inspected or retried without losing the original submission.

Example acknowledgements:

- `Saved: grocery reminder — bread, eggs, toothpaste.`
- `Saved: restaurant to try — Korean restaurant in F7.`
- `Saved. I’m still processing the audio.`

Acknowledgements should be concise. The bot should feel like an inbox, not a conversational assistant that demands follow-up after each capture.

### 5.2 Save-versus-query behavior

Preserving input is more important than guessing perfectly.

- `/ask <question>` always performs retrieval and does not save the question as a new memory.
- Clearly expressed natural-language retrieval questions may also be treated as queries.
- Other ordinary messages default to capture.
- If routing is uncertain, the system should prefer saving the message rather than silently discarding it as a query.

Examples of queries:

- `/ask What restaurants did I want to try?`
- `What was that history video I saved?`
- `Did I save anything about Norway?`
- `Show me product ideas from this month.`
- `What GitHub repository did I send a few weeks ago?`

### 5.3 Retrieval

The system should search using a combination of:

- Semantic similarity
- Exact or lexical text matching
- Available metadata such as date, source type, inferred type, people, places, and topics

Results should prioritize usefulness rather than embedding similarity alone.

Each result should include enough context to recognize it:

- Short summary or relevant excerpt
- Date saved
- Source type
- Original URL or a way to reach the original Discord content when applicable

The original item must remain accessible. The system must not rely exclusively on an AI-generated summary.

If there is no strong result, the bot should say so rather than fabricate an answer. It may show weaker candidate matches while labeling them as uncertain.

### 5.4 Related memories

After processing a new item, the system should compare it with previously stored items.

V1 may surface a previous item when the relationship is unusually strong, for example:

`Saved. This looks related to a restaurant recommendation you saved last month.`

The product should favor silence over low-quality or obvious associations. Similarity scores and candidate relationships may be stored internally even when nothing is shown to the user.

### 5.5 Basic control

The user must be able to:

- View recently saved items with `/recent`
- Undo the most recent capture with `/undo`
- Delete a specific item
- Correct an item's summary or inferred meaning in a simple way
- Mark an actionable item as completed when applicable

Corrections must not change or destroy the original captured content.

## 6. Content processing requirements

### Text

- Preserve the original text exactly.
- Use the text as the initial normalized content.
- Extract a concise summary, likely intent or type, topics, and useful entities.

### Voice notes

- Preserve Discord attachment identifiers and relevant source metadata.
- Download or durably store the audio according to the selected storage policy.
- Transcribe the recording using an external speech-to-text provider.
- Use the transcription as normalized searchable content.
- Keep the original transcription alongside any cleaned version.

### General URLs

- Preserve the exact submitted URL.
- Extract at least title, description, and domain when available.
- Extract readable page content where reasonably possible.
- A blocked, private, unavailable, or dynamically rendered page must not prevent the URL itself from being saved.

### YouTube links

- Preserve the submitted URL.
- Extract bounded title, channel, publication date, and a short description when available.
- Make the video searchable by its metadata and the user's accompanying note.
- Do not download video bytes or store transcripts, chapters, summaries, or key takeaways in V1.
- Metadata failure must not block saving the video.

### GitHub repositories

- Treat these as URLs with repository-specific metadata limited to owner, repository name,
  description, and topics.
- Do not fetch the README, source tree, dependencies, issues, history, languages, or license;
  do not clone repositories during normal ingestion in V1.

### Images and screenshots

- Preserve the Discord attachment reference and relevant metadata.
- Store the image durably according to the selected storage policy.
- Use a vision-capable model to create a factual retrieval-oriented description.
- Extract visible text when useful.
- Index the description, visible text, filename, and user's context for retrieval without a
  separate generated summary in V1.
- Communicate uncertainty and do not identify people or infer private or sensitive attributes.

## 7. Conceptual data model

V1 should distinguish between an immutable capture and the searchable items derived from it.

### Capture

Represents the original Discord submission.

Suggested fields:

- `id`
- `platform`
- `external_message_id`
- `conversation_id`
- `sender_id`
- `source_type`
- `raw_text`
- `source_metadata`
- `original_file_reference` or storage location
- `created_at`
- `processing_status`
- `processing_error`

### Item

Represents a meaningful searchable unit derived from a capture. One capture may produce one or more items.

Suggested fields:

- `id`
- `capture_id`
- `normalized_text`
- `summary`
- `inferred_type`
- `why_saved` when known
- `topics`
- `entities`
- `metadata`
- `status`
- `created_at`
- `completed_at`
- `embedding`

Inferred types may include `idea`, `reminder`, `task`, `shopping`, `want_to_try`, `place`, `saved_content`, `video`, `article`, `product`, and `reference`. These values must remain flexible rather than becoming a rigid ontology.

### Relationship

Optional for the first implementation milestone, but the schema may support:

- `source_item_id`
- `target_item_id`
- `relationship`
- `confidence`
- `created_at`

Simple nearest-neighbour similarity is sufficient for V1. A graph database is not required.

## 8. Functional requirements

### Reliability

- Original input must be persisted before enrichment begins.
- AI or third-party service failures must not lose captures.
- Enrichment jobs must be retryable.
- Repeated Discord message deliveries must not create duplicate captures.
- Duplicate URLs should be detected and communicated without unexpectedly deleting either submission.
- Processing status must be observable through logs or a simple diagnostic mechanism.

### Search quality

- Search must combine semantic and lexical evidence.
- Queries containing names, locations, repository names, dates, or exact phrases should not depend only on embeddings.
- Search responses must be grounded in stored items.
- Results should expose provenance and original sources.

### Privacy and security

- Only the allowlisted Discord account can use the bot.
- Secrets must be provided through environment variables and never committed.
- Logs must not unnecessarily expose private message bodies, tokens, or downloaded files.
- The user must be able to delete saved items.
- The database and stored files must be exportable or back-upable.
- External providers that receive message content, images, or audio should be documented.

### Provider modularity

Speech-to-text, embeddings, language reasoning, vision, and content extraction should be behind small internal interfaces so providers can be replaced without rewriting the ingestion and retrieval layers.

V1 does not need a universal plugin framework. Straightforward provider abstractions are sufficient.

## 9. Proposed technical architecture

```text
Discord DM Bot (Gateway process)
    ↓
Capture service / FastAPI API
    ↓
Durable capture storage
    ↓
Asynchronous content processing
    ├── transcription
    ├── URL/metadata extraction
    ├── vision description/OCR
    ├── summarization and entity extraction
    └── embeddings and relationship candidates
    ↓
PostgreSQL + pgvector
    ↓
Hybrid retrieval and grounded response generation
```

The exact hosting provider and background-job implementation may be selected during architecture setup. V1 should prefer the smallest dependable solution and must not require a local GPU.

## 10. Non-goals

V1 will not include:

- A mobile or desktop application
- A polished web frontend
- Multiple users or organizations
- Public signup and authentication flows
- Payments or subscriptions
- Manual folder management
- A fixed, elaborate taxonomy
- Graph visualization
- A dedicated graph database
- Sophisticated automatic clustering
- Dozens of external integrations
- Autonomous multi-agent workflows
- Full calendar, reminder, or task-management functionality
- Location-aware or always-on contextual notifications
- Production-scale infrastructure

## 11. V1 acceptance criteria

V1 is considered usable when all of the following are true:

1. The authorized user can send text, a voice note, a URL, a YouTube link, a GitHub link, and an image to the bot.
2. Every supported submission is durably stored and acknowledged even if later enrichment fails.
3. The user can inspect the original content and its processing state.
4. Successful processing produces a bounded searchable representation, metadata, and an embedding;
   text/voice/webpage captures also receive a generated summary, while YouTube, GitHub, and image
   captures rely on their source-specific metadata and context.
5. The user can ask natural-language questions and receive grounded results containing source and date information.
6. Retrieval handles both conceptual questions and exact identifying terms.
7. The bot does not fabricate a saved item when no credible result exists.
8. `/recent`, `/undo`, deletion, correction, and basic completion behavior work.
9. Duplicate Discord message deliveries do not produce duplicate records.
10. Processing failures can be retried without resending the original message.
11. Access is restricted to the configured Discord user.
12. Secrets are not committed, and setup is documented for a fresh environment.

## 12. Implementation milestones

### Milestone 1 — Durable text capture

- Project setup and local development instructions
- Discord Gateway bot
- Single-user allowlist
- PostgreSQL schema and migrations
- Text capture persisted before acknowledgement
- `/recent`, `/undo`, and deletion
- Tests for authorization, persistence, and message idempotency

### Milestone 2 — Enrichment and search

- Background processing with retries
- Summaries, flexible types, topics, entities, and embeddings
- Hybrid semantic and lexical retrieval
- `/ask` and high-confidence natural-language query routing
- Grounded results with dates and original sources
- Search-quality test fixtures

### Milestone 3 — Rich inputs

- Voice download and transcription
- General URL extraction
- YouTube and GitHub metadata
- Image storage, description, and visible-text extraction
- Graceful partial failures for every processor

### Milestone 4 — Connections and feedback

- Related-item candidates
- Conservative related-memory notifications
- Corrections and completion state
- Basic diagnostics for processing and retrieval
- Data export or documented backup procedure

### Milestone 5 — Personal-use evaluation

- Use the bot as the primary personal inbox until it contains roughly 200–500 real items.
- Review retrieval failures, routing mistakes, unsupported inputs, and useful connections.
- Decide which behaviors deserve improvement before considering clustering, richer resurfacing, or additional users.

## 13. Success measures

V1 should be evaluated primarily through actual personal use rather than vanity metrics.

Useful measures include:

- Percentage of captures saved without loss
- Percentage of processing jobs completed successfully
- Whether the desired item appears in the first few search results
- Time required to recover a vaguely remembered item
- Frequency of incorrect save-versus-query routing
- Number of related-memory suggestions judged genuinely useful
- Continued use after the initial novelty period
- Whether the user begins choosing this inbox instead of several separate saving locations

The central qualitative test is:

> When the user vaguely remembers saving something, do they trust the bot enough to ask it first?

## 14. Product principles for implementation

- Preserve first; enrich second.
- Default to capture when intent is uncertain.
- Keep the original content accessible.
- Prefer grounded uncertainty over confident invention.
- Use hybrid retrieval rather than vector similarity alone.
- Keep acknowledgements fast and brief.
- Make failures recoverable and observable.
- Avoid premature structure and infrastructure.
- Let real personal usage determine what comes after V1.

## 15. Open implementation decisions

These decisions should be made during the architecture and setup phase without changing the product scope:

- Hosting platform
- Object/file storage location
- Background-job mechanism
- Initial LLM, embedding, speech-to-text, and vision providers
- URL extraction library or service
- Exact thresholds for query routing and related-item notifications
- Retention and backup policy for downloaded Discord media

Choices should optimize for ease of operation, privacy, recoverability, and low cost for one user.
