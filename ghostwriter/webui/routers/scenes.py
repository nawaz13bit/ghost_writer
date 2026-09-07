"""Scene-level planning: breaking a chapter's outline into scene cards
(beats/pov/emotional_state/transition), drafting each individually, and
stitching them into one continuous chapter draft. Scenes are an internal
planning/generation unit only - never a labeled section in the manuscript.

Entirely optional and additive per chapter: a chapter with no scenes drafts
exactly as it always has via chapters.py. This mirrors chapters.py's
draft/revise background-job pattern so the UI can reuse the same polling
logic for both."""
from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ghostwriter.agents.author import stitch_scenes
from ghostwriter.agents.base import AIOutputError
from ghostwriter.llm_client import LLMCancelled
from ghostwriter.webui.deps import get_author, load_bible, maybe_compact_history, with_bible_lock
from ghostwriter.webui.diffing import word_diff
from ghostwriter.webui.jobs import JobStore
from ghostwriter.webui.routers.chapters import InstructionRequest, _guard_rewrite
from ghostwriter.webui.state import outliner, reviser

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scenes"])


class PlanScenesRequest(BaseModel):
    instruction: str | None = None


@router.post("/api/projects/{slug}/outline/{chapter_num}/scenes/plan")
def plan_scenes(slug: str, chapter_num: int, req: PlanScenesRequest | None = None) -> list[dict[str, Any]]:
    """AI-generates scene cards for a chapter. Returns a proposal - not
    saved until the writer reviews/edits and calls /apply, same pattern as
    suggest_outline_entry/revise_outline."""
    bible = load_bible(slug)
    if bible.outline_entry(chapter_num) is None:
        raise HTTPException(404, f"No outline entry for chapter {chapter_num}")
    instruction = req.instruction.strip() if req and req.instruction and req.instruction.strip() else None
    try:
        return outliner.plan_scenes(bible, chapter_num, extra_instruction=instruction)
    except AIOutputError as exc:
        logger.exception("Scene planning failed for project %r chapter %s", slug, chapter_num)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")
    except ValueError as exc:
        raise HTTPException(404, str(exc))


class ApplyScenesRequest(BaseModel):
    scenes: list[dict[str, Any]]


@router.post("/api/projects/{slug}/outline/{chapter_num}/scenes/apply")
@with_bible_lock
def apply_scenes(slug: str, chapter_num: int, req: ApplyScenesRequest) -> list[dict[str, Any]]:
    """Persists a planned/edited scene list, replacing any existing scenes
    for this chapter. Refuses to drop a scene that already has a draft, so
    an AI re-plan can't silently discard drafted prose."""
    bible = load_bible(slug)
    entry = bible.outline_entry(chapter_num)
    if entry is None:
        raise HTTPException(404, f"No outline entry for chapter {chapter_num}")
    existing_drafted = {
        s["scene_num"] for s in (entry.get("scenes") or []) if s.get("draft")
    }
    if existing_drafted:
        raise HTTPException(
            400,
            f"Chapter {chapter_num} already has drafted scene(s) {sorted(existing_drafted)} - "
            "delete or redraft them individually instead of re-applying a full plan.",
        )
    entry["scenes"] = []
    bible.save()
    for scene in req.scenes:
        bible.add_scene(
            chapter_num,
            beats=scene.get("beats", ""),
            pov=scene.get("pov"),
            emotional_state=scene.get("emotional_state", ""),
            transition=scene.get("transition", "continuous"),
            location=scene.get("location"),
        )
    return bible.outline_entry(chapter_num)["scenes"]


class SceneEditRequest(BaseModel):
    beats: str | None = None
    pov: str | None = None
    emotional_state: str | None = None
    transition: str | None = None
    location: str | None = None


@router.post("/api/projects/{slug}/outline/{chapter_num}/scenes/{scene_num}/edit")
@with_bible_lock
def edit_scene(slug: str, chapter_num: int, scene_num: int, req: SceneEditRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        return bible.update_scene(chapter_num, scene_num, **fields)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


_scene_draft_jobs = JobStore()
_scene_revise_jobs = JobStore()


@with_bible_lock
def _run_scene_draft_job(job_id: str, slug: str, chapter_num: int, scene_num: int) -> None:
    job = _scene_draft_jobs.get(job_id)
    try:
        bible = load_bible(slug)
        if bible.get_scene(chapter_num, scene_num) is None:
            raise ValueError(f"No scene {scene_num} for chapter {chapter_num}")
        author = get_author(bible)
        target_words = max(300, bible.data.get("chapter_target_words", 1800) // 3)
        draft = author.draft_scene(
            bible, chapter_num, scene_num, target_words=target_words,
            on_delta=lambda text: job.__setitem__("partial_text", text),
        )
        revision = bible.add_scene_revision(chapter_num, scene_num, draft, source="draft")
        bible.update_scene(chapter_num, scene_num, draft=draft)
        scene = bible.get_scene(chapter_num, scene_num)
        compacted_count = maybe_compact_history(bible, scene["history"], f"Chapter {chapter_num} scene {scene_num}")
        job["result"] = {**revision, "compacted_count": compacted_count}
    except Exception as exc:
        job["error"] = str(exc)
    finally:
        job["done"] = True


@router.post("/api/projects/{slug}/outline/{chapter_num}/scenes/{scene_num}/draft")
def draft_scene(slug: str, chapter_num: int, scene_num: int) -> dict[str, Any]:
    bible = load_bible(slug)
    if bible.get_scene(chapter_num, scene_num) is None:
        raise HTTPException(404, f"No scene {scene_num} for chapter {chapter_num}")

    job_id = _scene_draft_jobs.create({"partial_text": ""})
    threading.Thread(
        target=_run_scene_draft_job, args=(job_id, slug, chapter_num, scene_num), daemon=True
    ).start()
    return {"job_id": job_id}


@router.get("/api/projects/{slug}/outline/{chapter_num}/scenes/{scene_num}/draft/status/{job_id}")
def draft_scene_status(slug: str, chapter_num: int, scene_num: int, job_id: str) -> dict[str, Any]:
    job = _scene_draft_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job


@with_bible_lock
def _run_scene_revise_job(job_id: str, slug: str, chapter_num: int, scene_num: int, instruction: str) -> None:
    job = _scene_revise_jobs.get(job_id)
    try:
        bible = load_bible(slug)
        scene = bible.get_scene(chapter_num, scene_num)
        if scene is None:
            raise ValueError(f"No scene {scene_num} for chapter {chapter_num}")
        text = scene.get("draft") or ""
        if not text:
            raise ValueError("Scene has no draft yet")
        new_text = reviser.revise(
            f"scene {scene_num} of chapter {chapter_num} of the novel", text, instruction,
            on_delta=lambda partial: job.__setitem__("partial_text", partial),
        )
        new_text = _guard_rewrite(text, new_text, "Scene revise")
        revision = bible.add_scene_revision(chapter_num, scene_num, new_text, source="instruction", instruction=instruction)
        bible.update_scene(chapter_num, scene_num, draft=new_text)
        scene = bible.get_scene(chapter_num, scene_num)
        compacted_count = maybe_compact_history(bible, scene["history"], f"Chapter {chapter_num} scene {scene_num}")
        job["result"] = {**revision, "diff": word_diff(text, new_text), "compacted_count": compacted_count}
    except Exception as exc:
        job["error"] = str(exc)
    finally:
        job["done"] = True


@router.post("/api/projects/{slug}/outline/{chapter_num}/scenes/{scene_num}/revise")
def revise_scene(slug: str, chapter_num: int, scene_num: int, req: InstructionRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    scene = bible.get_scene(chapter_num, scene_num)
    if scene is None:
        raise HTTPException(404, f"No scene {scene_num} for chapter {chapter_num}")
    if not scene.get("draft"):
        raise HTTPException(400, "Scene has no draft yet")

    job_id = _scene_revise_jobs.create({"partial_text": ""})
    threading.Thread(
        target=_run_scene_revise_job, args=(job_id, slug, chapter_num, scene_num, req.instruction), daemon=True
    ).start()
    return {"job_id": job_id}


@router.get("/api/projects/{slug}/outline/{chapter_num}/scenes/{scene_num}/revise/status/{job_id}")
def revise_scene_status(slug: str, chapter_num: int, scene_num: int, job_id: str) -> dict[str, Any]:
    job = _scene_revise_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job


@router.post("/api/projects/{slug}/outline/{chapter_num}/scenes/{scene_num}/approve")
@with_bible_lock
def approve_scene(slug: str, chapter_num: int, scene_num: int) -> dict[str, Any]:
    bible = load_bible(slug)
    try:
        return bible.approve_scene(chapter_num, scene_num)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.delete("/api/projects/{slug}/outline/{chapter_num}/scenes/{scene_num}")
@with_bible_lock
def delete_scene(slug: str, chapter_num: int, scene_num: int) -> dict[str, Any]:
    bible = load_bible(slug)
    try:
        bible.delete_scene(chapter_num, scene_num)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"deleted": scene_num}


@router.post("/api/projects/{slug}/outline/{chapter_num}/scenes/stitch")
@with_bible_lock
def stitch_chapter_from_scenes(slug: str, chapter_num: int) -> dict[str, Any]:
    """Concatenates the chapter's drafted scenes into one chapter draft and
    saves it as a new chapter revision - the "Draft from Scenes" action.
    Requires every scene to have a draft, so a half-drafted chapter can't be
    silently stitched with gaps."""
    bible = load_bible(slug)
    entry = bible.outline_entry(chapter_num)
    if entry is None:
        raise HTTPException(404, f"No outline entry for chapter {chapter_num}")
    scenes = entry.get("scenes") or []
    if not scenes:
        raise HTTPException(400, f"Chapter {chapter_num} has no planned scenes")
    undrafted = [s["scene_num"] for s in scenes if not s.get("draft")]
    if undrafted:
        raise HTTPException(400, f"Scene(s) {sorted(undrafted)} have no draft yet")

    stitched = stitch_scenes(bible, chapter_num)
    bible.upsert_chapter(chapter_num, title=entry["title"])
    revision = bible.add_chapter_revision(chapter_num, stitched, source="draft")
    ch = bible.get_chapter(chapter_num)
    compacted_count = maybe_compact_history(bible, ch["history"], f"Chapter {chapter_num}")
    return {**revision, "compacted_count": compacted_count}
