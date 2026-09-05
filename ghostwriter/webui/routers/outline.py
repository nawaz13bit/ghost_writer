"""Outline CRUD, AI suggest/regenerate/whole-outline-revise, and the
cross-section consistency checker that flags consequences of a change
elsewhere in the bible."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ghostwriter.agents.base import AIOutputError
from ghostwriter.llm_client import LLMCancelled
from ghostwriter.webui.deps import load_bible
from ghostwriter.webui.state import continuity_checker, outliner

logger = logging.getLogger(__name__)

router = APIRouter(tags=["outline"])


class NewOutlineRequest(BaseModel):
    chapter_num: int
    title: str
    summary: str = ""
    act: str | None = None
    outline: str = ""
    characters: list[str] | None = None


class OutlineEditRequest(BaseModel):
    title: str | None = None
    summary: str | None = None
    act: str | None = None
    outline: str | None = None
    characters: list[str] | None = None


@router.post("/api/projects/{slug}/outline")
def create_outline_entry(slug: str, req: NewOutlineRequest) -> dict[str, Any]:
    """Manually adds an outline entry. If its act name is new, this also
    implicitly creates that act (acts can also be created standalone, with no
    chapters yet, via POST /acts/{name})."""
    bible = load_bible(slug)
    try:
        return bible.add_outline_entry(
            req.chapter_num, req.title, req.summary, req.act, req.outline, req.characters
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class SuggestOutlineEntryRequest(BaseModel):
    prompt: str
    chapter_num: int


@router.post("/api/projects/{slug}/outline/suggest")
def suggest_outline_entry(slug: str, req: SuggestOutlineEntryRequest) -> dict[str, Any]:
    if not req.prompt.strip():
        raise HTTPException(400, "A description is required")
    bible = load_bible(slug)
    try:
        return outliner.suggest_new_entry(bible, req.prompt, req.chapter_num)
    except AIOutputError as exc:
        logger.exception("Outline suggest failed for project %r", slug)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again or rephrase the description.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")


@router.post("/api/projects/{slug}/outline/{chapter_num}/edit")
def edit_outline_entry(slug: str, chapter_num: int, req: OutlineEditRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        entry = bible.update_outline_entry(chapter_num, **fields)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    if "title" in fields and bible.get_chapter(chapter_num) is not None:
        bible.upsert_chapter(chapter_num, title=entry["title"])
    return entry


class RegenerateOutlineEntryRequest(BaseModel):
    instruction: str | None = None


@router.post("/api/projects/{slug}/outline/{chapter_num}/regenerate")
def regenerate_outline_entry(slug: str, chapter_num: int, req: RegenerateOutlineEntryRequest | None = None) -> dict[str, Any]:
    """Regenerates a single outline entry with the AI, using the rest of the
    outline plus already-drafted chapters/continuity issues as context so it
    doesn't retroactively contradict finished work. An optional instruction
    (e.g. a flagged continuity issue from the consistency checker) is folded
    into the regeneration."""
    bible = load_bible(slug)
    instruction = req.instruction.strip() if req and req.instruction and req.instruction.strip() else None
    try:
        entry = outliner.regenerate_chapter(bible, chapter_num, extra_instruction=instruction)
        if bible.get_chapter(chapter_num) is not None:
            bible.upsert_chapter(chapter_num, title=entry["title"])
        return entry
    except AIOutputError as exc:
        logger.exception("Outline regenerate failed for project %r chapter %s", slug, chapter_num)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")
    except ValueError as exc:
        raise HTTPException(404, str(exc))


class ConsistencyCheckRequest(BaseModel):
    change_summary: str


@router.post("/api/projects/{slug}/consistency-check")
def consistency_check(slug: str, req: ConsistencyCheckRequest) -> dict[str, Any]:
    """Scans drafted chapters and outline entries for consequences of a
    change elsewhere in the bible (e.g. a character/world/engine edit or an
    outline revision). Manually triggered, never automatic, since it costs an
    LLM call and the writer should decide when a check is worth running."""
    if not req.change_summary.strip():
        raise HTTPException(400, "A change summary is required")
    bible = load_bible(slug)
    try:
        flags = continuity_checker.check(bible, req.change_summary)
    except AIOutputError as exc:
        logger.exception("Consistency check failed for project %r", slug)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")
    bible.add_continuity_flags(flags)
    return {"flags": flags}


@router.post("/api/projects/{slug}/continuity-flags/{flag_id}/resolve")
def resolve_continuity_flag(slug: str, flag_id: int) -> dict[str, Any]:
    """Dismisses a persisted continuity flag from the Continuity view -
    doesn't touch the flagged item itself, just marks the finding handled
    (or acknowledged as a non-issue) so it stops showing as open."""
    bible = load_bible(slug)
    bible.resolve_continuity_flag(flag_id)
    return {"ok": True}


class ReviseOutlineRequest(BaseModel):
    instruction: str


@router.post("/api/projects/{slug}/outline/revise")
def revise_outline(slug: str, req: ReviseOutlineRequest) -> list[dict[str, Any]]:
    """Drafts a revision of the ENTIRE outline per an instruction. Does NOT
    save - the writer reviews/edits the result and confirms via
    /outline/apply before it's persisted."""
    if not req.instruction.strip():
        raise HTTPException(400, "An instruction is required")
    bible = load_bible(slug)
    try:
        return outliner.revise_outline(bible, req.instruction)
    except AIOutputError as exc:
        logger.exception("Whole-outline revise failed for project %r", slug)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again or rephrase the instruction.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")


class ApplyOutlineRequest(BaseModel):
    outline: list[dict[str, Any]]


@router.post("/api/projects/{slug}/outline/apply")
def apply_outline(slug: str, req: ApplyOutlineRequest) -> list[dict[str, Any]]:
    """Saves a full outline replacement (from the Revise Whole Outline flow).
    Refuses to drop or renumber any chapter that already has drafted/approved
    prose, so an AI-drafted revision can't silently orphan finished work."""
    bible = load_bible(slug)
    drafted_nums = {
        c["chapter_num"] for c in bible.data.get("chapters", [])
        if c.get("history") or c.get("draft")
    }
    new_nums = {e["chapter_num"] for e in req.outline}
    missing = drafted_nums - new_nums
    if missing:
        raise HTTPException(
            400,
            f"Revision drops already-drafted chapter(s) {sorted(missing)} - "
            "edit the outline to keep them before applying.",
        )
    outline = [
        {
            "chapter_num": e["chapter_num"],
            "act": e.get("act"),
            "title": e.get("title", ""),
            "summary": e.get("summary", ""),
            "outline": e.get("outline", ""),
            "characters": e.get("characters") or [],
        }
        for e in req.outline
    ]
    outline.sort(key=lambda e: e["chapter_num"])
    bible.set_outline(outline)
    return bible.data["outline"]


class ActEditRequest(BaseModel):
    summary: str


@router.post("/api/projects/{slug}/acts/{name}")
def save_act_summary(slug: str, name: str, req: ActEditRequest) -> dict[str, Any]:
    """Saves the act-level summary (what happens across this act, before it's
    broken into individual chapter outlines). Also doubles as the "create a
    new act" call: POSTing an unseen act name with an empty summary adds it
    to bible.data["acts"] even before any outline entry references it."""
    bible = load_bible(slug)
    return bible.set_act_summary(name, req.summary)


class ElaborateActRequest(BaseModel):
    instruction: str | None = None


@router.post("/api/projects/{slug}/acts/{name}/elaborate")
def elaborate_act_summary(slug: str, name: str, req: ElaborateActRequest | None = None) -> dict[str, Any]:
    """Expands the act's summary with AI, using its own chapter outline
    entries as context. Saves the result immediately, same as a manual edit -
    the writer can undo by editing the summary field afterward."""
    bible = load_bible(slug)
    instruction = req.instruction.strip() if req and req.instruction and req.instruction.strip() else None
    try:
        summary = outliner.elaborate_act_summary(bible, name, extra_instruction=instruction)
    except AIOutputError as exc:
        logger.exception("Act summary elaboration failed for project %r act %r", slug, name)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")
    return bible.set_act_summary(name, summary)


@router.delete("/api/projects/{slug}/outline/{chapter_num}")
def delete_outline_entry(slug: str, chapter_num: int) -> dict[str, Any]:
    bible = load_bible(slug)
    try:
        bible.delete_outline_entry(chapter_num)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"deleted": chapter_num}
