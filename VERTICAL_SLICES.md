# Personal AI Inbox — V1 Vertical Slices

**Status:** Approved planning draft  
**Source:** `PRD.md`  
**Last updated:** September 6, 2026

## Purpose

This document breaks the V1 product requirements into independently demonstrable vertical slices.

A vertical slice must deliver a complete user-visible outcome through every required layer:

```text
Discord DM interaction
→ application behavior
→ persistence and processing
→ visible response
→ automated verification
```

Database tables, provider integrations, service abstractions, and other technical components are implementation tasks inside a vertical slice. They should not become standalone feature issues unless they are genuine repository-wide enabling work.

Project scaffolding and development-environment setup are enabling work, not a product slice.

## Slice overview

| Order | Vertical slice | User-visible outcome |
| --- | --- | --- |
| 1 | Private text inbox | Send text, receive an acknowledgement, and see it in `/recent` |
| 2 | Find saved text | Ask with `/ask` and retrieve exact matching memories |
| 3 | Semantic memory | Find something using its meaning rather than exact wording |
| 4 | Natural save/query routing | Ask naturally without always typing `/ask` |
| 5 | Voice-note memory | Send a voice note, have it transcribed, and find it later |
| 6 | Webpage memory | Send an article or URL, save bounded metadata, and retrieve it |
| 7 | YouTube memory | Save a YouTube video with bounded metadata and find it later |
| 8 | GitHub repository memory | Save a repository with useful metadata and retrieve it |
| 9 | Image and screenshot memory | Send an image, understand its contents, and find it later |
| 10 | Manage and correct memories | Delete, undo, correct, complete, and inspect saved items |
| 11 | Related-memory discovery | Receive a useful connection to something previously saved |
| 12 | Export and recovery | Export or back up the inbox and recover failed processing |

## Slice 1 — Private text inbox

### User outcome

> I can send a text note to my private Discord bot and trust that it has been saved.

### End-to-end scope

- Receive text through a Discord Gateway connection.
- Restrict the bot to the configured Discord user.
- Persist the original capture before acknowledging it.
- Send a short confirmation after successful persistence.
- Display recent captures through `/recent`.
- Undo the most recent capture through `/undo`.
- Prevent duplicate Discord message deliveries from creating duplicate captures.
- Reject unauthorized users without ingesting their content.
- Test authorization, persistence, acknowledgements, and idempotency.
- Document enough setup to demonstrate the slice locally.

This slice proves that Discord, the application, and PostgreSQL work together. It must not require an LLM.

## Slice 2 — Find saved text

### User outcome

> I can ask for something I saved and get the original item back.

### End-to-end scope

- Accept explicit queries through `/ask <query>`.
- Search saved text using PostgreSQL lexical search.
- Return recognizable excerpts.
- Include the saved date and source.
- Provide a link or reference to the original content when possible.
- Return a clear response when no credible result exists.
- Add realistic retrieval test fixtures.

Example:

```text
/ask electrician
```

The bot returns the saved note about calling the electrician.

## Slice 3 — Semantic memory

### User outcome

> I can retrieve something even when I do not remember its exact words.

### End-to-end scope

- Process saved text asynchronously.
- Produce normalized text and a concise summary.
- Infer a flexible type, topics, and useful entities.
- Generate and store an embedding.
- Search embeddings with pgvector.
- Combine lexical and semantic evidence into hybrid ranking.
- Generate retrieval responses grounded in stored items.
- Track processing states and errors.
- Retry failed enrichment without requiring the original message to be resent.
- Continue preserving captures when an AI provider is unavailable.
- Test exact, conceptual, date-sensitive, and no-result queries.

Example:

```text
Saved: Cool ramen place Ahmed mentioned in sector F7
Asked: What Asian restaurant did my friend recommend?
```

This slice completes the fundamental V1 capture-and-retrieval loop.

## Slice 4 — Natural save/query routing

### User outcome

> I can ask ordinary questions without always using a command.

### End-to-end scope

- Detect high-confidence natural-language retrieval questions.
- Preserve `/ask` as an unambiguous retrieval command.
- Default to capture when routing is uncertain.
- Avoid treating question-shaped ideas such as “What if I built…” as retrieval automatically.
- Provide a visible recovery path when a message is routed incorrectly.
- Test routing against realistic captures, queries, and ambiguous messages.

The system should prioritize avoiding lost captures over eliminating every unnecessary saved query.

## Slice 5 — Voice-note memory

### User outcome

> I can record a thought instead of typing it and find it later.

### End-to-end scope

- Receive voice notes through Discord.
- Persist the capture and Discord attachment metadata before transcription.
- Preserve a durable original audio reference or stored copy.
- Transcribe the recording.
- Preserve the raw transcription and produce searchable normalized text.
- Apply the existing enrichment and embedding pipeline.
- Acknowledge receipt while longer processing continues.
- Retrieve results with access to the original voice note.
- Track and retry transcription failures.
- Test successful, empty, unsupported, and failed transcriptions.

## Slice 6 — Webpage memory

### User outcome

> I can send an article or webpage and later find it by what it discussed.

### End-to-end scope

- Detect general HTTP(S) URLs in a Discord DM.
- Preserve the exact submitted URL and accompanying user text immediately.
- Extract bounded standard metadata: title, author, publication date, description,
  domain, and up to a small number of main headings.
- Use Open Graph, standard meta tags, schema metadata, and headings where available;
  never persist the full HTML body, scripts, styles, or a page copy.
- Summarize, embed, and index the bounded metadata together with the user's context.
- Detect repeated URLs and communicate the duplicate without losing provenance.
- Fall back to the URL, submitted context, and any metadata available when a page is
  private, blocked, unavailable, unsupported, or dynamically rendered.
- Retrieve the saved result with its original URL and selected metadata visible.
- Test complete extraction, bounded fields, metadata-only fallback, duplicate
  submission, and extraction failure/retry.

A failed extraction must still produce a saved and retrievable URL item.

## Slice 7 — YouTube memory

### User outcome

> I can save a YouTube video and retrieve it by its subject.

### End-to-end scope

- Recognize canonical YouTube URLs, including `youtu.be` links.
- Preserve the exact submitted URL and accompanying user text immediately.
- Extract bounded metadata: title, channel, publication date, and a short description excerpt.
- Derive lightweight searchable context from the metadata and the user's note.
- Return a concise result with the title, channel, relevant context, and original URL.
- Fall back to the URL and submitted context when metadata is unavailable.
- Handle private, deleted, inaccessible, and malformed video links safely.
- Do not download video bytes or retrieve/store full transcripts, chapters, summaries, or key takeaways.
- Test metadata, fallback, unavailable, duplicate, malformed, and retry paths.

## Slice 8 — GitHub repository memory

### User outcome

> I can save a repository and later find it by name, technology, or purpose.

### End-to-end scope

- Recognize GitHub repository URLs and canonicalize owner/repository identity for duplicates.
- Preserve the exact submitted URL and accompanying note immediately.
- Extract only the owner, repository name, description, and topics from the repository API.
- Produce a bounded searchable representation with the user's context and embed it through the
  existing retrieval pipeline.
- Retrieve a concise repository identity/description/topics result with the original URL.
- Handle deleted, private, inaccessible, malformed, and rate-limited repositories safely while
  retaining the URL and note as fallback context.
- Preserve duplicate provenance and support extraction retry.
- Do not fetch languages, license, README, source files, dependencies, issues, or history.
- Test complete metadata, metadata-only/failure fallback, duplicate, malformed, and retry paths.

The system must not clone or analyze entire repositories during normal V1 ingestion.

## Slice 9 — Image and screenshot memory

### User outcome

> I can save a screenshot or photo and retrieve it based on what is visible.

### End-to-end scope

- Receive supported image attachments through Discord and persist capture/attachment metadata
  before visual processing.
- Preserve the original attachment reference and a durable local image copy when available.
- Generate one concise, factual, retrieval-oriented visual description and extract legible
  visible text when useful.
- Index the description, OCR, user context, and filename through the existing embedding pipeline;
  do not generate or store a separate summary.
- Retrieve results with the original attachment URL, description, visible text, and uncertainty.
- Communicate ambiguity rather than inventing visual details, identifying people, or inferring
  sensitive/private attributes.
- Track unsupported formats and vision/download failures, preserve the capture, and support retry.
- Test screenshots with text, ordinary photos, ambiguous images, unsupported formats, and provider
  failures.

## Slice 10 — Manage and correct memories

### User outcome

> I remain in control when the system misunderstands something or it is no longer relevant.

### End-to-end scope

- Inspect a saved item and its source.
- Delete a selected item.
- Complete the basic undo behavior introduced in Slice 1.
- Correct an item's summary, type, or meaning.
- Mark an actionable item as completed.
- Preserve the immutable original capture when derived information is corrected.
- Exclude deleted items from normal retrieval.
- Treat completed items appropriately during retrieval.
- Store corrections as feedback signals.
- Test deletion, correction, completion, and retrieval-state behavior.

## Slice 11 — Related-memory discovery

### User outcome

> When I save something, the bot can remind me about a strongly related thing I saved earlier.

### End-to-end scope

- Compare a processed item with existing embeddings.
- Exclude the item itself and obvious duplicates.
- Store promising relationship candidates.
- Use a conservative threshold for user-visible suggestions.
- Explain the connection briefly.
- Suppress weak, repetitive, or unhelpful suggestions.
- Allow simple feedback about whether the connection was useful.
- Test strong relationships, weak relationships, duplicates, and unrelated items.

The product should favor silence over low-quality associations.

## Slice 12 — Export and recovery

### User outcome

> I own my inbox and can recover it if something fails.

### End-to-end scope

- Export original captures and derived items in a documented format.
- Document and verify the media-backup policy.
- Provide database backup and restore instructions.
- Inspect failed processing jobs.
- Retry a selected failed item.
- Document which external providers receive personal content.
- Verify that secrets and private content are not committed.
- Test export completeness and a representative recovery path.

## Delivery checkpoints

### Checkpoint 1 — Usable capture

After Slice 1, the product is a usable private text inbox.

### Checkpoint 2 — Core hypothesis proven

After Slice 3, the complete text capture, understanding, storage, and natural retrieval loop works.

### Checkpoint 3 — Promised input coverage

After Slice 9, all content types promised by the V1 PRD are supported end to end.

### Checkpoint 4 — V1 complete

After Slice 12, the system is ready for sustained personal use and evaluation with approximately 200–500 real items.

## GitHub issue rule

Each vertical-slice issue must:

- State the user outcome.
- Describe an end-to-end demonstration through Discord.
- Define in-scope and out-of-scope behavior.
- Include failure and fallback behavior.
- Include observable acceptance criteria.
- Include the automated tests required to prove the slice.
- Be independently reviewable and leave the application in a usable state.

Do not create standalone product issues named only after technical layers such as:

- Create the database schema
- Add an embedding provider
- Build the Discord handler
- Add the service layer
- Integrate pgvector

Those are implementation tasks within the vertical slice that first needs them. The completion test for every slice is a user-visible capability that can be demonstrated through Discord.
