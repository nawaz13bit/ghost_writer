"""Idea backlog CRUD and promotion into a real outline entry.

A holding area for chapter ideas the writer hasn't placed into an act/chapter
slot yet - see StoryBible's "idea backlog" section for the model-side
rationale."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ghostwriter.webui.deps import load_bible

router = APIRouter(tags=["ideas"])


class NewIdeaRequest(BaseModel):
    title: str
    notes: str = ""
    linked_kind: str | None = None
    linked_id: str | int | None = None


class IdeaEditRequest(BaseModel):
    title: str | None = None
    notes: str | None = None
    linked_kind: str | None = None
    linked_id: str | int | None = None


class PromoteIdeaRequest(BaseModel):
    chapter_num: int
    act: str | None = None
    summary: str = ""
    outline: str = ""


@router.post("/api/projects/{slug}/ideas")
def create_idea(slug: str, req: NewIdeaRequest) -> dict[str, Any]:
    if not req.title.strip():
        raise HTTPException(400, "A title is required")
    bible = load_bible(slug)
    return bible.add_idea(req.title.strip(), req.notes, req.linked_kind, req.linked_id)


@router.post("/api/projects/{slug}/ideas/{idea_id}/edit")
def edit_idea(slug: str, idea_id: int, req: IdeaEditRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    # exclude_unset (not the old "drop None" filter) so an explicit null -
    # e.g. clearing an idea's "relates to" link - actually gets applied
    # instead of being silently ignored.
    fields = req.model_dump(exclude_unset=True)
    try:
        return bible.update_idea(idea_id, **fields)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.delete("/api/projects/{slug}/ideas/{idea_id}")
def delete_idea(slug: str, idea_id: int) -> dict[str, Any]:
    bible = load_bible(slug)
    try:
        bible.delete_idea(idea_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"deleted": idea_id}


@router.post("/api/projects/{slug}/ideas/{idea_id}/promote")
def promote_idea(slug: str, idea_id: int, req: PromoteIdeaRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    try:
        return bible.promote_idea(idea_id, req.chapter_num, req.act, req.summary, req.outline)
    except ValueError as exc:
        status = 400 if "already exists" in str(exc) else 404
        raise HTTPException(status, str(exc))
