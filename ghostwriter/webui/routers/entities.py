"""Character/world/research-note entity CRUD, AI suggest/draft, structured
character sections, and the shared rename/revise/edit/diff/approve flows
that apply across all three entity kinds."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ghostwriter.agents.base import AIOutputError
from ghostwriter.llm_client import LLMCancelled
from ghostwriter.memory.story_bible import StoryBible
from ghostwriter.webui.deps import load_bible, maybe_compact_history
from ghostwriter.webui.diffing import word_diff
from ghostwriter.webui.state import character_builder, researcher, reviser, world_builder

logger = logging.getLogger(__name__)

router = APIRouter(tags=["entities"])

ENTITY_KINDS = ("characters", "world", "research_notes", "timeline")


class NewCharacterRequest(BaseModel):
    name: str
    role: str = "supporting"
    description: str = ""
    is_real: bool = False


class NewWorldEntryRequest(BaseModel):
    name: str
    category: str = "general"
    content: str = ""
    is_real: bool = False


class SourceRef(BaseModel):
    title: str
    url: str


class NewNoteRequest(BaseModel):
    name: str
    content: str = ""
    sources: list[SourceRef] = []


class TimelineConsequence(BaseModel):
    character: str
    status: str


class NewTimelineEventRequest(BaseModel):
    name: str
    story_date: str = ""
    description: str = ""
    chapter_num: int | None = None
    consequence: TimelineConsequence | None = None
    characters: list[str] = []
    locations: list[str] = []


class SuggestEntityRequest(BaseModel):
    prompt: str


class InstructionRequest(BaseModel):
    instruction: str


class ManualEditRequest(BaseModel):
    text: str


class ApproveRequest(BaseModel):
    history_id: int


@router.post("/api/projects/{slug}/entities/characters")
def create_character(slug: str, req: NewCharacterRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    if bible.find_character(req.name) is not None:
        raise HTTPException(400, f"Character {req.name!r} already exists")
    bible.add_character(req.name, req.role, req.description, req.is_real)
    return bible.get_entity("characters", req.name)


class SetCharacterRealRequest(BaseModel):
    is_real: bool


@router.post("/api/projects/{slug}/entities/characters/{name}/real")
def set_character_real(slug: str, name: str, req: SetCharacterRealRequest) -> dict[str, Any]:
    """Marks/unmarks a character as a real person who must stay factually
    accurate - the character-side counterpart to set_world_entry_real."""
    bible = load_bible(slug)
    try:
        return bible.set_character_real(name, req.is_real)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/api/projects/{slug}/entities/characters/suggest")
def suggest_character(slug: str, req: SuggestEntityRequest) -> dict[str, Any]:
    if not req.prompt.strip():
        raise HTTPException(400, "A description is required")
    bible = load_bible(slug)
    return character_builder.suggest_one(bible, req.prompt)


@router.post("/api/projects/{slug}/entities/world")
def create_world_entry(slug: str, req: NewWorldEntryRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    if bible.find_world_entry(req.name) is not None:
        raise HTTPException(400, f"World entry {req.name!r} already exists")
    bible.add_world_entry(req.name, req.category, req.content, req.is_real)
    return bible.get_entity("world", req.name)


class SetWorldRealRequest(BaseModel):
    is_real: bool


@router.post("/api/projects/{slug}/entities/world/{name}/real")
def set_world_entry_real(slug: str, name: str, req: SetWorldRealRequest) -> dict[str, Any]:
    """Marks/unmarks a world entry as a real place that must stay factually
    accurate, distinct from its (freeform) category - so the same entry can
    be categorized "location" whether it's real or invented."""
    bible = load_bible(slug)
    try:
        return bible.set_world_entry_real(name, req.is_real)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


class SetWorldCategoryRequest(BaseModel):
    category: str


@router.post("/api/projects/{slug}/entities/world/{name}/category")
def set_world_entry_category(slug: str, name: str, req: SetWorldCategoryRequest) -> dict[str, Any]:
    """Moves a world entry between the sidebar's Factions/Locations/
    Objects-Items/World(other) buckets by changing its category field."""
    bible = load_bible(slug)
    category = req.category.strip()
    if not category:
        raise HTTPException(400, "Category is required")
    try:
        return bible.set_world_entry_category(name, category)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/api/projects/{slug}/entities/world/suggest")
def suggest_world_entry(slug: str, req: SuggestEntityRequest) -> dict[str, Any]:
    if not req.prompt.strip():
        raise HTTPException(400, "A description is required")
    bible = load_bible(slug)
    return world_builder.suggest_one(bible, req.prompt)


@router.post("/api/projects/{slug}/world/migrate-import-notes")
def migrate_import_notes(slug: str) -> dict[str, Any]:
    bible = load_bible(slug)
    moved = bible.migrate_import_notes()
    return {"moved": moved}


@router.post("/api/projects/{slug}/entities/research_notes/suggest")
def suggest_note(slug: str, req: SuggestEntityRequest) -> dict[str, Any]:
    if not req.prompt.strip():
        raise HTTPException(400, "A topic is required")
    bible = load_bible(slug)
    return researcher.suggest_one(bible, req.prompt)


@router.post("/api/projects/{slug}/entities/research_notes")
def create_note(slug: str, req: NewNoteRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    if bible.get_entity("research_notes", req.name) is not None:
        raise HTTPException(400, f"Note {req.name!r} already exists")
    sources = [s.model_dump() for s in req.sources]
    bible.add_research_note(req.name, req.content, sources)
    return bible.get_entity("research_notes", req.name)


@router.post("/api/projects/{slug}/entities/timeline")
def create_timeline_event(slug: str, req: NewTimelineEventRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    if bible.find_timeline_event(req.name) is not None:
        raise HTTPException(400, f"Timeline event {req.name!r} already exists")
    consequence = req.consequence.model_dump() if req.consequence else None
    bible.add_timeline_event(
        req.name, req.story_date, req.description, req.chapter_num,
        consequence=consequence, characters=req.characters, locations=req.locations,
    )
    return bible.get_entity("timeline", req.name)


@router.post("/api/projects/{slug}/entities/timeline/{name}/consequence")
def set_timeline_consequence(slug: str, name: str, req: TimelineConsequence | None = None) -> dict[str, Any]:
    """Sets (or, with an empty body, clears) the status-change consequence on
    an existing timeline event - e.g. marking "The Siege of Kell" as the event
    that kills a character, so it follows them into the series bible."""
    bible = load_bible(slug)
    if bible.find_timeline_event(name) is None:
        raise HTTPException(404, f"No timeline event named {name!r}")
    consequence = req.model_dump() if req else None
    try:
        return bible.update_timeline_event(name, consequence=consequence)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


class CharacterSectionsRequest(BaseModel):
    sections: dict[str, str]


@router.post("/api/projects/{slug}/characters/{name}/sections")
def save_character_sections(slug: str, name: str, req: CharacterSectionsRequest) -> dict[str, Any]:
    """Directly saves one or more structured character sections (appearance,
    personality, background, goals/motivation, relationships, arc) - no
    revision history/approve step, since these are quick reference notes
    rather than prose."""
    bible = load_bible(slug)
    try:
        return bible.update_character_sections(name, req.sections)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


class CharacterFactionsRequest(BaseModel):
    factions: list[str]


@router.post("/api/projects/{slug}/characters/{name}/factions")
def save_character_factions(slug: str, name: str, req: CharacterFactionsRequest) -> dict[str, Any]:
    """Sets which faction/species/culture world entries this character is
    affiliated with - an explicit, curated link (distinct from the canvas's
    automatic name-mention linking) that also feeds the character context
    passed to the LLM, so affiliations actually shape generated prose."""
    bible = load_bible(slug)
    try:
        return bible.set_character_factions(name, req.factions)
    except ValueError as exc:
        raise HTTPException(404 if "No character" in str(exc) else 400, str(exc))


class WorldObjectsRequest(BaseModel):
    objects: list[str]


@router.post("/api/projects/{slug}/world/{name}/objects")
def save_world_entry_objects(slug: str, name: str, req: WorldObjectsRequest) -> dict[str, Any]:
    """Sets which object/item world entries a location entry uses - an
    explicit, curated link (same pattern as the character/factions link)
    that also feeds the location's context passed to the LLM."""
    bible = load_bible(slug)
    try:
        return bible.set_world_entry_objects(name, req.objects)
    except ValueError as exc:
        raise HTTPException(404 if "No world entry" in str(exc) else 400, str(exc))


@router.post("/api/projects/{slug}/characters/{name}/sections/draft")
def draft_character_sections(slug: str, name: str) -> dict[str, Any]:
    """Drafts the currently-empty structured sections with AI, for the writer
    to review/edit before saving - nothing is persisted here."""
    bible = load_bible(slug)
    character = bible.find_character(name)
    if character is None:
        raise HTTPException(404, f"No character named {name!r}")
    try:
        sections = character_builder.draft_sections(bible, character)
    except AIOutputError as exc:
        logger.exception("Draft sections failed for project %r character %r", slug, name)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")
    return {"sections": sections}


class ResyncSectionsRequest(BaseModel):
    new_facts: str


@router.post("/api/projects/{slug}/characters/{name}/sections/resync")
def resync_character_sections(slug: str, name: str, req: ResyncSectionsRequest) -> dict[str, Any]:
    """Given a newly-established fact (typically from a just-finalized
    chapter), drafts updated text for whichever already-filled sections that
    fact touches, for the writer to review/edit before saving - nothing is
    persisted here. Unlike /sections/draft, this can revise non-empty
    sections rather than only filling blanks."""
    bible = load_bible(slug)
    character = bible.find_character(name)
    if character is None:
        raise HTTPException(404, f"No character named {name!r}")
    try:
        sections = character_builder.resync_sections(bible, character, req.new_facts)
    except AIOutputError as exc:
        logger.exception("Resync sections failed for project %r character %r", slug, name)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")
    return {"sections": sections}


@router.delete("/api/projects/{slug}/entities/{kind}/{name}")
def delete_entity(slug: str, kind: str, name: str) -> dict[str, Any]:
    if kind not in ENTITY_KINDS:
        raise HTTPException(400, f"Unknown entity kind {kind!r}")
    bible = load_bible(slug)
    entity = bible.get_entity(kind, name)
    if entity is None:
        raise HTTPException(404, f"No {bible._singular(kind)} named {name!r}")
    bible.delete_entity(kind, name)
    return {"deleted": name}


class RenameEntityRequest(BaseModel):
    new_name: str


@router.post("/api/projects/{slug}/entities/{kind}/{name}/rename")
def rename_entity(slug: str, kind: str, name: str, req: RenameEntityRequest) -> dict[str, Any]:
    if kind not in ENTITY_KINDS:
        raise HTTPException(400, f"Unknown entity kind {kind!r}")
    bible = load_bible(slug)
    try:
        entity = bible.rename_entity(kind, name, req.new_name)
    except ValueError as exc:
        raise HTTPException(404 if "No " in str(exc) else 400, str(exc))
    return entity


@router.post("/api/projects/{slug}/entities/{kind}/{name}/revise")
def revise_entity(slug: str, kind: str, name: str, req: InstructionRequest) -> dict[str, Any]:
    if kind not in ENTITY_KINDS:
        raise HTTPException(400, f"Unknown entity kind {kind!r}")
    bible = load_bible(slug)
    entity = bible.get_entity(kind, name)
    if entity is None:
        raise HTTPException(404, f"No {bible._singular(kind)} named {name!r}")

    field = StoryBible.ENTITY_TEXT_FIELD[kind]
    text = entity[field]
    new_text = reviser.revise(f"a {bible._singular(kind)} bible entry named {name}", text, req.instruction)
    revision = bible.add_entity_revision(kind, name, new_text, instruction=req.instruction)
    compacted_count = maybe_compact_history(bible, entity["history"], f"{bible._singular(kind).title()} {name!r}")
    return {**revision, "diff": word_diff(text, new_text), "compacted_count": compacted_count}


@router.post("/api/projects/{slug}/entities/{kind}/{name}/edit")
def edit_entity(slug: str, kind: str, name: str, req: ManualEditRequest) -> dict[str, Any]:
    """Hand-typed edit for a character/world entry, bypassing the LLM reviser."""
    if kind not in ENTITY_KINDS:
        raise HTTPException(400, f"Unknown entity kind {kind!r}")
    bible = load_bible(slug)
    entity = bible.get_entity(kind, name)
    if entity is None:
        raise HTTPException(404, f"No {bible._singular(kind)} named {name!r}")

    field = StoryBible.ENTITY_TEXT_FIELD[kind]
    old_text = entity[field]
    revision = bible.add_entity_revision(kind, name, req.text, source="manual")
    compacted_count = maybe_compact_history(bible, entity["history"], f"{bible._singular(kind).title()} {name!r}")
    return {**revision, "diff": word_diff(old_text, req.text), "compacted_count": compacted_count}


@router.post("/api/projects/{slug}/entities/{kind}/{name}/approve")
def approve_entity(slug: str, kind: str, name: str, req: ApproveRequest) -> dict[str, Any]:
    if kind not in ENTITY_KINDS:
        raise HTTPException(400, f"Unknown entity kind {kind!r}")
    bible = load_bible(slug)
    entity = bible.get_entity(kind, name)
    if entity is None:
        raise HTTPException(404, f"No {bible._singular(kind)} named {name!r}")
    history = entity.get("history") or []
    try:
        text = history[req.history_id]["text"]
    except IndexError:
        raise HTTPException(404, "No such revision")
    bible.approve_entity_revision(kind, name, text)
    return bible.get_entity(kind, name)
