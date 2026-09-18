"""JSON-backed shared state for a single book project.

Every agent reads and writes through this one object, so it's the single
source of truth other agents draw context from (retrieval, continuity
checks, export).
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "untitled"


_REQUIRED_LIST_FIELDS = (
    "research_notes", "world", "characters", "outline", "acts", "chapters", "ideas", "timeline",
)


def _validate_data(data: dict[str, Any], source: str) -> None:
    """Guards against the class of corruption that's hit this project
    before (hand-edited JSON, a bad interpolation writing the literal string
    "undefined" into a field, a stale schema missing a key a newer code path
    assumes exists). Raises with a specific field name instead of letting a
    malformed bible surface later as a KeyError/TypeError deep in an agent
    or the UI."""
    if not isinstance(data, dict):
        raise ValueError(f"{source}: root is {type(data).__name__}, expected an object")
    if not isinstance(data.get("title"), str):
        raise ValueError(f"{source}: 'title' is {type(data.get('title')).__name__}, expected a string")
    for field in _REQUIRED_LIST_FIELDS:
        if field in data and not isinstance(data[field], list):
            raise ValueError(f"{source}: '{field}' is {type(data[field]).__name__}, expected a list")
    if "canvas_layout" in data and not isinstance(data["canvas_layout"], dict):
        raise ValueError(f"{source}: 'canvas_layout' is {type(data['canvas_layout']).__name__}, expected an object")
    for i, c in enumerate(data.get("characters", [])):
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or not c.get("name"):
            raise ValueError(f"{source}: characters[{i}] missing a valid 'name'")
        for j, r in enumerate(c.get("reveals") or []):
            if not isinstance(r, dict) or not isinstance(r.get("id"), int) or not isinstance(r.get("text"), str) \
                    or not isinstance(r.get("unlock_chapter_num"), int):
                raise ValueError(f"{source}: characters[{i}].reveals[{j}] missing a valid 'id'/'text'/'unlock_chapter_num'")
    for i, w in enumerate(data.get("world", [])):
        if not isinstance(w, dict) or not isinstance(w.get("name"), str) or not w.get("name"):
            raise ValueError(f"{source}: world[{i}] missing a valid 'name'")
    for i, t in enumerate(data.get("timeline", [])):
        if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t.get("name"):
            raise ValueError(f"{source}: timeline[{i}] missing a valid 'name'")
    for i, ch in enumerate(data.get("chapters", [])):
        if not isinstance(ch, dict) or not isinstance(ch.get("chapter_num"), int):
            raise ValueError(f"{source}: chapters[{i}] missing a valid 'chapter_num'")


def _migrate_outline_entry(entry: dict[str, Any]) -> None:
    """Old outline entries used fixed setup/event/resolution fields, which
    forced every book into the same three-beat shape regardless of genre.
    Entries now carry a single freeform "outline" field instead; entries
    saved under the old schema get folded into that field once, in place,
    the first time they're loaded."""
    if "outline" in entry:
        return
    parts = [entry.pop(k, "") for k in ("setup", "event", "resolution")]
    entry["outline"] = "\n\n".join(p for p in parts if p)


class StoryBible:
    def __init__(self, project_dir: Path, data: dict[str, Any]):
        self.project_dir = project_dir
        self.data = data
        self._save_suspended = 0
        self._dirty = False

    # -- construction -------------------------------------------------
    @classmethod
    def create(cls, projects_dir: Path, title: str, genre: str, premise: str, dir_slug: str | None = None) -> "StoryBible":
        slug = dir_slug or slugify(title)
        project_dir = Path(projects_dir) / slug
        project_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "title": title,
            "genre": genre,
            "premise": premise,
            "book_type": "fiction",
            "real_world_setting": False,
            "narrative_engine": "",
            "themes": "",
            "tone": "",
            "narrative_voice": "",
            "series_title": None,
            "series_book_num": None,
            "series_recap": "",
            "series_synced_at": None,
            "author_name": "",
            "blurb": "",
            "query_letter": "",
            "copyright_text": "",
            "foreword": "",
            "acknowledgments": "",
            "about_author": "",
            "research_notes": [],
            "world": [],
            "characters": [],
            "outline": [],
            "acts": [],
            "chapters": [],
            "ideas": [],
            "timeline": [],
            "timeline_digest_cache": None,
            "timeline_tracks": [],
            "timeline_crosspoints": [],
            "canvas_layout": {},
            "research_done": False,
            "world_built": False,
            "characters_built": False,
        }
        bible = cls(project_dir, data)
        bible.save()
        return bible

    @classmethod
    def create_in_series(cls, projects_dir: Path, title: str, genre: str, premise: str, series, book_num: int) -> "StoryBible":
        """Creates a new book bible seeded from a SeriesBible's canon: inherited
        characters/world/research are copied in as a starting point (read-only
        canon the builder agents can still add to), and series_recap carries
        forward a summary of prior books for outline/drafting context.

        Namespaces the project directory under the series slug so a book title
        that collides with a standalone project or another series can't cause
        load_or_create_in_series to silently load the wrong bible."""
        dir_slug = f"{slugify(series.data['series_title'])}__{slugify(title)}"
        bible = cls.create(projects_dir, title, genre, premise, dir_slug=dir_slug)
        bible.data["series_title"] = series.data["series_title"]
        bible.data["series_book_num"] = book_num
        bible.data["series_recap"] = series.series_recap(before_book_num=book_num)
        bible.data["characters"] = [dict(c) for c in series.data["characters"]]
        bible.data["world"] = [dict(w) for w in series.data["world"]]
        bible.data["research_notes"] = [dict(n) for n in series.data["research_notes"]]
        bible.save()
        return bible

    @classmethod
    def load_or_create_in_series(
        cls, projects_dir: Path, title: str, genre: str, premise: str, series, book_num: int
    ) -> "StoryBible":
        dir_slug = f"{slugify(series.data['series_title'])}__{slugify(title)}"
        path = Path(projects_dir) / dir_slug / "story_bible.json"
        if path.exists():
            return cls.load(projects_dir, title, dir_slug=dir_slug)
        return cls.create_in_series(projects_dir, title, genre, premise, series, book_num)

    @classmethod
    def load(cls, projects_dir: Path, title: str, dir_slug: str | None = None) -> "StoryBible":
        slug = dir_slug or slugify(title)
        project_dir = Path(projects_dir) / slug
        path = project_dir / "story_bible.json"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("canvas_layout", {})
        data.setdefault("acts", [])
        data.setdefault("ideas", [])
        data.setdefault("timeline", [])
        data.setdefault("series_synced_at", None)
        data.setdefault("book_type", "fiction")
        data.setdefault("real_world_setting", False)
        data.setdefault("author_name", "")
        data.setdefault("blurb", "")
        data.setdefault("query_letter", "")
        data.setdefault("copyright_text", "")
        data.setdefault("foreword", "")
        data.setdefault("acknowledgments", "")
        data.setdefault("about_author", "")
        data.setdefault("timeline_digest_cache", None)
        data.setdefault("timeline_tracks", [])
        data.setdefault("timeline_crosspoints", [])
        for event in data.get("timeline", []):
            event.setdefault("track_id", None)
            event.setdefault("chrono_order", None)
            event.setdefault("refers_back_to", None)
        for entry in data.get("outline", []):
            entry.setdefault("track_id", None)
        for note in data.get("research_notes", []):
            note.setdefault("sources", [])
            note.setdefault("id", slugify(note.get("name") or note.get("topic") or ""))
        for w in data.get("world", []):
            w.setdefault("is_real", False)
        for c in data.get("characters", []):
            c.setdefault("is_real", False)
            c.setdefault("reveals", [])
        for idea in data.get("ideas", []):
            idea.setdefault("category", None)
            idea.setdefault("origin_chapter", None)
        for entry in data.get("outline", []):
            _migrate_outline_entry(entry)
        _validate_data(data, str(path))
        return cls(project_dir, data)

    @classmethod
    def load_or_create(cls, projects_dir: Path, title: str, genre: str, premise: str) -> "StoryBible":
        slug = slugify(title)
        path = Path(projects_dir) / slug / "story_bible.json"
        if path.exists():
            return cls.load(projects_dir, title)
        return cls.create(projects_dir, title, genre, premise)

    def save(self) -> None:
        """Writes to a temp file, round-trip-validates what was actually
        written, then atomically replaces the real file - so a bad write
        (or a bad in-memory state) never clobbers a known-good
        story_bible.json. os.replace is atomic on both POSIX and Windows.

        While a batch_save() block is open, this just marks the bible dirty
        instead of writing - the block's exit does the one real write, so a
        request that calls several mutators in a row (e.g. finalizing a
        chapter, importing a manuscript) serializes/validates the whole file
        once instead of once per mutator call."""
        if self._save_suspended:
            self._dirty = True
            return
        self._write()

    def _write(self) -> None:
        path = self.project_dir / "story_bible.json"
        tmp_path = path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        with open(tmp_path, "r", encoding="utf-8") as f:
            written = json.load(f)
        _validate_data(written, str(tmp_path))
        os.replace(tmp_path, path)

    @contextmanager
    def batch_save(self):
        """Defers every save() inside the block to a single write at the
        end. Nestable - only the outermost block's exit actually writes."""
        self._save_suspended += 1
        try:
            yield self
        finally:
            self._save_suspended -= 1
            if self._save_suspended == 0 and self._dirty:
                self._dirty = False
                self._write()

    # -- mutators -------------------------------------------------------
    def add_research_note(
        self, topic: str, content: str, sources: list[dict[str, str]] | None = None
    ) -> dict[str, Any]:
        # "name" mirrors "topic" so notes flow through the same generic
        # get_entity/add_entity_revision machinery as characters/world.
        # "id" is the stable citation anchor inline markers ([^id]), the
        # chapter sources panel, and the book-level bibliography key off of.
        existing_ids = {n["id"] for n in self.data["research_notes"]}
        base_id = slugify(topic)
        note_id, n = base_id, 2
        while note_id in existing_ids:
            note_id = f"{base_id}-{n}"
            n += 1
        note = {
            "id": note_id, "name": topic, "topic": topic, "content": content,
            "sources": sources or [],
        }
        self.data["research_notes"].append(note)
        self.save()
        return note

    def add_world_entry(self, name: str, category: str, content: str, is_real: bool = False) -> None:
        self.data["world"].append({"name": name, "category": category, "content": content, "is_real": is_real})
        self.save()

    def set_world_entry_real(self, name: str, is_real: bool) -> dict[str, Any]:
        w = self.find_world_entry(name)
        if w is None:
            raise ValueError(f"No world entry named {name!r}")
        w["is_real"] = is_real
        self.save()
        return w

    def set_world_entry_category(self, name: str, category: str) -> dict[str, Any]:
        """Moves a world entry between the sidebar's category-filtered
        buckets (faction/location/object-item/other) by changing its
        freeform category field - the buckets aren't separate storage, just
        filters over self.data["world"], so this is the only write needed."""
        w = self.find_world_entry(name)
        if w is None:
            raise ValueError(f"No world entry named {name!r}")
        w["category"] = category
        self.save()
        return w

    def add_character(self, name: str, role: str, description: str, is_real: bool = False) -> None:
        self.data["characters"].append({"name": name, "role": role, "description": description, "is_real": is_real})
        self.save()

    def set_character_real(self, name: str, is_real: bool) -> dict[str, Any]:
        c = self.find_character(name)
        if c is None:
            raise ValueError(f"No character named {name!r}")
        c["is_real"] = is_real
        self.save()
        return c

    def has_real_entities(self) -> bool:
        """True if any character or world entry is tagged as a real
        person/place - used to auto-detect real-world research/fact-checking
        even in a project not otherwise marked real_world_setting (e.g. an
        invented setting that name-drops one real airport or agency)."""
        return any(w.get("is_real") for w in self.data["world"]) or any(
            c.get("is_real") for c in self.data["characters"]
        )

    def find_character(self, name: str) -> dict[str, Any] | None:
        name_lower = name.lower()
        for c in self.data["characters"]:
            if c["name"].lower() == name_lower:
                return c
        return None

    def append_character_facts(self, name: str, new_facts: str) -> None:
        c = self.find_character(name)
        if c is None:
            self.add_character(name, "supporting", new_facts)
            return
        c["description"] = f"{c['description']} {new_facts}".strip()
        self.save()

    def find_world_entry(self, name: str) -> dict[str, Any] | None:
        name_lower = name.lower()
        for w in self.data["world"]:
            if w["name"].lower() == name_lower:
                return w
        return None

    def append_world_facts(self, name: str, category: str, new_facts: str) -> None:
        w = self.find_world_entry(name)
        if w is None:
            self.add_world_entry(name, category, new_facts)
            return
        w["content"] = f"{w['content']} {new_facts}".strip()
        self.save()

    def add_timeline_event(
        self, name: str, story_date: str = "", description: str = "",
        chapter_num: int | None = None, order: int | None = None,
        consequence: dict[str, str] | None = None,
        characters: list[str] | None = None,
        locations: list[str] | None = None,
        track_id: str | None = None,
        chrono_order: int | None = None,
        refers_back_to: str | None = None,
    ) -> dict[str, Any]:
        """`consequence`, if given, is `{"character": name, "status": "dead"}`
        (or any freeform status) - a status change that, when this book joins
        a series, follows that character into later books until reverted
        (see SeriesBible.record_event/set_event_active). `characters`, if
        given, tags which characters this event involves, and `locations`
        tags which world entries (faction/location/object/item) it involves -
        lets the character/world editors show "events involving this entry"
        as a derived view without a separate structured event log per entry.
        `track_id` scopes the event to a cast/era (None = the single implicit
        "main" track, i.e. today's behavior). `chrono_order` is the event's
        in-world sequence number, independent of chapter_num/order (which are
        drafting/manuscript order) - None means the event isn't placed on a
        chronology yet. `refers_back_to` names an earlier (in chrono terms)
        event this one reveals/explains, for flashback/loop structures."""
        if order is None:
            order = max((t.get("order", 0) for t in self.data["timeline"]), default=0) + 1
        event = {
            "name": name, "story_date": story_date, "description": description,
            "chapter_num": chapter_num, "order": order, "created_at": time.time(),
            "consequence": consequence, "characters": characters or [],
            "locations": locations or [],
            "track_id": track_id, "chrono_order": chrono_order,
            "refers_back_to": refers_back_to,
        }
        self.data["timeline"].append(event)
        self.save()
        return event

    def find_timeline_event(self, name: str) -> dict[str, Any] | None:
        name_lower = name.lower()
        for t in self.data["timeline"]:
            if t["name"].lower() == name_lower:
                return t
        return None

    def update_timeline_event(self, name: str, **fields: Any) -> dict[str, Any]:
        event = self.find_timeline_event(name)
        if event is None:
            raise ValueError(f"No timeline event named {name!r}")
        event.update(fields)
        self.save()
        return event

    # -- Timeline tracks (Pattern B: parallel casts/eras) -------------------
    # A small registry so tracks can be named/colored in the UI rather than
    # just raw track_id strings; deleting a track never orphans its events -
    # they fall back to track_id=None (the implicit "main" track).

    def add_timeline_track(
        self, name: str, description: str = "", color: str | None = None,
    ) -> dict[str, Any]:
        track_id = str(uuid.uuid4())
        track = {"id": track_id, "name": name, "description": description, "color": color}
        self.data.setdefault("timeline_tracks", []).append(track)
        self.save()
        return track

    def list_timeline_tracks(self) -> list[dict[str, Any]]:
        return self.data.get("timeline_tracks", [])

    def find_timeline_track(self, track_id: str) -> dict[str, Any] | None:
        for t in self.data.get("timeline_tracks", []):
            if t["id"] == track_id:
                return t
        return None

    def update_timeline_track(self, track_id: str, **fields: Any) -> dict[str, Any]:
        track = self.find_timeline_track(track_id)
        if track is None:
            raise ValueError(f"No timeline track with id {track_id!r}")
        track.update(fields)
        self.save()
        return track

    def delete_timeline_track(self, track_id: str) -> None:
        if self.find_timeline_track(track_id) is None:
            raise ValueError(f"No timeline track with id {track_id!r}")
        self.data["timeline_tracks"] = [
            t for t in self.data.get("timeline_tracks", []) if t["id"] != track_id
        ]
        for event in self.data.get("timeline", []):
            if event.get("track_id") == track_id:
                event["track_id"] = None
        self.save()

    # -- Timeline crosspoints (Pattern B: cross-track links) -----------------
    # Typed edges between events on different tracks, used by the continuity
    # checker's paradox check and drawn as connecting lines in the swimlane
    # view. Kept as a flat list rather than nested under events since a
    # crosspoint conceptually belongs to neither event alone.

    CROSSPOINT_TYPES = ("cause_effect", "shared_location", "shared_object", "paradox_loop")

    def add_crosspoint(self, from_event: str, to_event: str, type: str) -> dict[str, Any]:
        if type not in self.CROSSPOINT_TYPES:
            raise ValueError(f"Unknown crosspoint type {type!r}")
        if self.find_timeline_event(from_event) is None:
            raise ValueError(f"No timeline event named {from_event!r}")
        if self.find_timeline_event(to_event) is None:
            raise ValueError(f"No timeline event named {to_event!r}")
        crosspoint = {
            "from_event": from_event, "to_event": to_event, "type": type,
            "created_at": time.time(),
        }
        self.data.setdefault("timeline_crosspoints", []).append(crosspoint)
        self.save()
        return crosspoint

    def list_crosspoints(self) -> list[dict[str, Any]]:
        return self.data.get("timeline_crosspoints", [])

    def delete_crosspoint(self, from_event: str, to_event: str, type: str) -> None:
        before = len(self.data.get("timeline_crosspoints", []))
        self.data["timeline_crosspoints"] = [
            c for c in self.data.get("timeline_crosspoints", [])
            if not (c["from_event"] == from_event and c["to_event"] == to_event and c["type"] == type)
        ]
        if len(self.data["timeline_crosspoints"]) == before:
            raise ValueError("No matching crosspoint found")
        self.save()

    def set_outline(self, chapters: list[dict[str, Any]]) -> None:
        self.data["outline"] = chapters
        self.save()

    def act_entry(self, name: str) -> dict[str, Any] | None:
        for entry in self.data["acts"]:
            if entry["name"] == name:
                return entry
        return None

    def set_act_summary(self, name: str, summary: str) -> dict[str, Any]:
        entry = self.act_entry(name)
        if entry is None:
            entry = {"name": name, "summary": summary}
            self.data["acts"].append(entry)
        else:
            entry["summary"] = summary
        self.save()
        return entry

    def add_outline_entry(
        self,
        chapter_num: int,
        title: str,
        summary: str = "",
        act: str | None = None,
        outline: str = "",
        characters: list[str] | None = None,
        world_refs: list[str] | None = None,
        track_id: str | None = None,
    ) -> dict[str, Any]:
        if self.outline_entry(chapter_num) is not None:
            raise ValueError(f"Outline entry for chapter {chapter_num} already exists")
        entry = {
            "chapter_num": chapter_num, "title": title, "summary": summary, "act": act,
            "outline": outline, "characters": characters or [], "world_refs": world_refs or [],
            "track_id": track_id,
        }
        self.data["outline"].append(entry)
        self.data["outline"].sort(key=lambda o: o["chapter_num"])
        self.save()
        return entry

    def update_outline_entry(self, chapter_num: int, **fields: Any) -> dict[str, Any]:
        entry = self.outline_entry(chapter_num)
        if entry is None:
            raise ValueError(f"No outline entry for chapter {chapter_num}")
        entry.update(fields)
        self.save()
        return entry

    def delete_outline_entry(self, chapter_num: int) -> None:
        entry = self.outline_entry(chapter_num)
        if entry is None:
            raise ValueError(f"No outline entry for chapter {chapter_num}")
        self.data["outline"].remove(entry)
        self.save()

    # -- scene-level planning ---------------------------------------------
    # Chapters can optionally be broken into scene cards for finer-grained
    # drafting; purely additive per outline entry - a chapter with no
    # "scenes" list drafts exactly as it always has (no load()/_validate_data
    # change needed, matching the continuity_issues pattern on chapters).
    SCENE_TRANSITIONS = ("continuous", "same-day", "time-skip", "pov-shift")

    def get_scene(self, chapter_num: int, scene_num: int) -> dict[str, Any] | None:
        entry = self.outline_entry(chapter_num)
        if entry is None:
            return None
        for scene in entry.get("scenes") or []:
            if scene["scene_num"] == scene_num:
                return scene
        return None

    def add_scene(
        self, chapter_num: int, beats: str = "", pov: str | None = None,
        emotional_state: str = "", transition: str = "continuous",
        location: str | None = None,
    ) -> dict[str, Any]:
        entry = self.outline_entry(chapter_num)
        if entry is None:
            raise ValueError(f"No outline entry for chapter {chapter_num}")
        scenes = entry.setdefault("scenes", [])
        next_num = max((s["scene_num"] for s in scenes), default=0) + 1
        scene = {
            "scene_num": next_num, "beats": beats, "pov": pov,
            "emotional_state": emotional_state, "transition": transition,
            "location": location, "draft": None, "history": [], "approved": False,
        }
        scenes.append(scene)
        self.save()
        return scene

    def update_scene(self, chapter_num: int, scene_num: int, **fields: Any) -> dict[str, Any]:
        scene = self.get_scene(chapter_num, scene_num)
        if scene is None:
            raise ValueError(f"No scene {scene_num} for chapter {chapter_num}")
        scene.update(fields)
        self.save()
        return scene

    def delete_scene(self, chapter_num: int, scene_num: int) -> None:
        entry = self.outline_entry(chapter_num)
        scene = self.get_scene(chapter_num, scene_num)
        if entry is None or scene is None:
            raise ValueError(f"No scene {scene_num} for chapter {chapter_num}")
        entry["scenes"].remove(scene)
        self.save()

    def add_scene_revision(
        self, chapter_num: int, scene_num: int, text: str, source: str, instruction: str | None = None,
    ) -> dict[str, Any]:
        scene = self.get_scene(chapter_num, scene_num)
        if scene is None:
            raise ValueError(f"No scene {scene_num} for chapter {chapter_num}")
        history = scene.setdefault("history", [])
        entry = {
            "id": len(history), "text": text, "source": source,
            "instruction": instruction, "created_at": time.time(),
        }
        history.append(entry)
        self.save()
        return entry

    def approve_scene(self, chapter_num: int, scene_num: int, text: str | None = None) -> dict[str, Any]:
        scene = self.get_scene(chapter_num, scene_num)
        if scene is None:
            raise ValueError(f"No scene {scene_num} for chapter {chapter_num}")
        if text is not None:
            scene["draft"] = text
        scene["approved"] = True
        self.save()
        return scene

    # -- idea backlog ----------------------------------------------------
    # A holding area for chapter ideas the writer hasn't placed yet - no
    # chapter_num/act required, unlike outline entries which need both. An
    # idea is promoted into a real outline entry once its slot is known.
    def get_idea(self, idea_id: int) -> dict[str, Any] | None:
        for idea in self.data["ideas"]:
            if idea["id"] == idea_id:
                return idea
        return None

    def add_idea(
        self, title: str, notes: str = "",
        linked_kind: str | None = None, linked_id: Any = None,
        category: str | None = None, origin_chapter: int | None = None,
    ) -> dict[str, Any]:
        next_id = max((i["id"] for i in self.data["ideas"]), default=0) + 1
        idea = {
            "id": next_id, "title": title, "notes": notes, "created_at": time.time(),
            "linked_kind": linked_kind, "linked_id": linked_id, "category": category,
            "origin_chapter": origin_chapter,
            "status": "open",
        }
        self.data["ideas"].append(idea)
        self.save()
        return idea

    def update_idea(self, idea_id: int, **fields: Any) -> dict[str, Any]:
        idea = self.get_idea(idea_id)
        if idea is None:
            raise ValueError(f"No idea {idea_id}")
        idea.update(fields)
        self.save()
        return idea

    def delete_idea(self, idea_id: int) -> None:
        idea = self.get_idea(idea_id)
        if idea is None:
            raise ValueError(f"No idea {idea_id}")
        self.data["ideas"].remove(idea)
        self.save()

    def promote_idea(
        self, idea_id: int, chapter_num: int, act: str | None = None,
        summary: str = "", outline: str = "",
    ) -> dict[str, Any]:
        """Turns a backlog idea into a real outline entry once its act/chapter
        slot is known, and removes it from the backlog. Title/notes seed the
        entry's title/outline fields but can be overridden here."""
        idea = self.get_idea(idea_id)
        if idea is None:
            raise ValueError(f"No idea {idea_id}")
        entry = self.add_outline_entry(
            chapter_num, idea["title"], summary, act, outline or idea.get("notes", "")
        )
        self.data["ideas"].remove(idea)
        self.save()
        return entry

    def upsert_chapter(self, chapter_num: int, **fields: Any) -> None:
        chapters = self.data["chapters"]
        for ch in chapters:
            if ch["chapter_num"] == chapter_num:
                ch.update(fields)
                self.save()
                return
        chapters.append({"chapter_num": chapter_num, **fields})
        chapters.sort(key=lambda c: c["chapter_num"])
        self.save()

    def delete_chapter(self, chapter_num: int) -> None:
        """Removes a chapter's drafted/revised text and history entirely -
        distinct from delete_outline_entry, which only removes the outline
        entry and leaves any drafted text behind."""
        ch = self.get_chapter(chapter_num)
        if ch is None:
            raise ValueError(f"No chapter {chapter_num}")
        self.data["chapters"].remove(ch)
        self.save()

    # -- UI revision/approval workflow -----------------------------------
    # Distinct from the automatic pipeline's draft->final chain: these let a
    # human review each revision and explicitly decide when a chapter (or
    # bible entry) is "final", instead of the pipeline auto-finalizing it.
    def add_chapter_revision(
        self, chapter_num: int, text: str, source: str, instruction: str | None = None,
        idea_id: int | None = None,
    ) -> dict[str, Any]:
        """idea_id links this revision to an idea being integrated (see
        [[project_ghostwriter_idea_integration_feature]]-style "Integrate into
        chapter" flow): recorded on the chapter's pending_idea_ids so
        approve_chapter can resolve it durably server-side once the writer
        approves this specific revision, instead of relying on transient
        client-side state that a browser refresh mid-finalize would lose."""
        ch = self.get_chapter(chapter_num)
        if ch is None:
            self.upsert_chapter(chapter_num, history=[])
            ch = self.get_chapter(chapter_num)
        history = ch.setdefault("history", [])
        entry = {
            "id": len(history),
            "text": text,
            "source": source,
            "instruction": instruction,
            "created_at": time.time(),
        }
        history.append(entry)
        if idea_id is not None:
            pending = ch.setdefault("pending_idea_ids", [])
            if idea_id not in pending:
                pending.append(idea_id)
        self.save()
        return entry

    def link_idea_to_outline(self, chapter_num: int, idea_id: int) -> None:
        """Records that an idea was folded into an undrafted chapter's outline
        summary ("Integrate into outline"), so it resolves once that chapter
        is eventually drafted and approved - see approve_chapter."""
        entry = self.outline_entry(chapter_num)
        if entry is None:
            raise ValueError(f"No outline entry for chapter {chapter_num}")
        pending = entry.setdefault("pending_idea_ids", [])
        if idea_id not in pending:
            pending.append(idea_id)
        self.save()

    def approve_chapter(self, chapter_num: int, text: str) -> None:
        """(Re-)finalizes a chapter. Approval is not a lock - re-approving an
        already-approved chapter is expected (e.g. a permanent change like a
        character losing an arm). Since later chapters may already assume the
        old facts, they're flagged needs_recheck rather than silently rewritten -
        a human decides whether/how to fix each one via check-continuity.

        This also covers out-of-order drafting: chapters aren't necessarily
        written or first-approved in chapter_num order, so a later chapter
        can already be approved before an earlier one is ever touched. Even
        on this chapter's FIRST approval (not just re-approval), any
        already-approved chapter with a higher chapter_num may have been
        written assuming facts this chapter has just newly established -
        flag those for recheck too, not only on re-approval.

        needs_recheck_from records which chapter_num(s) triggered the flag,
        so the UI can point the writer at the specific chapter to compare
        against instead of a bare "something changed" warning.

        Also resolves any ideas pending on this chapter (from an "Integrate
        into chapter" revision) or on its outline entry (from an "Integrate
        into outline" note, now finally drafted+approved) - this runs
        server-side as part of approval itself, so it can't be lost by a
        browser refresh during the slow finalize pipeline the way the old
        client-only pendingIdeaIntegration flag could."""
        self.upsert_chapter(chapter_num, final=text, approved=True)
        for ch in self.data["chapters"]:
            if ch["chapter_num"] > chapter_num and ch.get("approved"):
                ch["needs_recheck"] = True
                triggers = ch.get("needs_recheck_from") or []
                if chapter_num not in triggers:
                    ch["needs_recheck_from"] = sorted(triggers + [chapter_num])
        resolved_ideas: list[dict[str, Any]] = []
        ch = self.get_chapter(chapter_num)
        entry = self.outline_entry(chapter_num)
        pending_ids = list((ch.get("pending_idea_ids") or []) if ch else [])
        pending_ids += list((entry.get("pending_idea_ids") or []) if entry else [])
        for idea_id in dict.fromkeys(pending_ids):
            idea = self.get_idea(idea_id)
            if idea is not None and idea.get("status") != "resolved":
                idea["status"] = "resolved"
                resolved_ideas.append(idea)
        if ch is not None:
            ch["pending_idea_ids"] = []
        if entry is not None:
            entry["pending_idea_ids"] = []
        self.save()
        return resolved_ideas

    # -- translation (post-finalization final pass) --------------------------
    # A separate per-language field on each chapter, distinct from history/
    # final - translation runs only after the English text is finalized, so
    # it never competes with the English revision chain. Written directly
    # via setdefault rather than through upsert_chapter, whose ch.update()
    # would clobber every other language's entry under the same key.
    def set_chapter_translation(self, chapter_num: int, language: str, text: str) -> None:
        ch = self.get_chapter(chapter_num)
        if ch is None:
            raise ValueError(f"No chapter {chapter_num}")
        ch.setdefault("translations", {})[language] = {"text": text, "translated_at": time.time()}
        self.save()

    def get_chapter_translation(self, chapter_num: int, language: str) -> dict[str, Any] | None:
        ch = self.get_chapter(chapter_num)
        if ch is None:
            return None
        return (ch.get("translations") or {}).get(language)

    def get_translation_glossary(self, language: str) -> dict[str, str]:
        """Name -> rendering map for a target language, built once per book so a
        character/place name renders identically in every chapter instead of
        drifting translation to translation."""
        return dict((self.data.setdefault("translation_glossaries", {}).get(language)) or {})

    def set_translation_glossary(self, language: str, mapping: dict[str, str]) -> None:
        self.data.setdefault("translation_glossaries", {})[language] = mapping
        self.save()

    # -- continuity flags ---------------------------------------------------
    # Findings from a manual consistency-check run, persisted so they show up
    # project-wide in the Continuity view (not just for the session that ran
    # the check) and cover character/world/note kinds, which - unlike
    # chapters - have no other field to hold an open issue on. Purely a flag
    # list: never rewrites anything, same as continuity_issues on chapters.
    def add_continuity_flags(self, flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
        open_flags = self.data.setdefault("continuity_flags", [])
        existing_keys = {
            (f.get("kind"), f.get("chapter_num"), f.get("target_name"), f.get("issue")) for f in open_flags
        }
        next_id = max((f.get("id", 0) for f in open_flags), default=0) + 1
        for flag in flags:
            key = (flag.get("kind"), flag.get("chapter_num"), flag.get("target_name"), flag.get("issue"))
            if key in existing_keys:
                continue
            existing_keys.add(key)
            open_flags.append({**flag, "id": next_id, "created_at": time.time()})
            next_id += 1
        self.save()
        return open_flags

    def resolve_continuity_flag(self, flag_id: int) -> None:
        open_flags = self.data.setdefault("continuity_flags", [])
        self.data["continuity_flags"] = [f for f in open_flags if f.get("id") != flag_id]
        self.save()

    # -- critique flags -------------------------------------------------
    # Same review-gated-flag pattern as continuity_flags above, kept as its
    # own list (not merged into continuity_flags) so developmental-editing
    # findings (pacing/stakes/craft/book-level) render in their own
    # Critique sidebar section rather than mixing with fact/continuity
    # correctness issues.
    def add_critique_flags(self, flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
        open_flags = self.data.setdefault("critique_flags", [])
        existing_keys = {
            (f.get("category"), f.get("chapter_num"), f.get("issue")) for f in open_flags
        }
        next_id = max((f.get("id", 0) for f in open_flags), default=0) + 1
        for flag in flags:
            key = (flag.get("category"), flag.get("chapter_num"), flag.get("issue"))
            if key in existing_keys:
                continue
            existing_keys.add(key)
            open_flags.append({**flag, "id": next_id, "created_at": time.time()})
            next_id += 1
        self.save()
        return open_flags

    def resolve_critique_flag(self, flag_id: int) -> None:
        open_flags = self.data.setdefault("critique_flags", [])
        self.data["critique_flags"] = [f for f in open_flags if f.get("id") != flag_id]
        self.save()

    # -- character structured sections ------------------------------------
    # Independent of the description/history/approve machinery below - these
    # are directly-edited supplementary fields (no revision history, since
    # they're meant to be quick reference notes, not prose that needs
    # AI-revise/diff/approve treatment).
    CHARACTER_SECTION_KEYS = ["appearance", "personality", "background", "goals_motivation", "relationships", "arc"]

    def update_character_sections(self, name: str, sections: dict[str, str]) -> dict[str, Any]:
        c = self.find_character(name)
        if c is None:
            raise ValueError(f"No character named {name!r}")
        existing = c.setdefault("sections", {})
        for key, text in sections.items():
            if key in self.CHARACTER_SECTION_KEYS:
                existing[key] = text
        self.save()
        return c

    def set_character_factions(self, name: str, factions: list[str]) -> dict[str, Any]:
        """Sets a character's faction/species/culture affiliations (names of
        world entries filed under category "faction") - an explicit, curated
        link rather than the canvas's heuristic name-mention linking, so it
        actually feeds character context passed to the LLM (see
        characters_brief) instead of being purely visual."""
        c = self.find_character(name)
        if c is None:
            raise ValueError(f"No character named {name!r}")
        known = {w["name"] for w in self.data["world"] if w.get("category") == "faction"}
        unknown = [f for f in factions if f not in known]
        if unknown:
            raise ValueError(f"Not a known faction: {', '.join(unknown)!r}")
        c["factions"] = factions
        self.save()
        return c

    # -- plot-gated character reveals --------------------------------------
    # A character's base name/role/description is always visible in draft
    # prompts, but deeper facts (backstory, secrets, relationships) can be
    # withheld until a specific outline beat has actually happened, so a
    # central character gets introduced organically instead of fully
    # dumped on their first appearance. Independent of CHARACTER_SECTION_KEYS
    # above - `section` here is just an optional display grouping tag, not a
    # link to those text fields. See characters_brief()'s `unlock_chapter_num`
    # param for how gating is applied (only author.py's draft prompts opt in;
    # every other caller - consistency checks, voice checks, bible sync,
    # outlining - sees every reveal regardless of chapter).
    def add_character_reveal(
        self, name: str, text: str, unlock_chapter_num: int, section: str | None = None,
    ) -> dict[str, Any]:
        c = self.find_character(name)
        if c is None:
            raise ValueError(f"No character named {name!r}")
        if self.outline_entry(unlock_chapter_num) is None:
            raise ValueError(f"Not a known outline chapter: {unlock_chapter_num!r}")
        reveals = c.setdefault("reveals", [])
        next_id = max((r["id"] for r in reveals), default=0) + 1
        reveal = {
            "id": next_id, "text": text, "unlock_chapter_num": unlock_chapter_num,
            "section": section, "created_at": time.time(),
        }
        reveals.append(reveal)
        self.save()
        return reveal

    def _find_character_reveal(self, name: str, reveal_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        c = self.find_character(name)
        if c is None:
            raise ValueError(f"No character named {name!r}")
        for r in c.get("reveals", []):
            if r["id"] == reveal_id:
                return c, r
        raise ValueError(f"No reveal {reveal_id} on character {name!r}")

    def update_character_reveal(self, name: str, reveal_id: int, **fields: Any) -> dict[str, Any]:
        _, r = self._find_character_reveal(name, reveal_id)
        if "unlock_chapter_num" in fields and self.outline_entry(fields["unlock_chapter_num"]) is None:
            raise ValueError(f"Not a known outline chapter: {fields['unlock_chapter_num']!r}")
        r.update(fields)
        self.save()
        return r

    def delete_character_reveal(self, name: str, reveal_id: int) -> None:
        c, r = self._find_character_reveal(name, reveal_id)
        c["reveals"].remove(r)
        self.save()

    def set_world_entry_objects(self, name: str, objects: list[str]) -> dict[str, Any]:
        """Sets which object/item world entries a location entry uses - an
        explicit, curated link (same pattern as set_character_factions)
        so e.g. a location's known props actually feed world_brief context
        passed to the LLM, distinct from the canvas's heuristic linking."""
        w = self.find_world_entry(name)
        if w is None:
            raise ValueError(f"No world entry named {name!r}")
        known = {e["name"] for e in self.data["world"] if e.get("category") in ("object", "item")}
        unknown = [o for o in objects if o not in known]
        if unknown:
            raise ValueError(f"Not a known object/item: {', '.join(unknown)!r}")
        w["used_objects"] = objects
        self.save()
        return w

    # -- entity (character/world/research_notes/timeline) revision/approval --
    ENTITY_TEXT_FIELD = {
        "characters": "description", "world": "content", "research_notes": "content",
        "timeline": "description",
    }
    # kind[:-1] singularizes fine for the plural kinds (characters/world -
    # world has no trailing "s" to strip so it's a no-op there too), but
    # "timeline" isn't plural, so it needs an explicit label for error text.
    ENTITY_SINGULAR = {"timeline": "timeline event"}

    def _singular(self, kind: str) -> str:
        return self.ENTITY_SINGULAR.get(kind, kind[:-1])

    def get_entity(self, kind: str, name: str) -> dict[str, Any] | None:
        name_lower = name.lower()
        for e in self.data[kind]:
            # research_notes predating the "name" field only have "topic".
            e_name = e.get("name") or e.get("topic")
            if e_name and e_name.lower() == name_lower:
                return e
        return None

    def add_entity_revision(
        self, kind: str, name: str, text: str, instruction: str | None = None, source: str = "instruction"
    ) -> dict[str, Any]:
        entity = self.get_entity(kind, name)
        if entity is None:
            raise ValueError(f"No {self._singular(kind)} named {name!r}")
        history = entity.setdefault("history", [])
        entry = {
            "id": len(history),
            "text": text,
            "source": source,
            "instruction": instruction,
            "created_at": time.time(),
        }
        history.append(entry)
        self.save()
        return entry

    def approve_entity_revision(self, kind: str, name: str, text: str) -> None:
        entity = self.get_entity(kind, name)
        if entity is None:
            raise ValueError(f"No {self._singular(kind)} named {name!r}")
        entity[self.ENTITY_TEXT_FIELD[kind]] = text
        entity["approved"] = True
        self.save()

    # -- history compaction -------------------------------------------------
    # add_chapter_revision/add_entity_revision/add_scene_revision above all
    # append a full-text snapshot on every revision, forever - on a long book
    # this becomes the largest thing in the file (e.g. one character's history
    # alone can dwarf every other field combined). Compaction folds everything
    # but the most recent KEEP_TAIL entries into one synthetic entry holding
    # an LLM-written synopsis, then the cap can be hit again later. Canonical
    # text (a chapter's "final", an entity's description/content) is never
    # touched - only this audit trail shrinks. The synopsis itself has to be
    # written by an LLM call, which this pure-data class has no access to, so
    # this only does the mechanical fold; see webui/deps.py:maybe_compact_history
    # for the orchestration (check cap -> call the compactor agent -> fold).
    HISTORY_CAP = 12
    HISTORY_KEEP_TAIL = 4

    @staticmethod
    def history_needs_compaction(history: list[dict[str, Any]]) -> bool:
        return len(history) > StoryBible.HISTORY_CAP

    @staticmethod
    def compact_history(history: list[dict[str, Any]], synopsis: str, keep_tail: int = HISTORY_KEEP_TAIL) -> int:
        """Mutates history in place: folds every entry but the last keep_tail
        into one synthetic {"source": "compaction"} entry holding synopsis.
        Returns how many entries were folded away (0 if there weren't enough
        to compact)."""
        if len(history) <= keep_tail:
            return 0
        folded, tail = history[:-keep_tail], history[-keep_tail:]
        compacted_count = len(folded)
        synthetic = {
            "id": 0, "text": synopsis, "source": "compaction", "instruction": None,
            "created_at": time.time(), "compacted_count": compacted_count,
        }
        history[:] = [synthetic] + tail
        for i, entry in enumerate(history):
            entry["id"] = i
        return compacted_count

    def delete_entity(self, kind: str, name: str) -> None:
        entity = self.get_entity(kind, name)
        if entity is None:
            raise ValueError(f"No {self._singular(kind)} named {name!r}")
        self.data[kind].remove(entity)
        self.save()

    def rename_entity(self, kind: str, name: str, new_name: str) -> dict[str, Any]:
        entity = self.get_entity(kind, name)
        if entity is None:
            raise ValueError(f"No {self._singular(kind)} named {name!r}")
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("New name cannot be empty")
        if new_name.lower() != name.lower() and self.get_entity(kind, new_name) is not None:
            raise ValueError(f"A {self._singular(kind)} named {new_name!r} already exists")
        entity["name"] = new_name
        layout = self.data.get("canvas_layout") or {}
        old_key = f"{kind}:{name}"
        if old_key in layout:
            layout[f"{kind}:{new_name}"] = layout.pop(old_key)
        self.save()
        return entity

    def migrate_import_notes(self) -> int:
        """One-time cleanup for projects imported before the Notes panel existed:
        moves World entries filed under category "import-note" (unsorted import
        text that landed in World for lack of anywhere better) into research_notes,
        where they belong and can be reviewed/re-filed by hand. Returns the count
        moved."""
        to_move = [e for e in self.data["world"] if e.get("category") == "import-note"]
        if not to_move:
            return 0
        used_names = {(n.get("name") or n.get("topic") or "").lower() for n in self.data["research_notes"]}
        for e in to_move:
            self.data["world"].remove(e)
            base_name = e.get("name") or "Untitled note"
            name = base_name
            i = 2
            while name.lower() in used_names:
                name = f"{base_name} ({i})"
                i += 1
            used_names.add(name.lower())
            self.data["research_notes"].append({"name": name, "topic": name, "content": e.get("content", "")})
        self.save()
        return len(to_move)

    # -- series ------------------------------------------------------------
    def attach_to_series(self, series, book_num: int) -> None:
        """Retrofits an already-existing standalone project onto a series: renames
        the project directory to the series-namespaced form create_in_series uses
        (so future load_or_create_in_series calls for other books find it), seeds
        the series bible's shared canon from this project's current characters/
        world/notes via absorb_book, and records this book's place in the series."""
        new_slug = f"{slugify(series.data['series_title'])}__{slugify(self.data['title'])}"
        new_dir = self.project_dir.parent / new_slug
        if new_dir != self.project_dir and new_dir.exists():
            raise ValueError(f"A project already exists at {new_slug!r}")

        series.absorb_book(
            book_num,
            title=self.data["title"],
            synopsis=self.data.get("premise", ""),
            characters=self.data["characters"],
            world=self.data["world"],
            research_notes=self.data["research_notes"],
            timeline=self.data.get("timeline"),
        )
        self.data["series_title"] = series.data["series_title"]
        self.data["series_book_num"] = book_num
        self.data["series_recap"] = series.series_recap(before_book_num=book_num)
        self.data["series_synced_at"] = time.time()

        if new_dir != self.project_dir:
            self.project_dir.rename(new_dir)
            self.project_dir = new_dir
        self.save()

    def sync_to_series(self, series) -> None:
        """Re-absorbs this book's current characters/world/notes/timeline into
        series canon. Unlike attach_to_series (a one-time onboarding step,
        4xx's if already attached), this is safe to call repeatedly as a book
        progresses - e.g. right after a chapter introduces a status-changing
        event, so it's visible on the Series Bible page and inherited by the
        next book without waiting for this one to finish."""
        if not self.data.get("series_title"):
            raise ValueError("Project is not part of a series")
        series.absorb_book(
            self.data["series_book_num"],
            title=self.data["title"],
            synopsis=self.data.get("premise", ""),
            characters=self.data["characters"],
            world=self.data["world"],
            research_notes=self.data["research_notes"],
            timeline=self.data.get("timeline"),
        )
        self.data["series_synced_at"] = time.time()
        self.save()

    def series_sync_pending(self) -> bool:
        """True if this book belongs to a series and has recorded a
        status-changing timeline event (e.g. a character death) since the
        last sync - the signal that the Series Bible page/next book don't
        see yet without an explicit sync_to_series call."""
        if not self.data.get("series_title"):
            return False
        synced_at = self.data.get("series_synced_at") or 0
        return any(
            t.get("consequence") and t.get("created_at", 0) > synced_at
            for t in self.data.get("timeline", [])
        )

    # -- accessors --------------------------------------------------------
    def get_chapter(self, chapter_num: int) -> dict[str, Any] | None:
        for ch in self.data["chapters"]:
            if ch["chapter_num"] == chapter_num:
                return ch
        return None

    def outline_entry(self, chapter_num: int) -> dict[str, Any] | None:
        for entry in self.data["outline"]:
            if entry["chapter_num"] == chapter_num:
                return entry
        return None

    def previous_chapters_summary(self, before_chapter_num: int, max_chapters: int = 3) -> str:
        """Short recap of the most recent finished chapters, for prompt context."""
        chapters = [c for c in self.data["chapters"] if c["chapter_num"] < before_chapter_num]
        chapters.sort(key=lambda c: c["chapter_num"])
        recap = chapters[-max_chapters:]
        if not recap:
            return "(This is the first chapter.)"
        lines = []
        for ch in recap:
            title = ch.get("title", f"Chapter {ch['chapter_num']}")
            summary = ch.get("summary") or ch.get("final", "")[:400]
            lines.append(f"Chapter {ch['chapter_num']} - {title}: {summary}")
        return "\n".join(lines)

    def character_chapter_neighbors(self, chapter_num: int, characters: list[str]) -> tuple[str, set[int]]:
        """For each character, finds the nearest drafted chapter before AND
        after chapter_num (in outline order) that also features them - so
        continuity context isn't limited to strictly-preceding chapters when
        the book is written out of order (chapters drafted non-sequentially).
        Uses each outline entry's "characters" field, falling back to its
        "pov" field for entries that predate that field. Returns the brief
        text plus the set of chapter numbers it drew on, so callers can keep
        continuity_context() from independently pulling in the same chapter's
        full text as a redundant BM25 hit."""
        drafted_nums = {c["chapter_num"] for c in self.data["chapters"] if c.get("final") or c.get("draft")}
        by_num = {c["chapter_num"]: c for c in self.data["chapters"]}
        lines = []
        used_nums: set[int] = set()
        for name in characters:
            name = (name or "").strip()
            if not name:
                continue
            before = None
            after = None
            for entry in sorted(self.data["outline"], key=lambda o: o["chapter_num"]):
                num = entry["chapter_num"]
                if num == chapter_num or num not in drafted_nums:
                    continue
                cast = entry.get("characters") or ([entry["pov"]] if entry.get("pov") else [])
                if name not in cast:
                    continue
                if num < chapter_num:
                    before = entry
                elif after is None:
                    after = entry
            for entry, direction in ((before, "earlier"), (after, "later")):
                if entry is None:
                    continue
                ch = by_num.get(entry["chapter_num"])
                summary = (ch.get("summary") or ch.get("final", "")[:400]) if ch else entry.get("summary", "")
                lines.append(
                    f"{name} - nearest {direction} drafted chapter with them, "
                    f"Chapter {entry['chapter_num']} ({entry.get('title', '')}): {summary}"
                )
                used_nums.add(entry["chapter_num"])
        return "\n".join(lines), used_nums

    def continuity_and_progress_brief(self, exclude_chapter_num: int | None = None) -> str:
        """Summarizes already-drafted/approved chapters and any open continuity
        issues, so a chapter can be regenerated without contradicting work
        that's already locked in. Returns "" if nothing's been drafted yet."""
        chapters = [c for c in self.data["chapters"] if c["chapter_num"] != exclude_chapter_num]
        chapters.sort(key=lambda c: c["chapter_num"])
        drafted = [c for c in chapters if (c.get("history") or c.get("draft"))]
        if not drafted:
            return ""
        lines = ["Chapters already drafted (do not contradict these):"]
        for ch in drafted:
            title = ch.get("title", f"Chapter {ch['chapter_num']}")
            status = "approved" if ch.get("approved") else "drafted"
            summary = ch.get("summary") or ch.get("final", "")[:400]
            lines.append(f"- Chapter {ch['chapter_num']} ({status}) - {title}: {summary}")
        issues = []
        for ch in chapters:
            for issue in ch.get("continuity_issues") or []:
                issues.append(f"- Chapter {ch['chapter_num']}: {issue if isinstance(issue, str) else str(issue)}")
        if issues:
            lines.append("Open continuity issues to keep in mind:")
            lines.extend(issues)
        return "\n".join(lines)

    def planted_threads_brief(self, chapter_num: int) -> str:
        """Ideas tagged category="planted_thread" and linked to this outline
        entry - setups an earlier chapter planted (see ThreadPlannerAgent)
        that this chapter should pay off. Purely additive prompt context.
        Resolved/dropped threads are excluded - see ThreadPlannerAgent's
        propose_resolutions, which proposes marking a thread resolved once a
        chapter pays it off (review-gated, not automatic)."""
        threads = [
            i for i in self.data["ideas"]
            if i.get("category") == "planted_thread"
            and i.get("linked_kind") == "outline"
            and i.get("linked_id") == chapter_num
            and i.get("status", "open") == "open"
        ]
        if not threads:
            return ""
        return "\n".join(f"- {t['notes'] or t['title']}" for t in threads)

    def open_ideas_brief(self) -> str:
        """All open backlog ideas (not filtered to planted_thread, unlike
        planted_threads_brief), for the continuity checker to flag a chapter
        that contradicts or silently ignores one - purely additive prompt
        context, not a hard requirement the chapter must satisfy."""
        ideas = [i for i in self.data["ideas"] if i.get("status", "open") == "open"]
        if not ideas:
            return ""
        return "\n".join(f"- {i['title']}: {i['notes']}" if i.get("notes") else f"- {i['title']}" for i in ideas)

    def story_engine_brief(self) -> str:
        """Formats tone/narrative_voice/narrative_engine/themes for prompt
        injection, or "" if the writer hasn't filled any in - callers should
        omit the section entirely rather than pass an empty placeholder to
        the LLM."""
        tone = self.data.get("tone", "").strip()
        voice = self.data.get("narrative_voice", "").strip()
        engine = self.data.get("narrative_engine", "").strip()
        themes = self.data.get("themes", "").strip()
        if not tone and not voice and not engine and not themes:
            return ""
        lines = []
        if tone:
            lines.append(f"Tone: {tone}")
        if voice:
            lines.append(f"Narrative voice/style: {voice}")
        if engine:
            lines.append(f"Narrative engine (what should drive momentum chapter to chapter): {engine}")
        if themes:
            lines.append(f"Themes to keep alive throughout (do not state on the nose): {themes}")
        return "\n".join(lines)

    def characters_brief(self, names: list[str] | None = None, unlock_chapter_num: int | None = None) -> str:
        """`names`, when given, limits the brief to those characters (case-
        insensitive) instead of the whole roster - keeps per-chapter prompts
        (draft/continuity/voice checks) from growing unboundedly with total
        character count as the book gets longer. Callers that genuinely need
        the whole cast (outline building, bible sync, character creation)
        pass nothing and get the full brief as before.

        `unlock_chapter_num`, when given, additionally gates each
        character's `reveals` (see add_character_reveal) to only those whose
        unlock_chapter_num has already been reached - so a central character
        can be introduced organically instead of fully dumped on their first
        appearance. Left as None (the default), every reveal is shown
        regardless of chapter - this is the "ground truth" view every caller
        except author.py's draft prompts should use (consistency/voice
        checks, bible sync, outlining all need the full picture)."""
        chars = self.data["characters"]
        if names is not None:
            wanted = {n.strip().lower() for n in names if n and n.strip()}
            chars = [c for c in chars if (c.get("name") or "").strip().lower() in wanted]
        lines = []
        for c in chars:
            factions = c.get("factions") or []
            tag = f" [Factions: {', '.join(factions)}]" if factions else ""
            real_tag = " [REAL PERSON - keep accurate]" if c.get("is_real") else ""
            lines.append(f"- {c['name']} ({c['role']}){tag}{real_tag}: {c['description']}")
            reveals = c.get("reveals") or []
            if unlock_chapter_num is not None:
                reveals = [r for r in reveals if r["unlock_chapter_num"] <= unlock_chapter_num]
            if reveals:
                lines.append(f"  Reveals: {'; '.join(r['text'] for r in reveals)}")
        return "\n".join(lines) if lines else "(no characters defined yet)"

    def world_brief(self) -> str:
        lines = []
        for w in self.data["world"]:
            real_tag = " [REAL - keep accurate]" if w.get("is_real") else ""
            objects = w.get("used_objects") or []
            objects_tag = f" [Uses: {', '.join(objects)}]" if objects else ""
            lines.append(f"- {w['name']} [{w['category']}]{real_tag}{objects_tag}: {w['content']}")
        return "\n".join(lines) if lines else "(no world-building notes yet)"

    def _timeline_sort_key(self, t: dict[str, Any]) -> tuple:
        # Groups events by track (None/"main" first) then by chrono_order when
        # set - the in-world sequence, which can differ from drafting order
        # (flashbacks, time loops). Events without chrono_order fall back to
        # the original (chapter_num, order) drafting-order sort: chapters are
        # often drafted out of sequence, so an event from a later-in-story
        # chapter drafted first must not appear before an earlier chapter's
        # events drafted afterward. Undated/no-chapter events sort first;
        # "order" only breaks ties within a chapter.
        chrono = t.get("chrono_order")
        return (
            t.get("track_id") or "",
            0 if chrono is not None else 1,
            chrono if chrono is not None else 0,
            t.get("chapter_num") if t.get("chapter_num") is not None else -1,
            t.get("order", 0),
        )

    def timeline_events_for_track(self, track_id: str) -> list[dict[str, Any]]:
        """Events belonging to the given track, plus any event on another
        track connected to one of them via a crosspoint - a crosspoint is
        precisely the reason two tracks intersect, so the connected event's
        facts are relevant even though it lives on a different track. Used
        to scope draft-time timeline injection (Agent.timeline_digest) to an
        outline entry's track instead of the whole book."""
        events = self.data.get("timeline", [])
        own = [e for e in events if e.get("track_id") == track_id]
        own_names = {e["name"] for e in own}
        linked_names: set[str] = set()
        for cp in self.data.get("timeline_crosspoints", []):
            if cp["from_event"] in own_names:
                linked_names.add(cp["to_event"])
            elif cp["to_event"] in own_names:
                linked_names.add(cp["from_event"])
        linked = [e for e in events if e["name"] in linked_names and e.get("track_id") != track_id]
        return own + linked

    def timeline_event_lines(self, events: list[dict[str, Any]] | None = None) -> list[str]:
        events = sorted(events if events is not None else self.data.get("timeline", []), key=self._timeline_sort_key)
        lines = []
        for t in events:
            date = t.get("story_date") or "(undated)"
            chapter_tag = f" [Ch.{t['chapter_num']}]" if t.get("chapter_num") else ""
            chars = t.get("characters") or []
            chars_tag = f" [Characters: {', '.join(chars)}]" if chars else ""
            locs = t.get("locations") or []
            locs_tag = f" [Locations: {', '.join(locs)}]" if locs else ""
            track = self.find_timeline_track(t["track_id"]) if t.get("track_id") else None
            track_tag = f" [Track: {track['name']}]" if track else ""
            chrono_tag = f" [Chrono #{t['chrono_order']}]" if t.get("chrono_order") is not None else ""
            lines.append(
                f"- {date}: {t['name']}{chapter_tag}{track_tag}{chrono_tag}{chars_tag}{locs_tag} - {t.get('description', '')}"
            )
        return lines

    def timeline_brief(self) -> str:
        lines = self.timeline_event_lines()
        return "\n".join(lines) if lines else "(no timeline events defined yet)"

    def timeline_chrono_view(self, track_id: str | None = None) -> dict[str, Any]:
        """Structured timeline for the swimlane UI: events grouped by track
        (sorted by chrono_order, undated-chrono events last) plus the
        crosspoints connecting them. Distinct from timeline_brief(), which is
        prompt-facing prose text."""
        tracks = self.list_timeline_tracks()
        events = self.data.get("timeline", [])
        if track_id is not None:
            events = [e for e in events if e.get("track_id") == track_id]

        def chrono_key(t: dict[str, Any]) -> tuple:
            chrono = t.get("chrono_order")
            return (0 if chrono is not None else 1, chrono if chrono is not None else 0)

        by_track: dict[str | None, list[dict[str, Any]]] = {}
        for e in sorted(events, key=chrono_key):
            by_track.setdefault(e.get("track_id"), []).append(e)

        lanes = []
        for t in tracks:
            if track_id is not None and t["id"] != track_id:
                continue
            lanes.append({"track": t, "events": by_track.get(t["id"], [])})
        if None in by_track and (track_id is None):
            lanes.append({"track": None, "events": by_track[None]})

        return {"lanes": lanes, "crosspoints": self.list_crosspoints()}

    def timeline_signature(self, events: list[dict[str, Any]] | None = None) -> str:
        """Stable fingerprint of the current timeline event set - used by
        get_timeline_digest/set_timeline_digest to tell whether a cached
        digest (see agents/base.py's Agent.timeline_digest) is stale.
        Content-based rather than a simple len() check so an edited or
        deleted event also invalidates the cache, not just an added one."""
        import hashlib
        events = sorted(
            events if events is not None else self.data.get("timeline", []),
            key=lambda t: (t.get("name", ""), t.get("chapter_num") or -1),
        )
        fingerprint = json.dumps(
            [[t.get("name"), t.get("story_date"), t.get("chapter_num"), t.get("description"),
              t.get("characters"), t.get("locations"), t.get("track_id"), t.get("chrono_order"),
              t.get("refers_back_to")] for t in events],
            sort_keys=True,
        )
        return hashlib.md5(fingerprint.encode("utf-8")).hexdigest()

    def get_timeline_digest(self, track_id: str | None = None) -> str | None:
        """Returns the cached compact timeline digest if one exists and still
        matches the current timeline_signature(), else None to tell the
        caller (Agent.timeline_digest) it must rebuild it. track_id scopes
        both the cache slot and the signature to one track's (plus its
        crosspoint-linked) events, kept separate from the whole-book cache
        (key None) so a track-scoped draft prompt never sees a stale
        whole-book digest or vice versa."""
        cache_key = track_id or "__all__"
        cache = self.data.get("timeline_digest_cache")
        if not isinstance(cache, dict) or cache_key not in cache:
            return None
        entry = cache[cache_key]
        events = self.timeline_events_for_track(track_id) if track_id else None
        if entry.get("signature") != self.timeline_signature(events):
            return None
        return entry["text"]

    def set_timeline_digest(self, text: str, track_id: str | None = None) -> None:
        cache_key = track_id or "__all__"
        cache = self.data.get("timeline_digest_cache")
        if not isinstance(cache, dict):
            cache = {}
        events = self.timeline_events_for_track(track_id) if track_id else None
        cache[cache_key] = {"signature": self.timeline_signature(events), "text": text}
        self.data["timeline_digest_cache"] = cache
        self.save()

    def research_note_lines(self) -> list[str]:
        return [f"- {n['topic']}: {n['content']}" for n in self.data["research_notes"]]

    def research_brief(self) -> str:
        lines = self.research_note_lines()
        return "\n".join(lines) if lines else "(no research notes yet)"

    def full_synopsis_brief(self) -> str:
        """Complete, spoiler-full recap of every finished chapter's events, built
        from per-chapter summaries. Used to seed the *next* book's series canon -
        deliberately not the marketing blurb/short_synopsis, which is written
        spoiler-light on purpose and would leave book N+1 outlining against an
        incomplete picture of how book N ended."""
        chapters = sorted(self.data["chapters"], key=lambda c: c["chapter_num"])
        lines = []
        for ch in chapters:
            summary = ch.get("summary")
            if summary:
                title = ch.get("title", f"Chapter {ch['chapter_num']}")
                lines.append(f"Ch{ch['chapter_num']} ({title}): {summary}")
        return "\n".join(lines) if lines else "(no chapters finished)"

    def full_outline_brief(self, around_chapter: int | None = None, window: int = 10) -> str:
        """`around_chapter`, when given, limits the brief to chapters within
        `window` of it instead of the whole outline - keeps per-chapter draft
        prompts (which only need "where does this chapter sit") from growing
        unboundedly with total chapter count as the book gets longer. Callers
        that need the whole outline (e.g. building/regenerating it) pass
        nothing and get the full brief as before."""
        outline = self.data["outline"]
        if around_chapter is not None:
            outline = [o for o in outline if abs(o["chapter_num"] - around_chapter) <= window]
        lines = [f"{o['chapter_num']}. {o['title']} - {o['summary']}" for o in outline]
        return "\n".join(lines) if lines else "(no outline yet)"

    def all_chapter_text(self) -> list[tuple[int, str, str]]:
        """Returns (chapter_num, title, text) for every finished chapter, in order."""
        chapters = sorted(self.data["chapters"], key=lambda c: c["chapter_num"])
        out = []
        for ch in chapters:
            text = ch.get("final") or ch.get("draft") or ""
            if text:
                out.append((ch["chapter_num"], ch.get("title", f"Chapter {ch['chapter_num']}"), text))
        return out
