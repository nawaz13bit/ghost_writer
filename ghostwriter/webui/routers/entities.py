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
from ghostwriter.webui.deps import load_bible, maybe_compact_history, with_bible_lock
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
    track_id: str | None = None
    chrono_order: int | None = None
    refers_back_to: str | None = None


class TimelinePlacementRequest(BaseModel):
    track_id: str | None = None
    chrono_order: int | None = None
    refers_back_to: str | None = None


class NewTimelineTrackRequest(BaseModel):
    name: str
    description: str = ""
    color: str | None = None


class UpdateTimelineTrackRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    color: str | None = None


class NewCrosspointRequest(BaseModel):
    from_event: str
    to_event: str
    type: str


class DeleteCrosspointRequest(BaseModel):
    from_event: str
    to_event: str
    type: str


class SuggestEntityRequest(BaseModel):
    prompt: str


class InstructionRequest(BaseModel):
    instruction: str


class ManualEditRequest(BaseModel):
    text: str


class ApproveRequest(BaseModel):
    history_id: int


@router.post("/api/projects/{slug}/entities/characters")
@with_bible_lock
def create_character(slug: str, req: NewCharacterRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    if bible.find_character(req.name) is not None:
        raise HTTPException(400, f"Character {req.name!r} already exists")
    bible.add_character(req.name, req.role, req.description, req.is_real)
    return bible.get_entity("characters", req.name)


class SetCharacterRealRequest(BaseModel):
    is_real: bool


@router.post("/api/projects/{slug}/entities/characters/{name}/real")
@with_bible_lock
def set_character_real(slug: str, name: str, req: SetCharacterRealRequest) -> dict[str, Any]:
    """Marks/unmarks a character as a real person who must stay factually
    accurate - the character-side counterpart to set_world_entry_real."""
    bible = load_bible(slug)
    try:
        return bible.set_character_real(name, req.is_real)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


# Kinds with an AI "suggest one" flow, each via .suggest_one(bible, prompt) -
# not the full ENTITY_KINDS: timeline has no suggest today, and reveal/
# outline suggest are shaped differently and stay as their own routes.
SUGGEST_KIND_AGENT = {
    "characters": character_builder,
    "world": world_builder,
    "research_notes": researcher,
}
SUGGEST_KIND_ERROR = {
    "characters": "A description is required",
    "world": "A description is required",
    "research_notes": "A topic is required",
}


@router.post("/api/projects/{slug}/entities/{kind}/suggest")
def suggest_entity(slug: str, kind: str, req: SuggestEntityRequest) -> dict[str, Any]:
    if kind not in SUGGEST_KIND_AGENT:
        raise HTTPException(400, f"Unsupported entity kind {kind!r}")
    if not req.prompt.strip():
        raise HTTPException(400, SUGGEST_KIND_ERROR[kind])
    bible = load_bible(slug)
    return SUGGEST_KIND_AGENT[kind].suggest_one(bible, req.prompt)


@router.post("/api/projects/{slug}/entities/world")
@with_bible_lock
def create_world_entry(slug: str, req: NewWorldEntryRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    if bible.find_world_entry(req.name) is not None:
        raise HTTPException(400, f"World entry {req.name!r} already exists")
    bible.add_world_entry(req.name, req.category, req.content, req.is_real)
    return bible.get_entity("world", req.name)


class SetWorldRealRequest(BaseModel):
    is_real: bool


@router.post("/api/projects/{slug}/entities/world/{name}/real")
@with_bible_lock
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
@with_bible_lock
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


@router.post("/api/projects/{slug}/world/migrate-import-notes")
@with_bible_lock
def migrate_import_notes(slug: str) -> dict[str, Any]:
    bible = load_bible(slug)
    moved = bible.migrate_import_notes()
    return {"moved": moved}


@router.post("/api/projects/{slug}/entities/research_notes")
@with_bible_lock
def create_note(slug: str, req: NewNoteRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    if bible.get_entity("research_notes", req.name) is not None:
        raise HTTPException(400, f"Note {req.name!r} already exists")
    sources = [s.model_dump() for s in req.sources]
    bible.add_research_note(req.name, req.content, sources)
    return bible.get_entity("research_notes", req.name)


@router.post("/api/projects/{slug}/entities/timeline")
@with_bible_lock
def create_timeline_event(slug: str, req: NewTimelineEventRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    if bible.find_timeline_event(req.name) is not None:
        raise HTTPException(400, f"Timeline event {req.name!r} already exists")
    consequence = req.consequence.model_dump() if req.consequence else None
    bible.add_timeline_event(
        req.name, req.story_date, req.description, req.chapter_num,
        consequence=consequence, characters=req.characters, locations=req.locations,
        track_id=req.track_id, chrono_order=req.chrono_order, refers_back_to=req.refers_back_to,
    )
    return bible.get_entity("timeline", req.name)


@router.post("/api/projects/{slug}/entities/timeline/{name}/consequence")
@with_bible_lock
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


@router.post("/api/projects/{slug}/entities/timeline/{name}/placement")
@with_bible_lock
def set_timeline_placement(slug: str, name: str, req: TimelinePlacementRequest) -> dict[str, Any]:
    """Sets an existing timeline event's track/chrono-order/reveal-link -
    the structured non-linear-timeline fields, distinct from the freeform
    description edited via the generic entities/{kind}/{name}/edit route."""
    bible = load_bible(slug)
    if bible.find_timeline_event(name) is None:
        raise HTTPException(404, f"No timeline event named {name!r}")
    if req.track_id is not None and bible.find_timeline_track(req.track_id) is None:
        raise HTTPException(404, f"No timeline track with id {req.track_id!r}")
    if req.refers_back_to is not None and bible.find_timeline_event(req.refers_back_to) is None:
        raise HTTPException(404, f"No timeline event named {req.refers_back_to!r}")
    try:
        return bible.update_timeline_event(
            name, track_id=req.track_id, chrono_order=req.chrono_order, refers_back_to=req.refers_back_to,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.get("/api/projects/{slug}/timeline/tracks")
def list_timeline_tracks(slug: str) -> list[dict[str, Any]]:
    return load_bible(slug).list_timeline_tracks()


@router.post("/api/projects/{slug}/timeline/tracks")
@with_bible_lock
def create_timeline_track(slug: str, req: NewTimelineTrackRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    return bible.add_timeline_track(req.name, req.description, req.color)


@router.post("/api/projects/{slug}/timeline/tracks/{track_id}")
@with_bible_lock
def update_timeline_track(slug: str, track_id: str, req: UpdateTimelineTrackRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        return bible.update_timeline_track(track_id, **fields)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.delete("/api/projects/{slug}/timeline/tracks/{track_id}")
@with_bible_lock
def delete_timeline_track(slug: str, track_id: str) -> dict[str, Any]:
    bible = load_bible(slug)
    try:
        bible.delete_timeline_track(track_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"deleted": track_id}


@router.get("/api/projects/{slug}/timeline/crosspoints")
def list_crosspoints(slug: str) -> list[dict[str, Any]]:
    return load_bible(slug).list_crosspoints()


@router.post("/api/projects/{slug}/timeline/crosspoints")
@with_bible_lock
def create_crosspoint(slug: str, req: NewCrosspointRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    try:
        return bible.add_crosspoint(req.from_event, req.to_event, req.type)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/api/projects/{slug}/timeline/crosspoints")
@with_bible_lock
def delete_crosspoint(slug: str, req: DeleteCrosspointRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    try:
        bible.delete_crosspoint(req.from_event, req.to_event, req.type)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"deleted": True}


@router.get("/api/projects/{slug}/timeline/chrono-view")
def timeline_chrono_view(slug: str, track_id: str | None = None) -> dict[str, Any]:
    return load_bible(slug).timeline_chrono_view(track_id)


class CharacterSectionsRequest(BaseModel):
    sections: dict[str, str]


@router.post("/api/projects/{slug}/characters/{name}/sections")
@with_bible_lock
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
@with_bible_lock
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


class CharacterRevealRequest(BaseModel):
    text: str
    unlock_chapter_num: int
    section: str | None = None


@router.post("/api/projects/{slug}/characters/{name}/reveals")
@with_bible_lock
def create_character_reveal(slug: str, name: str, req: CharacterRevealRequest) -> dict[str, Any]:
    """Adds a plot-gated reveal: a fact about this character that only
    appears in draft prompts once the linked outline chapter has been
    reached, so a central character can be introduced organically instead
    of having their whole bible dumped on first appearance."""
    bible = load_bible(slug)
    try:
        return bible.add_character_reveal(name, req.text, req.unlock_chapter_num, req.section)
    except ValueError as exc:
        raise HTTPException(404 if "No character" in str(exc) else 400, str(exc))


class CharacterRevealEditRequest(BaseModel):
    text: str | None = None
    unlock_chapter_num: int | None = None
    section: str | None = None


@router.post("/api/projects/{slug}/characters/{name}/reveals/{reveal_id}/edit")
@with_bible_lock
def edit_character_reveal(slug: str, name: str, reveal_id: int, req: CharacterRevealEditRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        return bible.update_character_reveal(name, reveal_id, **fields)
    except ValueError as exc:
        raise HTTPException(404 if "No character" in str(exc) or "No reveal" in str(exc) else 400, str(exc))


@router.delete("/api/projects/{slug}/characters/{name}/reveals/{reveal_id}")
@with_bible_lock
def remove_character_reveal(slug: str, name: str, reveal_id: int) -> dict[str, Any]:
    bible = load_bible(slug)
    try:
        bible.delete_character_reveal(name, reveal_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True}


class SuggestRevealRequest(BaseModel):
    unlock_chapter_num: int
    section: str | None = None
    prompt: str = ""


@router.post("/api/projects/{slug}/characters/{name}/reveals/suggest")
def suggest_character_reveal(slug: str, name: str, req: SuggestRevealRequest) -> dict[str, Any]:
    """Drafts a plot-gated reveal for this character with AI - using the
    target chapter's outline plus the character's sections and already-planned
    reveals as context - for the writer to review/edit before saving via the
    normal POST .../reveals call. Nothing is persisted here."""
    bible = load_bible(slug)
    character = bible.find_character(name)
    if character is None:
        raise HTTPException(404, f"No character named {name!r}")
    try:
        return character_builder.suggest_reveal(
            bible, character, req.unlock_chapter_num, req.section, req.prompt
        )
    except AIOutputError as exc:
        logger.exception("Suggest reveal failed for project %r character %r", slug, name)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")


class WorldObjectsRequest(BaseModel):
    objects: list[str]


@router.post("/api/projects/{slug}/world/{name}/objects")
@with_bible_lock
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
    chapter_num: int | None = None


@router.post("/api/projects/{slug}/characters/{name}/sections/resync")
def resync_character_sections(slug: str, name: str, req: ResyncSectionsRequest) -> dict[str, Any]:
    """Given a newly-established fact (typically from a just-finalized
    chapter), drafts updated text for whichever already-filled sections that
    fact touches, for the writer to review/edit before saving - nothing is
    persisted here. Unlike /sections/draft, this can revise non-empty
    sections rather than only filling blanks.

    chapter_num (the source chapter's book position) lets the AI account for
    out-of-order finalization - see resync_sections' docstring."""
    bible = load_bible(slug)
    character = bible.find_character(name)
    if character is None:
        raise HTTPException(404, f"No character named {name!r}")
    try:
        sections = character_builder.resync_sections(bible, character, req.new_facts, req.chapter_num)
    except AIOutputError as exc:
        logger.exception("Resync sections failed for project %r character %r", slug, name)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")
    return {"sections": sections}


@router.delete("/api/projects/{slug}/entities/{kind}/{name}")
@with_bible_lock
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
@with_bible_lock
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
@with_bible_lock
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
@with_bible_lock
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
@with_bible_lock
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
