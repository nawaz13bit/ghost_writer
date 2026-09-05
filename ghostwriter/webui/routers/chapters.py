"""Chapter lifecycle: draft, revise (LLM or manual), diff, and the
approve/finalize pipeline (editor -> continuity/voice check -> copyedit ->
approve -> bible sync -> outline sync), which runs as a background job so
the UI can poll progress instead of blocking on ~8 serial LLM calls."""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ghostwriter.agents.base import AIOutputError
from ghostwriter.llm_client import LLMCancelled
from ghostwriter.memory.story_bible import slugify
from ghostwriter.webui.deps import current_chapter_text, get_author, load_bible, maybe_compact_history, require_chapter
from ghostwriter.webui.diffing import word_diff
from ghostwriter.webui.state import (
    bible_manager, book_critique, copy_editor, craft_checker, editor, fact_checker, outliner,
    pacing_checker, researcher, reviser, stakes_checker, timeline_extractor, translators, voice_checker,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chapters"])


class InstructionRequest(BaseModel):
    instruction: str


class ApproveRequest(BaseModel):
    history_id: int


# In-memory job stores for the streaming draft/revise calls' live progress -
# same single-user-local pattern as _finalize_jobs below, just keyed
# separately since these are simpler (one LLM call, no step list).
_draft_jobs: dict[str, dict[str, Any]] = {}
_revise_jobs: dict[str, dict[str, Any]] = {}
_critique_book_jobs: dict[str, dict[str, Any]] = {}
_translate_book_jobs: dict[str, dict[str, Any]] = {}

CRITIQUE_CHECKERS = [pacing_checker, stakes_checker, craft_checker]


@router.get("/api/translate-languages")
def translate_languages() -> list[dict[str, Any]]:
    return [
        {"key": t.language.key, "name": t.language.name, "pdf_supported": t.language.pdf_supported}
        for t in translators.values()
    ]


def _assign_note_ids(bible, proposals: list[dict]) -> list[dict]:
    """Gives each research proposal a stable id (matching add_research_note's
    slugify+dedupe scheme) before it's ever saved, so the [^id] markers the
    draft emits still resolve correctly once the writer approves the note
    later via the review queue."""
    existing_ids = {n["id"] for n in bible.data["research_notes"]}
    for proposal in proposals:
        base_id = slugify(proposal["topic"])
        note_id, n = base_id, 2
        while note_id in existing_ids:
            note_id = f"{base_id}-{n}"
            n += 1
        existing_ids.add(note_id)
        proposal["id"] = note_id
    return proposals


def _run_draft_job(job_id: str, slug: str, chapter_num: int) -> None:
    job = _draft_jobs[job_id]
    try:
        bible = load_bible(slug)
        entry = bible.outline_entry(chapter_num)
        if entry is None:
            raise ValueError(f"No outline entry for chapter {chapter_num}")

        job["phase"] = "researching"
        proposals = researcher.suggest_for_chapter(bible, chapter_num, entry)
        research_notes = _assign_note_ids(bible, proposals) if proposals else None
        job["research_proposals"] = research_notes or []

        job["phase"] = "drafting"
        author = get_author(bible)
        target_words = bible.data.get("chapter_target_words", 1800)
        draft = author.draft_chapter(
            bible, chapter_num, target_words=target_words,
            on_delta=lambda text: job.__setitem__("partial_text", text),
            research_notes=research_notes,
        )
        bible.upsert_chapter(chapter_num, title=entry["title"])
        revision = bible.add_chapter_revision(chapter_num, draft, source="draft")
        ch = bible.get_chapter(chapter_num)
        compacted_count = maybe_compact_history(bible, ch["history"], f"Chapter {chapter_num}")
        job["result"] = {**revision, "compacted_count": compacted_count}
    except Exception as exc:
        job["error"] = str(exc)
    finally:
        job["done"] = True


@router.post("/api/projects/{slug}/chapters/{chapter_num}/draft")
def draft_chapter(slug: str, chapter_num: int) -> dict[str, Any]:
    """Kicks off the chapter draft in a background thread and returns a
    job_id immediately, so the UI can poll partial_text for live progress
    instead of showing a static busy label for the 30-90+s this single call
    takes."""
    bible = load_bible(slug)
    if bible.outline_entry(chapter_num) is None:
        raise HTTPException(404, f"No outline entry for chapter {chapter_num}")

    job_id = uuid.uuid4().hex
    _draft_jobs[job_id] = {
        "done": False, "error": None, "result": None, "partial_text": "",
        "phase": "drafting", "research_proposals": [],
    }
    threading.Thread(target=_run_draft_job, args=(job_id, slug, chapter_num), daemon=True).start()
    return {"job_id": job_id}


@router.get("/api/projects/{slug}/chapters/{chapter_num}/draft/status/{job_id}")
def draft_chapter_status(slug: str, chapter_num: int, job_id: str) -> dict[str, Any]:
    job = _draft_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job


def _run_revise_job(job_id: str, slug: str, chapter_num: int, instruction: str) -> None:
    job = _revise_jobs[job_id]
    try:
        bible = load_bible(slug)
        ch = require_chapter(bible, chapter_num)
        text = current_chapter_text(ch)
        if not text:
            raise ValueError("Chapter has no draft yet")

        job["phase"] = "researching"
        entry = bible.outline_entry(chapter_num) or {}
        synthetic_entry = {"title": entry.get("title", ""), "outline": instruction}
        proposals = researcher.suggest_for_chapter(bible, chapter_num, synthetic_entry)
        research_notes = _assign_note_ids(bible, proposals) if proposals else None
        job["research_proposals"] = research_notes or []

        job["phase"] = "revising"
        new_text = reviser.revise(
            f"chapter {chapter_num} of the novel", text, instruction,
            on_delta=lambda partial: job.__setitem__("partial_text", partial),
            research_notes=research_notes,
        )
        if entry.get("title"):
            bible.upsert_chapter(chapter_num, title=entry["title"])
        revision = bible.add_chapter_revision(chapter_num, new_text, source="instruction", instruction=instruction)
        ch = bible.get_chapter(chapter_num)
        compacted_count = maybe_compact_history(bible, ch["history"], f"Chapter {chapter_num}")
        job["result"] = {**revision, "diff": word_diff(text, new_text), "compacted_count": compacted_count}
    except Exception as exc:
        job["error"] = str(exc)
    finally:
        job["done"] = True


@router.post("/api/projects/{slug}/chapters/{chapter_num}/revise")
def revise_chapter(slug: str, chapter_num: int, req: InstructionRequest) -> dict[str, Any]:
    """Same streaming-job pattern as draft_chapter - a chapter-length revise
    is the other single call long enough to want live progress."""
    bible = load_bible(slug)
    ch = require_chapter(bible, chapter_num)
    if not current_chapter_text(ch):
        raise HTTPException(400, "Chapter has no draft yet")

    job_id = uuid.uuid4().hex
    _revise_jobs[job_id] = {
        "done": False, "error": None, "result": None, "partial_text": "",
        "phase": "revising", "research_proposals": [],
    }
    threading.Thread(
        target=_run_revise_job, args=(job_id, slug, chapter_num, req.instruction), daemon=True
    ).start()
    return {"job_id": job_id}


@router.get("/api/projects/{slug}/chapters/{chapter_num}/revise/status/{job_id}")
def revise_chapter_status(slug: str, chapter_num: int, job_id: str) -> dict[str, Any]:
    job = _revise_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job


class ManualEditRequest(BaseModel):
    text: str


@router.post("/api/projects/{slug}/chapters/{chapter_num}/edit")
def edit_chapter(slug: str, chapter_num: int, req: ManualEditRequest) -> dict[str, Any]:
    """Records a hand-typed edit directly, without going through the LLM
    reviser - for small fixes the writer would rather just type than explain
    as an instruction. Also how a chapter gets its very first revision if the
    writer would rather hand-write it than have the AI draft it - the chapter
    record doesn't need to exist yet, only its outline entry does."""
    bible = load_bible(slug)
    ch = bible.get_chapter(chapter_num)
    entry = bible.outline_entry(chapter_num)
    if ch is None and entry is None:
        raise HTTPException(404, f"No outline entry for chapter {chapter_num}")
    old_text = current_chapter_text(ch) if ch else ""
    if entry and entry.get("title"):
        bible.upsert_chapter(chapter_num, title=entry["title"])
    revision = bible.add_chapter_revision(chapter_num, req.text, source="manual")
    ch = bible.get_chapter(chapter_num)
    compacted_count = maybe_compact_history(bible, ch["history"], f"Chapter {chapter_num}")
    return {**revision, "diff": word_diff(old_text, req.text), "compacted_count": compacted_count}


@router.delete("/api/projects/{slug}/chapters/{chapter_num}")
def delete_chapter(slug: str, chapter_num: int) -> dict[str, Any]:
    """Removes a drafted/revised chapter's text and history entirely - the
    outline entry (if any) is untouched, so the chapter slot stays available
    to redraft. Distinct from DELETE /outline/{chapter_num}, which removes
    the outline entry but leaves any drafted text behind."""
    bible = load_bible(slug)
    require_chapter(bible, chapter_num)
    bible.delete_chapter(chapter_num)
    return {"deleted": chapter_num}


_FINALIZE_STEPS = [
    ("revise", "Revising text"),
    ("continuity_check", "Checking continuity"),
    ("voice_check", "Checking voice"),
    ("fix_continuity", "Fixing continuity issues"),
    ("copyedit", "Copyediting"),
    ("finalize_summarize", "Finalizing & summarizing"),
    ("bible_sync", "Updating character/faction/world bible"),
    ("outline_sync", "Syncing outline"),
]

# In-memory job store for the finalize pipeline's progress bar. Single-user
# local app, so no persistence/expiry needed - jobs just accumulate for the
# life of the server process.
_finalize_jobs: dict[str, dict[str, Any]] = {}


def _guard_rewrite(original: str, rewritten: str, step: str) -> str:
    """Rejects a rewrite step's output if it's implausibly short compared to
    the input - the local model occasionally echoes back only the prompt's
    delimiter markers (or truncates early) instead of actual prose, and
    without this check that garbage silently becomes the chapter's new
    current text via add_chapter_revision, clobbering the real draft."""
    orig_words = len(original.split())
    new_words = len(rewritten.split())
    if orig_words >= 20 and new_words < orig_words * 0.4:
        raise AIOutputError(
            f"{step} returned {new_words} words for a {orig_words}-word chapter - "
            "looks truncated/degenerate, not applying it"
        )
    return rewritten


def _run_finalize_job(job_id: str, slug: str, chapter_num: int, history_id: int) -> None:
    job = _finalize_jobs[job_id]

    def advance(step_key: str) -> None:
        idx = next(i for i, (key, _) in enumerate(_FINALIZE_STEPS) if key == step_key)
        job["step_index"] = idx
        job["label"] = _FINALIZE_STEPS[idx][1]

    try:
        bible = load_bible(slug)
        ch = require_chapter(bible, chapter_num)
        history = ch.get("history") or []
        text = history[history_id]["text"]

        advance("revise")
        edited = _guard_rewrite(text, editor.revise(bible, chapter_num, text), "Editor revise")

        advance("continuity_check")
        issues = editor.check_continuity(bible, chapter_num, edited)

        advance("voice_check")
        issues += voice_checker.check(bible, chapter_num, edited)

        advance("fix_continuity")
        polished = edited
        if issues:
            polished = _guard_rewrite(
                edited, editor.fix_continuity(bible, chapter_num, edited, issues), "Continuity fix"
            )

        advance("copyedit")
        polished = _guard_rewrite(polished, copy_editor.copyedit(bible, chapter_num, polished), "Copyedit")
        if polished != text:
            with bible.batch_save():
                bible.add_chapter_revision(chapter_num, polished, source="finalize_polish")
                ch = bible.get_chapter(chapter_num)
                maybe_compact_history(bible, ch["history"], f"Chapter {chapter_num}")

        advance("finalize_summarize")
        summary = editor.summarize(bible, chapter_num, polished)
        with bible.batch_save():
            bible.approve_chapter(chapter_num, polished)
            bible.upsert_chapter(chapter_num, summary=summary, word_count=len(polished.split()), continuity_issues=issues)

        advance("bible_sync")
        job["bible_proposals"] = bible_manager.propose_from_chapter(bible, chapter_num, polished)
        job["timeline_proposals"] = timeline_extractor.propose_from_chapter(bible, chapter_num, polished)

        advance("outline_sync")
        entry = bible.outline_entry(chapter_num)
        if entry is not None:
            entry = outliner.regenerate_chapter(bible, chapter_num, final_text=polished)
            if entry.get("title"):
                bible.upsert_chapter(chapter_num, title=entry["title"])

        job["step_index"] = len(_FINALIZE_STEPS)
        job["result"] = bible.get_chapter(chapter_num)
    except Exception as exc:
        job["error"] = str(exc)
    finally:
        job["done"] = True


@router.post("/api/projects/{slug}/chapters/{chapter_num}/approve")
def start_approve_chapter(slug: str, chapter_num: int, req: ApproveRequest) -> dict[str, Any]:
    """Kicks off the finalize pipeline (editor -> continuity/voice check ->
    copyedit -> approve -> bible sync proposals -> outline sync) in a
    background thread and returns a job_id immediately, so the UI can poll
    for progress instead of blocking on one long request - this chain now
    runs 8 serial LLM calls and can take minutes with reasoning mode on.
    Bible sync only proposes character/faction/world updates here; nothing
    is written to the bible until the writer reviews and approves each one
    via the returned job's "bible_proposals" list."""
    bible = load_bible(slug)
    ch = require_chapter(bible, chapter_num)
    history = ch.get("history") or []
    if not (0 <= req.history_id < len(history)):
        raise HTTPException(404, "No such revision")

    job_id = uuid.uuid4().hex
    _finalize_jobs[job_id] = {
        "done": False,
        "error": None,
        "result": None,
        "bible_proposals": [],
        "timeline_proposals": [],
        "step_index": 0,
        "total_steps": len(_FINALIZE_STEPS),
        "label": _FINALIZE_STEPS[0][1],
    }
    threading.Thread(
        target=_run_finalize_job, args=(job_id, slug, chapter_num, req.history_id), daemon=True
    ).start()
    return {"job_id": job_id}


@router.get("/api/projects/{slug}/chapters/{chapter_num}/approve/status/{job_id}")
def approve_chapter_status(slug: str, chapter_num: int, job_id: str) -> dict[str, Any]:
    job = _finalize_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job


@router.post("/api/projects/{slug}/chapters/{chapter_num}/bible-sync")
def sync_chapter_bible(slug: str, chapter_num: int) -> dict[str, Any]:
    """Re-runs just the bible/timeline proposal step (the last two of the
    finalize pipeline's 8 steps) against the chapter's current finalized
    text, without touching the prose itself - for recovering proposals that
    were lost to a failed/errored finalize job (e.g. the frontend used to
    discard bible_proposals/timeline_proposals whenever a later pipeline
    step, like outline sync, failed). As with the finalize pipeline, nothing
    is written to the bible until the writer reviews and approves each
    proposal via the returned lists."""
    bible = load_bible(slug)
    ch = require_chapter(bible, chapter_num)
    text = current_chapter_text(ch)
    if not text:
        raise HTTPException(404, "Chapter has no finalized text yet")
    return {
        "bible_proposals": bible_manager.propose_from_chapter(bible, chapter_num, text),
        "timeline_proposals": timeline_extractor.propose_from_chapter(bible, chapter_num, text),
    }


@router.post("/api/projects/{slug}/chapters/{chapter_num}/check-continuity")
def check_continuity_chapter(slug: str, chapter_num: int) -> dict[str, Any]:
    """Re-checks an already-approved chapter against the CURRENT bible (which may
    have changed since this chapter was finalized, e.g. an earlier chapter was
    edited to make a permanent change). Does not rewrite anything - surfaces
    issues for the user to fix via a normal instruction revise, then clears the
    needs_recheck flag since it's now been reviewed."""
    bible = load_bible(slug)
    ch = require_chapter(bible, chapter_num)
    text = ch.get("final") or current_chapter_text(ch)
    if not text:
        raise HTTPException(400, "Chapter has no text yet")

    issues = editor.check_continuity(bible, chapter_num, text)
    issues += voice_checker.check(bible, chapter_num, text)
    bible.upsert_chapter(chapter_num, needs_recheck=False, continuity_issues=issues)
    return {"issues": issues}


@router.post("/api/projects/{slug}/chapters/{chapter_num}/fact-check")
def fact_check_chapter(slug: str, chapter_num: int) -> dict[str, Any]:
    """Post-hoc real-world plausibility pass over a finalized chapter's prose -
    distinct from check-continuity above, which only checks internal bible
    consistency. Manually triggered (costs an LLM call + web research), and a
    no-op unless the project is nonfiction or flagged real_world_setting.
    Findings are persisted as continuity_flags (kind "chapter") so they show
    up in the same Continuity view/review queue as internal-consistency
    flags, rather than a separate mechanism."""
    bible = load_bible(slug)
    ch = require_chapter(bible, chapter_num)
    text = ch.get("final") or current_chapter_text(ch)
    if not text:
        raise HTTPException(400, "Chapter has no text yet")

    try:
        flags = fact_checker.check_chapter(bible, chapter_num, text)
    except AIOutputError as exc:
        logger.exception("Fact check failed for project %r chapter %s", slug, chapter_num)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")
    bible.add_continuity_flags(flags)
    return {"flags": flags}


@router.post("/api/projects/{slug}/chapters/{chapter_num}/critique")
def critique_chapter(slug: str, chapter_num: int) -> dict[str, Any]:
    """Developmental-editing pass over one chapter (pacing/stakes/craft) -
    distinct from check-continuity (correctness) and fact-check (real-world
    accuracy). Works on any chapter with drafted text, not just finalized
    ones (partial drafts included by design), and persists findings as
    critique_flags so they show up in their own Critique view/queue rather
    than mixing with continuity_flags."""
    bible = load_bible(slug)
    ch = require_chapter(bible, chapter_num)
    text = ch.get("final") or current_chapter_text(ch)
    if not text:
        raise HTTPException(400, "Chapter has no text yet")

    try:
        flags: list[dict[str, Any]] = []
        for checker in CRITIQUE_CHECKERS:
            flags += checker.check_chapter(bible, chapter_num, text)
    except AIOutputError as exc:
        logger.exception("Critique failed for project %r chapter %s", slug, chapter_num)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")
    bible.add_critique_flags(flags)
    return {"flags": flags}


class TranslateChapterRequest(BaseModel):
    language: str


@router.post("/api/projects/{slug}/chapters/{chapter_num}/translate")
def translate_chapter(slug: str, chapter_num: int, body: TranslateChapterRequest) -> dict[str, Any]:
    """Single-chapter final-pass translation - the one-at-a-time counterpart to
    the whole-book translate-book job below. Requires the chapter to be
    finalized (translation runs on the settled English text, not a draft in
    progress), and reuses/extends the same per-language glossary so names
    stay consistent whether the book is translated chapter-by-chapter or in
    one batch."""
    if body.language not in translators:
        raise HTTPException(400, f"Unknown language {body.language!r}")
    bible = load_bible(slug)
    ch = require_chapter(bible, chapter_num)
    text = ch.get("final")
    if not text:
        raise HTTPException(400, "Chapter isn't finalized yet - translation runs on finalized text")

    translator = translators[body.language]
    names = sorted(
        {c["name"] for c in bible.data.get("characters", [])} | {w["name"] for w in bible.data.get("world", [])}
    )
    glossary = bible.get_translation_glossary(body.language)
    missing_names = [n for n in names if n not in glossary]
    try:
        if missing_names:
            glossary.update(translator.translate_glossary(missing_names))
            bible.set_translation_glossary(body.language, glossary)
        translated = translator.translate(bible, chapter_num, text, glossary)
    except AIOutputError as exc:
        logger.exception("Translation failed for project %r chapter %s (%s)", slug, chapter_num, body.language)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")
    bible.set_chapter_translation(chapter_num, body.language, translated)
    return {"translated": True}


def _run_critique_book_job(job_id: str, slug: str) -> None:
    job = _critique_book_jobs[job_id]
    try:
        bible = load_bible(slug)
        chapters = [
            ch for ch in bible.data.get("chapters", [])
            if (ch.get("final") or current_chapter_text(ch))
        ]
        chapters.sort(key=lambda ch: ch["chapter_num"])
        # total_steps counts one step per chapter plus the final rollup pass,
        # matching pollFinalizeJob's step_index/total_steps/label shape on
        # the frontend so it can reuse the same determinate progress bar.
        job["total_steps"] = len(chapters) + 1

        all_flags: list[dict[str, Any]] = []
        chapter_reports: list[dict[str, Any]] = []
        for i, ch in enumerate(chapters):
            chapter_num = ch["chapter_num"]
            job["step_index"] = i
            job["label"] = f"Critiquing chapter {chapter_num}"
            text = ch.get("final") or current_chapter_text(ch)
            findings: list[dict[str, Any]] = []
            for checker in CRITIQUE_CHECKERS:
                findings += checker.check_chapter(bible, chapter_num, text)
            all_flags += findings
            summary = editor.summarize(bible, chapter_num, text)
            chapter_reports.append({"chapter_num": chapter_num, "summary": summary, "findings": findings})

        job["step_index"] = len(chapters)
        job["label"] = "Synthesizing book-level findings"
        all_flags += book_critique.critique_book(bible, chapter_reports)

        bible.add_critique_flags(all_flags)
        job["step_index"] = job["total_steps"]
        job["result"] = {"flags": all_flags}
    except Exception as exc:
        job["error"] = str(exc)
    finally:
        job["done"] = True


@router.post("/api/projects/{slug}/critique-book")
def critique_book(slug: str) -> dict[str, Any]:
    """Whole-book critique: runs the narrow checkers chapter by chapter (kept
    small enough for a local LLM's context), then a book-level rollup pass
    over just those chapters' summaries/findings - never the full manuscript
    text at once. Runs as a background job since it's many chapters' worth
    of LLM calls, same job_id/poll pattern as draft/revise."""
    bible = load_bible(slug)
    if not any(ch.get("final") or current_chapter_text(ch) for ch in bible.data.get("chapters", [])):
        raise HTTPException(400, "No drafted chapters yet")

    job_id = uuid.uuid4().hex
    _critique_book_jobs[job_id] = {
        "done": False, "error": None, "result": None,
        "step_index": 0, "total_steps": 1, "label": "Starting...",
    }
    threading.Thread(target=_run_critique_book_job, args=(job_id, slug), daemon=True).start()
    return {"job_id": job_id}


@router.get("/api/projects/{slug}/critique-book/status/{job_id}")
def critique_book_status(slug: str, job_id: str) -> dict[str, Any]:
    job = _critique_book_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job


def _run_translate_book_job(job_id: str, slug: str, language: str, retranslate: bool) -> None:
    job = _translate_book_jobs[job_id]
    translator = translators[language]
    try:
        bible = load_bible(slug)
        chapters = [
            ch for ch in bible.data.get("chapters", [])
            if (ch.get("final") or current_chapter_text(ch))
        ]
        chapters.sort(key=lambda ch: ch["chapter_num"])
        if not retranslate:
            chapters = [ch for ch in chapters if not (ch.get("translations") or {}).get(language)]
        # total_steps counts one step per chapter plus the glossary pass,
        # matching pollFinalizeJob's step_index/total_steps/label shape on
        # the frontend so it can reuse the same determinate progress bar.
        job["total_steps"] = len(chapters) + 1

        job["label"] = "Building name glossary"
        names = sorted({c["name"] for c in bible.data.get("characters", [])} | {w["name"] for w in bible.data.get("world", [])})
        glossary = bible.get_translation_glossary(language)
        missing_names = [n for n in names if n not in glossary]
        if missing_names:
            glossary.update(translator.translate_glossary(missing_names))
            bible.set_translation_glossary(language, glossary)
        job["step_index"] = 1

        translated_count = 0
        errors: list[str] = []
        for i, ch in enumerate(chapters):
            chapter_num = ch["chapter_num"]
            job["step_index"] = i + 1
            job["label"] = f"Translating chapter {chapter_num}"
            text = ch.get("final") or current_chapter_text(ch)
            try:
                translated = translator.translate(bible, chapter_num, text, glossary)
                bible.set_chapter_translation(chapter_num, language, translated)
                translated_count += 1
            except AIOutputError as exc:
                errors.append(f"Chapter {chapter_num}: {exc}")

        job["step_index"] = job["total_steps"]
        job["result"] = {"translated": translated_count, "skipped": len(errors), "errors": errors}
    except Exception as exc:
        job["error"] = str(exc)
    finally:
        job["done"] = True


class TranslateBookRequest(BaseModel):
    language: str
    retranslate: bool = False


@router.post("/api/projects/{slug}/translate-book")
def translate_book(slug: str, body: TranslateBookRequest) -> dict[str, Any]:
    """Whole-manuscript final translation pass: runs only after chapters are
    finalized in English, translating each into the target language with a
    shared name glossary for consistency. Saves each chapter's translation as
    soon as it's produced (not just at job end) so a cancel or crash partway
    through a long book doesn't discard already-completed chapters."""
    if body.language not in translators:
        raise HTTPException(400, f"Unknown language {body.language!r}")
    bible = load_bible(slug)
    if not any(ch.get("final") or current_chapter_text(ch) for ch in bible.data.get("chapters", [])):
        raise HTTPException(400, "No finalized chapters yet")

    job_id = uuid.uuid4().hex
    _translate_book_jobs[job_id] = {
        "done": False, "error": None, "result": None,
        "step_index": 0, "total_steps": 1, "label": "Starting...",
    }
    threading.Thread(
        target=_run_translate_book_job, args=(job_id, slug, body.language, body.retranslate), daemon=True
    ).start()
    return {"job_id": job_id}


@router.get("/api/projects/{slug}/translate-book/status/{job_id}")
def translate_book_status(slug: str, job_id: str) -> dict[str, Any]:
    job = _translate_book_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job


@router.post("/api/projects/{slug}/critique-flags/{flag_id}/resolve")
def resolve_critique_flag(slug: str, flag_id: int) -> dict[str, Any]:
    """Dismisses a persisted critique flag from the Critique view - mirrors
    resolve_continuity_flag in outline.py."""
    bible = load_bible(slug)
    bible.resolve_critique_flag(flag_id)
    return {"ok": True}
