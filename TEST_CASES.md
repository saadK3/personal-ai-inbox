# Personal AI Inbox — End-to-End Test Cases

These cases cover the user-visible behavior delivered by Slices 1–10.

## Preconditions

- PostgreSQL is running and the database is current: `make migrate`.
- The bot is running with the allowlisted Discord user and a valid provider key.
- Use a private DM with the bot. Server-channel messages should be ignored.
- Use `/recent` to see the current item ordinals and short IDs. Ordinal `1` is the
  newest active item; a short ID remains stable while the item is retained.

## Capture and retrieval

| ID | Action | Expected result |
| --- | --- | --- |
| T01 | Send `Remember to buy bread and eggs` | One acknowledgement; `/recent` shows one captured text item. Re-delivery of the same Discord message must not create a second capture. |
| T02 | Send `/ask bread` | The saved note is returned with date and Discord source. Send `/ask something-never-saved` and confirm a clear no-match response. |
| T03 | Save `I need to compare local football prices` and wait for processing. Ask `/ask football prices`. | The item is found by exact terms. Ask `Did I save anything about football?` and confirm natural retrieval without a new saved question. |
| T04 | Send `What should I eat tonight?` | It is saved as a note because it is advice-shaped and ambiguous. Send `What if I built an AI inbox?` and confirm it is also saved, not treated as retrieval. `/save What have I saved?` must explicitly save that text. |
| T05 | Send a supported OGG/MP3 voice attachment. | The bot acknowledges immediately, stores the attachment reference, then exposes the transcription in `/recent` and retrieval. A failed transcription remains saved and `/retry` schedules it again. Unsupported audio is saved with a clear status. |

## URL memories

| ID | Action | Expected result |
| --- | --- | --- |
| T06 | Send a public article URL with a note. | The exact URL is acknowledged first. After extraction, search results show bounded title/author/date/description/headings and the original URL; full HTML/scripts are not shown. Send the same URL with different casing/trailing slash and confirm a new provenance item marked as a duplicate. |
| T07 | Send a YouTube watch URL and a `youtu.be` form for the same video. | Title/channel/date/short description are searchable and the original URL is returned. Duplicate provenance is retained. Confirm no transcript, chapters, video bytes, or generated summary are stored. A failed metadata request is visible and retryable. |
| T08 | Send `https://github.com/openai/openai-python` with a note. | Results show the `owner/repository` identity, description, topics, and original URL. Only the repository API metadata is used: README, source, issues, history, dependencies, languages, and license are not fetched. Send a casing/`.git` variant and confirm duplicate provenance. Test `/retry` after a simulated/unavailable repository. |

## Image memories

| ID | Action | Expected result |
| --- | --- | --- |
| T09 | Attach a PNG/JPG screenshot containing readable text and add a short caption. | The image is acknowledged before vision work, a durable local copy/reference is retained, and `/recent` eventually shows completion. Ask a question about the visible text/content; results include the factual description, OCR text, uncertainty when applicable, and original attachment URL. |
| T10 | Attach an ordinary photo with little/no text, then an ambiguous or blurry image. | The photo is described without invented details or sensitive/person identification. Ambiguity is stated as uncertainty. Attach an unsupported HEIC/TIFF file and confirm it is saved with an unsupported status and no processing job. A provider/download failure remains saved and `/retry` schedules vision again. |

## Management and correction (Slice 10)

1. Send two notes, then `/recent`. Confirm each line has an ordinal and short ID.
2. Run `/inspect 1`. Confirm it includes original context, search text, processing state,
   derived fields, and a source reference. Repeat with the short ID.
3. Run `/correct 1 meaning corrected searchable meaning`, then `/correct 1 summary corrected
   summary`, then `/correct 1 type recommendation`. Confirm acknowledgements mention that the
   original capture was preserved. `/inspect 1` must show the corrected fields while the original
   text remains unchanged. After enrichment runs again, the corrections must still be present.
4. Run `/complete 1` twice. The first marks the item complete; the second reports it is already
   complete. `/recent` and `/ask` show `completed`, while the item remains retrievable.
5. Run `/delete 1`, then `/ask` for a distinctive term from that item. It must be excluded from
   normal retrieval. Inspect it by its short ID and confirm the audit record/source remains.
6. Save a new note and run `/undo` using a new message. Run `/undo` again using another new
   message; the second command must report that the latest capture is already deleted and must
   not delete an older note.
7. From a non-allowlisted Discord account, try `/delete`, `/correct`, and `/complete`. The bot
   must not respond or mutate any capture.

## Automated regression checks

From the activated virtual environment:

```bash
make test
.venv/bin/ruff check app tests
```

The Slice 10 implementation and the previous slices currently pass 50 automated tests. The
manual cases above cover Discord Gateway behavior, real attachments, provider fallbacks, and
the personal-use workflow that unit tests cannot fully exercise.
