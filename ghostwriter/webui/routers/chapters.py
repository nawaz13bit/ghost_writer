"""Chapter lifecycle: draft, revise (LLM or manual), diff, and the
approve/finalize pipeline (editor -> continuity/voice check -> copyedit ->
approve -> bible sync -> outline sync), which runs as a background job so
the UI can poll progress instead of blocking on ~8 serial LLM calls."""
from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ghostwriter.agents.base import AIOutputError
from ghostwriter.llm_client import LLMCancelled
from ghostwriter.memory.story_bible import slugify
from ghostwriter.webui.deps import (
    current_chapter_text, get_author, load_bible, maybe_compact_history, require_chapter, with_bible_lock,
)
from ghostwriter.webui.diffing import word_diff
from ghostwriter.webui.jobs import JobStore
from ghostwriter.webui.state import (
    bible_manager, book_critique, continuity_checker, copy_editor, craft_checker, editor, fact_checker, outliner,
    pacing_checker, researcher, reviser, stakes_checker, thread_planner, timeline_extractor, translators, voice_checker,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chapters"])


class InstructionRequest(BaseModel):
    instruction: str


class ApproveRequest(BaseModel):
    history_id: int


# In-memory job stores for the various long-running AI pipelines' live
# progress - see JobStore's docstring for why each feature gets its own
# store despite sharing the same lifecycle plumbing.
_draft_jobs = JobStore()
_revise_jobs = JobStore()
_critique_book_jobs = JobStore()
_book_consistency_jobs = JobStore()
_translate_book_jobs = JobStore()

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


@with_bible_lock
def _run_draft_job(job_id: str, slug: str, chapter_num: int) -> None:
    job = _draft_jobs.get(job_id)
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

    job_id = _draft_jobs.create({
        "partial_text": "", "phase": "drafting", "research_proposals": [],
    })
    threading.Thread(target=_run_draft_job, args=(job_id, slug, chapter_num), daemon=True).start()
    return {"job_id": job_id}


@router.get("/api/projects/{slug}/chapters/{chapter_num}/draft/status/{job_id}")
def draft_chapter_status(slug: str, chapter_num: int, job_id: str) -> dict[str, Any]:
    job = _draft_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job


@with_bible_lock
def _run_revise_job(job_id: str, slug: str, chapter_num: int, instruction: str) -> None:
    job = _revise_jobs.get(job_id)
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

    job_id = _revise_jobs.create({
        "partial_text": "", "phase": "revising", "research_proposals": [],
    })
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
@with_bible_lock
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
@with_bible_lock
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

# Job store for the finalize pipeline's progress bar.
_finalize_jobs = JobStore()


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


@with_bible_lock
def _run_finalize_job(job_id: str, slug: str, chapter_num: int, history_id: int) -> None:
    job = _finalize_jobs.get(job_id)

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

        # From here on, the chapter itself is already approved and saved above
        # (text, summary, word_count, continuity_issues) - these remaining
        # steps are derived syncs (bible/timeline/thread proposals, outline
        # regen). Each is independent of the others, so each gets its own
        # try/except: one failing (e.g. a malformed-JSON AIOutputError from
        # the bible proposal call) must not prevent the rest from running -
        # previously a single early failure here silently skipped timeline
        # extraction and outline sync too, with no recovery.
        sync_errors: list[str] = []

        advance("bible_sync")
        try:
            job["bible_proposals"] = bible_manager.propose_from_chapter(bible, chapter_num, polished)
        except Exception as exc:
            sync_errors.append(f"Bible sync: {exc}")
        try:
            job["timeline_proposals"] = timeline_extractor.propose_from_chapter(bible, chapter_num, polished)
        except Exception as exc:
            sync_errors.append(f"Timeline sync: {exc}")
        try:
            paradox_flags = continuity_checker.check_timeline_paradoxes(bible)
            if paradox_flags:
                bible.add_continuity_flags(paradox_flags)
        except Exception as exc:
            sync_errors.append(f"Timeline paradox check: {exc}")
        try:
            job["thread_proposals"] = thread_planner.propose_from_chapter(bible, chapter_num, polished)
        except Exception as exc:
            sync_errors.append(f"Thread sync: {exc}")
        try:
            job["resolve_proposals"] = thread_planner.propose_resolutions(bible, chapter_num, polished)
        except Exception as exc:
            sync_errors.append(f"Thread resolution sync: {exc}")

        advance("outline_sync")
        try:
            entry = bible.outline_entry(chapter_num)
            if entry is not None:
                entry = outliner.regenerate_chapter(bible, chapter_num, final_text=polished)
                if entry.get("title"):
                    bible.upsert_chapter(chapter_num, title=entry["title"])
        except Exception as exc:
            sync_errors.append(f"Outline sync: {exc}")

        job["step_index"] = len(_FINALIZE_STEPS)
        job["result"] = bible.get_chapter(chapter_num)
        if sync_errors:
            job["error"] = (
                "Chapter approved, but some follow-up syncs failed (use the chapter's "
                '"Re-sync bible/timeline updates" action to retry them): ' + "; ".join(sync_errors)
            )
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

    job_id = _finalize_jobs.create({
        "bible_proposals": [],
        "timeline_proposals": [],
        "thread_proposals": [],
        "resolve_proposals": [],
        "step_index": 0,
        "total_steps": len(_FINALIZE_STEPS),
        "label": _FINALIZE_STEPS[0][1],
    })
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


_BIBLE_SYNC_STEPS = [
    ("bible_sync", "Checking character/faction/world bible"),
    ("timeline_sync", "Checking timeline"),
    ("thread_sync", "Checking upcoming threads"),
    ("resolve_sync", "Checking paid-off threads"),
]

# Separate job store from _finalize_jobs - this recovery action can be run
# independently of (and concurrently with) a finalize pipeline job.
_bible_sync_jobs = JobStore()


def _run_bible_sync_job(job_id: str, slug: str, chapter_num: int) -> None:
    """Body of the old synchronous sync_chapter_bible endpoint, moved to a
    background thread. Each of the three proposal agents now batches its
    reference-data dump (see Agent.ask_json_batched) once a project's bible
    grows large, which can turn this from 3 LLM calls into a dozen+ - too
    long to hold open one HTTP request for, with no progress feedback in
    the meantime. Runs the same job-polling pattern as _run_finalize_job."""
    job = _bible_sync_jobs.get(job_id)

    def advance(step_key: str) -> None:
        idx = next(i for i, (key, _) in enumerate(_BIBLE_SYNC_STEPS) if key == step_key)
        job["step_index"] = idx
        job["label"] = _BIBLE_SYNC_STEPS[idx][1]

    try:
        bible = load_bible(slug)
        ch = require_chapter(bible, chapter_num)
        text = current_chapter_text(ch)
        if not text:
            raise ValueError("Chapter has no finalized text yet")

        errors: list[str] = []
        advance("bible_sync")
        try:
            job["bible_proposals"] = bible_manager.propose_from_chapter(bible, chapter_num, text)
        except Exception as exc:
            logger.exception("Bible sync failed for project %r chapter %r", slug, chapter_num)
            errors.append(f"Bible sync: {exc}")
        advance("timeline_sync")
        try:
            job["timeline_proposals"] = timeline_extractor.propose_from_chapter(bible, chapter_num, text)
        except Exception as exc:
            logger.exception("Timeline sync failed for project %r chapter %r", slug, chapter_num)
            errors.append(f"Timeline sync: {exc}")
        try:
            paradox_flags = continuity_checker.check_timeline_paradoxes(bible)
            if paradox_flags:
                bible.add_continuity_flags(paradox_flags)
        except Exception as exc:
            logger.exception("Timeline paradox check failed for project %r chapter %r", slug, chapter_num)
            errors.append(f"Timeline paradox check: {exc}")
        advance("thread_sync")
        try:
            job["thread_proposals"] = thread_planner.propose_from_chapter(bible, chapter_num, text)
        except Exception as exc:
            logger.exception("Thread sync failed for project %r chapter %r", slug, chapter_num)
            errors.append(f"Thread sync: {exc}")
        advance("resolve_sync")
        try:
            job["resolve_proposals"] = thread_planner.propose_resolutions(bible, chapter_num, text)
        except Exception as exc:
            logger.exception("Thread resolution sync failed for project %r chapter %r", slug, chapter_num)
            errors.append(f"Thread resolution sync: {exc}")

        job["step_index"] = len(_BIBLE_SYNC_STEPS)
        if errors:
            job["error"] = "Some updates failed and can be retried by running this again: " + "; ".join(errors)
    except Exception as exc:
        job["error"] = str(exc)
    finally:
        job["done"] = True


@router.post("/api/projects/{slug}/chapters/{chapter_num}/bible-sync")
def sync_chapter_bible(slug: str, chapter_num: int) -> dict[str, Any]:
    """Kicks off the bible/timeline/thread proposal recovery pass (the last
    steps of the finalize pipeline) in a background thread and returns a
    job_id immediately, so the UI can poll for progress instead of blocking
    on what can now be a dozen+ serial LLM calls on a long-running project -
    for recovering proposals that were lost to a failed/errored finalize job
    (e.g. the frontend used to discard bible_proposals/timeline_proposals
    whenever a later pipeline step, like outline sync, failed). As with the
    finalize pipeline, nothing is written to the bible until the writer
    reviews and approves each proposal via the job's returned lists."""
    bible = load_bible(slug)
    require_chapter(bible, chapter_num)

    job_id = _bible_sync_jobs.create({
        "bible_proposals": [],
        "timeline_proposals": [],
        "thread_proposals": [],
        "resolve_proposals": [],
        "step_index": 0,
        "total_steps": len(_BIBLE_SYNC_STEPS),
        "label": _BIBLE_SYNC_STEPS[0][1],
    })
    threading.Thread(target=_run_bible_sync_job, args=(job_id, slug, chapter_num), daemon=True).start()
    return {"job_id": job_id}


@router.get("/api/projects/{slug}/chapters/{chapter_num}/bible-sync/status/{job_id}")
def bible_sync_status(slug: str, chapter_num: int, job_id: str) -> dict[str, Any]:
    job = _bible_sync_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job


@router.post("/api/projects/{slug}/chapters/{chapter_num}/check-continuity")
@with_bible_lock
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
    bible.upsert_chapter(chapter_num, needs_recheck=False, needs_recheck_from=[], continuity_issues=issues)
    return {"issues": issues}


@router.post("/api/projects/{slug}/chapters/{chapter_num}/fact-check")
@with_bible_lock
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
@with_bible_lock
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
@with_bible_lock
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


@with_bible_lock
def _run_critique_book_job(job_id: str, slug: str) -> None:
    job = _critique_book_jobs.get(job_id)
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

    job_id = _critique_book_jobs.create({"step_index": 0, "total_steps": 1, "label": "Starting..."})
    threading.Thread(target=_run_critique_book_job, args=(job_id, slug), daemon=True).start()
    return {"job_id": job_id}


@router.get("/api/projects/{slug}/critique-book/status/{job_id}")
def critique_book_status(slug: str, job_id: str) -> dict[str, Any]:
    job = _critique_book_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job


@with_bible_lock
def _run_book_consistency_check_job(job_id: str, slug: str) -> None:
    job = _book_consistency_jobs.get(job_id)
    try:
        bible = load_bible(slug)
        chapters = [
            ch for ch in bible.data.get("chapters", [])
            if (ch.get("final") or current_chapter_text(ch))
        ]
        chapters.sort(key=lambda ch: ch["chapter_num"])
        # total_steps counts one step per chapter for the continuity sweep,
        # one step per chapter again for the voice-check pass, plus the final
        # save pass - same determinate-progress shape as _run_critique_book_job.
        job["total_steps"] = len(chapters) * 2 + 1
        job["step_index"] = 0
        job["label"] = "Sweeping chapters in book order"

        chapter_payload = [
            {"chapter_num": ch["chapter_num"], "text": ch.get("final") or current_chapter_text(ch)}
            for ch in chapters
        ]

        def _on_batch(chapters_done: int, total_chapters: int) -> None:
            job["step_index"] = chapters_done
            job["label"] = "Sweeping chapters in book order"

        def _on_voice_progress(chapters_done: int, total_chapters: int) -> None:
            job["step_index"] = len(chapters) + chapters_done
            job["label"] = "Checking narrative voice"

        flags = continuity_checker.check_book(
            bible, chapter_payload, on_batch=_on_batch,
            voice_checker=voice_checker, on_voice_progress=_on_voice_progress,
        )

        job["step_index"] = len(chapters) * 2
        job["label"] = "Saving flags"
        bible.add_continuity_flags(flags)
        job["step_index"] = job["total_steps"]
        job["result"] = {"flags": flags}
    except Exception as exc:
        job["error"] = str(exc)
    finally:
        job["done"] = True


@router.post("/api/projects/{slug}/book-consistency-check")
def book_consistency_check(slug: str) -> dict[str, Any]:
    """Whole-book continuity/consistency sweep: walks every drafted/finalized
    chapter in book order against a rolling story-so-far summary AND the
    characters/world/timeline briefs, distinct from consistency-check (scoped
    to the fallout of one described change) and critique-book (craft/pacing,
    not continuity). Runs as a background job, same job_id/poll pattern as
    critique-book, since it's many chapters' worth of LLM calls."""
    bible = load_bible(slug)
    if not any(ch.get("final") or current_chapter_text(ch) for ch in bible.data.get("chapters", [])):
        raise HTTPException(400, "No drafted chapters yet")

    job_id = _book_consistency_jobs.create({"step_index": 0, "total_steps": 1, "label": "Starting..."})
    threading.Thread(target=_run_book_consistency_check_job, args=(job_id, slug), daemon=True).start()
    return {"job_id": job_id}


@router.get("/api/projects/{slug}/book-consistency-check/status/{job_id}")
def book_consistency_check_status(slug: str, job_id: str) -> dict[str, Any]:
    job = _book_consistency_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job


@with_bible_lock
def _run_translate_book_job(job_id: str, slug: str, language: str, retranslate: bool) -> None:
    job = _translate_book_jobs.get(job_id)
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

    job_id = _translate_book_jobs.create({"step_index": 0, "total_steps": 1, "label": "Starting..."})
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
