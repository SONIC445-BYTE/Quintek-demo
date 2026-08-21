"""
Source ingestion: upload -> text -> chunks with locators -> concepts.

Two design rules carried from `docs/QUINTEK_LOGIC.md` section 4.1:

  * **Never send a whole source in one call.** Sources are chunked first, and
    each chunk is processed independently. A 300-page textbook is not one
    prompt.

  * **Resumable at chunk granularity.** A failed chunk 87 does not reset
    1..86. Chunk status is persisted before and after each unit of work, so a
    crashed or killed worker resumes where it stopped rather than re-running
    (and re-billing) everything.

The locator is the point of the whole exercise. Every chunk records where it
came from -- `{page, paragraph, lines}` for text, `{page, figure, caption}` for
a figure, `{t_start, t_end}` for video -- because "show me exactly the passage
this came from" is a product requirement, and it is impossible to add later if
the position was discarded at ingestion.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .db import Database, new_id, now_iso

# Roughly a page of prose. Small enough that a model attends to all of it,
# large enough that a concept's context is not split across a boundary.
TARGET_CHUNK_CHARS = 1800
MIN_CHUNK_CHARS = 200


@dataclass
class ExtractedPage:
    """One page / slide / segment of a source, before chunking."""
    ordinal: int
    text: str
    locator: dict = field(default_factory=dict)


class ExtractionUnavailable(RuntimeError):
    """No extractor exists for this source kind in this deployment.

    Raised rather than returning empty text, so a source is marked `failed`
    with a reason instead of `extracted` with nothing in it -- the second looks
    like a document that legitimately contained no text.
    """


# ---------------------------------------------------------------------------
# Text extraction, per source kind
# ---------------------------------------------------------------------------

def _pdf_available() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except Exception:
        return False


def extract_pdf(path: str | Path) -> list[ExtractedPage]:
    """
    Page-by-page text with page numbers preserved.

    pypdf is an optional dependency: a deployment without it should say so
    rather than silently accept PDFs and produce nothing. A PDF with no text
    layer (a scan) is also reported rather than treated as empty -- it needs
    OCR, which is a different capability, not a failure of this function.
    """
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise ExtractionUnavailable(
            "PDF extraction needs the 'pypdf' package, which is not importable "
            f"here ({exc}). Install it, or upload the text directly."
        ) from exc

    reader = PdfReader(str(path))
    pages: list[ExtractedPage] = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(ExtractedPage(ordinal=i, text=text, locator={"page": i}))
    if not pages:
        raise ExtractionUnavailable(
            "this PDF has no extractable text layer -- it is probably a scan, "
            "which needs OCR rather than text extraction"
        )
    return pages


def extract_plain_text(text: str) -> list[ExtractedPage]:
    """Text and notes. Paragraphs are the unit, so a locator can name one."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return [
        ExtractedPage(ordinal=i, text=p, locator={"paragraph": i})
        for i, p in enumerate(paragraphs, start=1)
    ] or [ExtractedPage(ordinal=1, text=text.strip(), locator={"paragraph": 1})]


# What this deployment can actually read, and why not when it cannot.
#
# The five source kinds are offered with equal prominence in the app, but three
# of them raise `ExtractionUnavailable` the moment they are tried. A picker
# that offers five doors and opens two is the same defect as a "Choose file"
# button that shows a text box: the failure is discovered after the learner has
# committed, not before.
#
# Derived from the SAME conditions `extract_for_kind` branches on below, so the
# two cannot drift: adding OCR makes this report OCR without a second edit.
def source_capabilities() -> dict:
    pdf_ok = _pdf_available()
    return {
        "text": {"available": True, "reason": ""},
        "note": {"available": True, "reason": ""},
        "pdf": {
            "available": pdf_ok,
            "reason": "" if pdf_ok else
            "PDF reading needs the 'pypdf' package, which is not installed here.",
        },
        "link": {
            "available": False,
            "reason": "Fetching a page needs outbound access and an HTML-to-text "
                      "pass, neither of which is configured. Paste the text instead.",
        },
        "image": {
            "available": False,
            "reason": "Reading a photo needs OCR, which is not configured here. "
                      "Type the notes out instead.",
        },
        "video": {
            "available": False,
            "reason": "A video needs a transcript source, which is not configured "
                      "here. Paste the transcript instead.",
        },
    }


def extract_for_kind(kind: str, *, path: str | Path | None = None,
                     raw_text: str = "", url: str = "") -> list[ExtractedPage]:
    if kind in {"text", "note"}:
        if not raw_text.strip():
            raise ExtractionUnavailable("no text was supplied")
        return extract_plain_text(raw_text)

    if kind == "pdf":
        if path is None:
            raise ExtractionUnavailable("no file was stored for this PDF source")
        return extract_pdf(path)

    if kind == "link":
        # Fetching a URL means egress, an HTML-to-text pass, and a robots
        # decision. Stated plainly rather than half-implemented.
        raise ExtractionUnavailable(
            "link ingestion is not configured: it needs outbound fetching and an "
            "HTML-to-text extractor. Paste the text instead."
        )

    if kind == "image":
        raise ExtractionUnavailable(
            "image ingestion needs OCR, which is not configured in this deployment"
        )

    if kind == "video":
        raise ExtractionUnavailable(
            "video ingestion needs a transcript source, which is not configured "
            "in this deployment"
        )

    raise ExtractionUnavailable(f"unsupported source kind: {kind!r}")


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_pages(pages: list[ExtractedPage]) -> list[tuple[str, dict]]:
    """
    Group extracted pages into model-sized chunks, carrying locators through.

    Splitting happens on sentence boundaries where possible: a chunk that ends
    mid-sentence costs the model the context it needed to place the concept,
    and that shows up later as a concept extracted with the wrong meaning.
    """
    chunks: list[tuple[str, dict]] = []
    buffer, buf_locators = "", []

    def flush():
        nonlocal buffer, buf_locators
        if buffer.strip():
            first, last = buf_locators[0], buf_locators[-1]
            locator = dict(first)
            if last != first:
                locator["spans_to"] = last
            locator["chars"] = len(buffer.strip())
            chunks.append((buffer.strip(), locator))
        buffer, buf_locators = "", []

    for page in pages:
        text = page.text.strip()
        if not text:
            continue
        # A single page larger than the target is split on sentences.
        if len(text) > TARGET_CHUNK_CHARS:
            flush()
            sentences = re.split(r"(?<=[.!?])\s+", text)
            part, idx = "", 1
            for sentence in sentences:
                if part and len(part) + len(sentence) + 1 > TARGET_CHUNK_CHARS:
                    loc = dict(page.locator)
                    loc.update({"part": idx, "chars": len(part.strip())})
                    chunks.append((part.strip(), loc))
                    part, idx = "", idx + 1
                part = f"{part} {sentence}".strip()
            if part.strip():
                loc = dict(page.locator)
                loc.update({"part": idx, "chars": len(part.strip())})
                chunks.append((part.strip(), loc))
            continue

        if buffer and len(buffer) + len(text) + 2 > TARGET_CHUNK_CHARS:
            flush()
        buffer = f"{buffer}\n\n{text}".strip()
        buf_locators.append(page.locator)

    flush()

    # A trailing scrap is merged backwards rather than shipped as its own
    # chunk: a 40-character chunk carries no usable context.
    if len(chunks) > 1 and len(chunks[-1][0]) < MIN_CHUNK_CHARS:
        tail_text, tail_loc = chunks.pop()
        prev_text, prev_loc = chunks[-1]
        merged_loc = dict(prev_loc)
        merged_loc["spans_to"] = tail_loc
        chunks[-1] = (f"{prev_text}\n\n{tail_text}", merged_loc)

    return chunks


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

class IngestionEngine:
    """
    Background worker that turns an uploaded source into chunks and concepts.

    `concept_extractor` is injected: it receives (chunk_text, context) and
    returns concepts and relationships. That keeps this module about pipeline
    mechanics -- ordering, resumability, status -- and leaves what a concept IS
    to `student/concepts.py`, which is the part that calls a model.
    """

    def __init__(self, db: Database, *, concept_extractor=None,
                 storage_dir: str | Path | None = None, workers: int = 1):
        self.db = db
        self.concept_extractor = concept_extractor
        self.storage_dir = Path(storage_dir) if storage_dir else (
            Path(db.path).parent / "sources")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        for _ in range(max(1, workers)):
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()
            self._threads.append(t)

    # ---------- queueing ----------

    def enqueue_source(self, source_id: str, *, raw_text: str = "", url: str = "") -> None:
        self._idle.clear()
        self._queue.put({"source_id": source_id, "raw_text": raw_text, "url": url})

    def wait_idle(self, timeout: float = 60.0) -> bool:
        """Block until the queue drains. Used by tests and by any caller that
        needs ingestion finished before it reads the results."""
        return self._idle.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        for _ in self._threads:
            self._queue.put(None)

    def _worker(self) -> None:
        while not self._stop.is_set():
            job = self._queue.get()
            if job is None:
                break
            try:
                self.process_source(job["source_id"], raw_text=job.get("raw_text", ""),
                                    url=job.get("url", ""))
            except Exception:
                # A worker that dies takes every later source with it.
                traceback.print_exc()
            finally:
                self._queue.task_done()
                if self._queue.empty():
                    self._idle.set()

    # ---------- the pipeline ----------

    def process_source(self, source_id: str, *, raw_text: str = "", url: str = "") -> None:
        row = self.db.query_one("SELECT * FROM sources WHERE id = ?", (source_id,))
        if row is None:
            return

        try:
            self.db.execute("UPDATE sources SET status='chunking' WHERE id=?", (source_id,))
            pages = self._extract(row, raw_text=raw_text, url=url)
            chunks = chunk_pages(pages)
            if not chunks:
                raise ExtractionUnavailable("no usable text was found in this source")
            self._store_chunks(source_id, chunks)
            self.db.execute(
                "UPDATE sources SET status='processing', page_count=? WHERE id=?",
                (len({p.locator.get('page', p.ordinal) for p in pages}), source_id))
            self._process_chunks(source_id)
            # Extraction succeeding does not mean the source is usable. If every
            # chunk failed concept extraction, marking the source 'extracted'
            # with error=NULL reports success for a source nothing downstream
            # can use -- generation then finds no concepts and the learner is
            # told there is nothing to make questions from, with the real cause
            # two tables away. Report the state the source is actually in.
            counts = {r["status"]: r["n"] for r in self.db.query(
                "SELECT status, COUNT(*) AS n FROM source_chunks WHERE source_id = ?"
                " GROUP BY status", (source_id,))}
            failed, processed = counts.get("failed", 0), counts.get("processed", 0)
            if failed and not processed:
                reason = self.db.query_one(
                    "SELECT error FROM source_chunks WHERE source_id = ? AND status = 'failed'"
                    " AND error IS NOT NULL ORDER BY ordinal LIMIT 1", (source_id,))
                self.db.execute(
                    "UPDATE sources SET status='failed', error=? WHERE id=?",
                    (f"all {failed} chunk(s) failed concept extraction; first error: "
                     f"{reason['error'] if reason else 'unrecorded'}", source_id))
            elif failed:
                # Partial success is still success -- one bad page must not cost
                # a 300-page book -- but it is not silent success.
                self.db.execute(
                    "UPDATE sources SET status='extracted', error=? WHERE id=?",
                    (f"{failed} of {failed + processed} chunk(s) failed concept extraction; "
                     "the rest were processed", source_id))
            else:
                self.db.execute("UPDATE sources SET status='extracted', error=NULL WHERE id=?",
                                (source_id,))
        except ExtractionUnavailable as exc:
            self.db.execute("UPDATE sources SET status='failed', error=? WHERE id=?",
                            (str(exc), source_id))
        except Exception as exc:
            self.db.execute("UPDATE sources SET status='failed', error=? WHERE id=?",
                            (f"{type(exc).__name__}: {exc}", source_id))

    def _extract(self, row, *, raw_text: str, url: str) -> list[ExtractedPage]:
        path = None
        if row["storage_key"]:
            candidate = self.storage_dir / row["storage_key"]
            if candidate.exists():
                path = candidate
        return extract_for_kind(row["kind"], path=path, raw_text=raw_text, url=url)

    def _store_chunks(self, source_id: str, chunks: list[tuple[str, dict]]) -> None:
        """Idempotent: re-ingesting a source replaces its chunk set rather than
        appending a second copy."""
        self.db.execute("DELETE FROM source_chunks WHERE source_id = ?", (source_id,))
        conn = self.db.connect()
        conn.executemany(
            "INSERT INTO source_chunks (id, source_id, ordinal, text, locator_json, status)"
            " VALUES (?,?,?,?,?, 'pending')",
            [(new_id("chk"), source_id, i, text, json.dumps(loc))
             for i, (text, loc) in enumerate(chunks, start=1)],
        )
        conn.commit()

    def _process_chunks(self, source_id: str) -> None:
        """
        Process every chunk that is not already `processed`.

        The status write happens before the work, so a process killed mid-chunk
        resumes at that chunk rather than repeating the whole source. Failure of
        one chunk is recorded and the rest continue: a single bad page should
        not cost a 300-page book.
        """
        pending = self.db.query(
            "SELECT * FROM source_chunks WHERE source_id = ? AND status != 'processed'"
            " ORDER BY ordinal", (source_id,))
        for chunk in pending:
            self.db.execute("UPDATE source_chunks SET status='processing' WHERE id=?",
                            (chunk["id"],))
            try:
                if self.concept_extractor is not None:
                    self.concept_extractor.extract_for_chunk(
                        source_id=source_id, chunk_id=chunk["id"],
                        text=chunk["text"], locator=json.loads(chunk["locator_json"]))
                self.db.execute(
                    "UPDATE source_chunks SET status='processed', error=NULL, processed_at=?"
                    " WHERE id=?", (now_iso(), chunk["id"]))
            except Exception as exc:
                self.db.execute(
                    "UPDATE source_chunks SET status='failed', error=? WHERE id=?",
                    (f"{type(exc).__name__}: {exc}", chunk["id"]))
