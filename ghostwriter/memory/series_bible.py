"""JSON-backed shared state for a multi-book series.

Sits above the per-book StoryBible: when a book belongs to a series, its
bible is seeded from the series bible's canon (characters, world, persona
roster, and a recap of prior books), and once that book is finalized its
new/changed facts are merged back up here for the next book to inherit.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ghostwriter.memory.story_bible import slugify


class SeriesBible:
    def __init__(self, series_dir: Path, data: dict[str, Any]):
        self.series_dir = series_dir
        self.data = data

    # -- construction -------------------------------------------------
    @classmethod
    def create(
        cls,
        series_root: Path,
        series_title: str,
        genre: str,
        premise: str,
        persona_keys: list[str] | None = None,
    ) -> "SeriesBible":
        slug = slugify(series_title)
        series_dir = Path(series_root) / slug
        series_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "series_title": series_title,
            "genre": genre,
            "premise": premise,
            "persona_keys": persona_keys,
            "characters": [],
            "world": [],
            "research_notes": [],
            "books": [],
            "persistent_events": [],
        }
        series = cls(series_dir, data)
        series.save()
        return series

    @classmethod
    def load(cls, series_root: Path, series_title: str) -> "SeriesBible":
        return cls.load_by_slug(series_root, slugify(series_title))

    @classmethod
    def load_by_slug(cls, series_root: Path, slug: str) -> "SeriesBible":
        series_dir = Path(series_root) / slug
        path = series_dir / "series_bible.json"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("persistent_events", [])
        return cls(series_dir, data)

    @classmethod
    def load_or_create(
        cls,
        series_root: Path,
        series_title: str,
        genre: str,
        premise: str,
        persona_keys: list[str] | None = None,
    ) -> "SeriesBible":
        slug = slugify(series_title)
        path = Path(series_root) / slug / "series_bible.json"
        if path.exists():
            return cls.load(series_root, series_title)
        return cls.create(series_root, series_title, genre, premise, persona_keys)

    def save(self) -> None:
        path = self.series_dir / "series_bible.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    # -- book tracking --------------------------------------------------
    def get_book(self, book_num: int) -> dict[str, Any] | None:
        for b in self.data["books"]:
            if b["book_num"] == book_num:
                return b
        return None

    def upsert_book(self, book_num: int, **fields: Any) -> None:
        book = self.get_book(book_num)
        if book is None:
            self.data["books"].append({"book_num": book_num, **fields})
            self.data["books"].sort(key=lambda b: b["book_num"])
        else:
            book.update(fields)
        self.save()

    def series_recap(self, before_book_num: int | None = None) -> str:
        """Formatted recap of prior books' synopses, for seeding/prompt context."""
        books = sorted(self.data["books"], key=lambda b: b["book_num"])
        if before_book_num is not None:
            books = [b for b in books if b["book_num"] < before_book_num]
        if not books:
            return "(This is the first book in the series.)"
        lines = []
        for b in books:
            synopsis = b.get("synopsis", "(no synopsis recorded)")
            lines.append(f"Book {b['book_num']} - {b.get('title', '')}: {synopsis}")
        return "\n".join(lines)

    # -- canon merge (characters/world) ----------------------------------
    def find_character(self, name: str) -> dict[str, Any] | None:
        name_lower = name.lower()
        for c in self.data["characters"]:
            if c["name"].lower() == name_lower:
                return c
        return None

    def upsert_character(self, name: str, role: str, description: str) -> None:
        c = self.find_character(name)
        if c is None:
            self.data["characters"].append({"name": name, "role": role, "description": description})
        else:
            c["role"] = role
            c["description"] = description
        self.save()

    def find_world_entry(self, name: str) -> dict[str, Any] | None:
        name_lower = name.lower()
        for w in self.data["world"]:
            if w["name"].lower() == name_lower:
                return w
        return None

    def upsert_world_entry(self, name: str, category: str, content: str) -> None:
        w = self.find_world_entry(name)
        if w is None:
            self.data["world"].append({"name": name, "category": category, "content": content})
        else:
            w["category"] = category
            w["content"] = content
        self.save()

    def find_research_note(self, topic: str) -> dict[str, Any] | None:
        topic_lower = topic.lower()
        for n in self.data["research_notes"]:
            if n["topic"].lower() == topic_lower:
                return n
        return None

    def upsert_research_note(self, topic: str, content: str) -> None:
        n = self.find_research_note(topic)
        if n is None:
            self.data["research_notes"].append({"topic": topic, "content": content})
        else:
            n["content"] = content
        self.save()

    def absorb_book(
        self,
        book_num: int,
        title: str,
        synopsis: str,
        characters: list[dict],
        world: list[dict],
        research_notes: list[dict] | None = None,
        timeline: list[dict] | None = None,
    ) -> None:
        """Called after a book is finalized: promotes its characters/world/research
        back into series canon (so book N+1 inherits them) and records its synopsis.
        Any timeline event carrying a "consequence" (e.g. a character's death) is
        recorded as a persistent series event, so the status it sets follows that
        character into later books until someone explicitly reverts it."""
        for c in characters:
            self.upsert_character(c["name"], c["role"], c["description"])
        for w in world:
            self.upsert_world_entry(w["name"], w["category"], w["content"])
        for n in research_notes or []:
            self.upsert_research_note(n["topic"], n["content"])
        for t in timeline or []:
            consequence = t.get("consequence")
            if consequence and consequence.get("character") and consequence.get("status"):
                self.record_event(
                    book_num, consequence["character"], t["name"],
                    t.get("description", ""), consequence["status"],
                )
        self.upsert_book(book_num, title=title, synopsis=synopsis)

    # -- persistent events (status changes that carry across books) -----
    # A character's "status" (alive/dead/missing/anything freeform) is plain
    # data on their series-canon entry, always directly hand-editable for a
    # retcon. Persistent events are the *audit trail* behind it: each records
    # which book set which status, so reverting one event recomputes the
    # character's status from whatever events remain active instead of just
    # toggling a flag - letting a character be "killed" in one event and
    # "resurrected" by reverting it, or by a later event overriding it.
    def get_character_status(self, name: str) -> str:
        c = self.find_character(name)
        return (c or {}).get("status", "alive")

    def _recompute_character_status(self, name: str) -> str:
        name_lower = name.lower()
        events = [
            e for e in self.data["persistent_events"]
            if e["character"].lower() == name_lower and e["active"]
        ]
        events.sort(key=lambda e: (e["book_num"], e["created_at"]))
        status = "alive"
        for e in events:
            status = e["status"]
        c = self.find_character(name)
        if c is not None:
            c["status"] = status
        return status

    def record_event(
        self, book_num: int, character: str, event_name: str, description: str, status: str,
    ) -> dict[str, Any]:
        """Upserted on (book_num, character, event_name) so re-syncing a book
        already absorbed into the series (e.g. after editing mid-book) doesn't
        duplicate the event or silently undo a revert/retcon made on the
        Series Bible page - a pre-existing match is returned untouched."""
        for e in self.data["persistent_events"]:
            if e["book_num"] == book_num and e["character"] == character and e["event_name"] == event_name:
                return e
        next_id = max((e["id"] for e in self.data["persistent_events"]), default=0) + 1
        event = {
            "id": next_id, "book_num": book_num, "character": character,
            "event_name": event_name, "description": description, "status": status,
            "active": True, "created_at": time.time(),
        }
        self.data["persistent_events"].append(event)
        self._recompute_character_status(character)
        self.save()
        return event

    def find_persistent_event(self, event_id: int) -> dict[str, Any] | None:
        for e in self.data["persistent_events"]:
            if e["id"] == event_id:
                return e
        return None

    def set_event_active(self, event_id: int, active: bool) -> dict[str, Any]:
        """Reverting un-applies the event's status (e.g. resurrects a killed
        character); reapplying re-applies it. Either way status is recomputed
        from all remaining active events, not toggled blindly."""
        event = self.find_persistent_event(event_id)
        if event is None:
            raise ValueError(f"No persistent event {event_id}")
        event["active"] = active
        self._recompute_character_status(event["character"])
        self.save()
        return event

    def update_persistent_event(self, event_id: int, **fields: Any) -> dict[str, Any]:
        """Edits an event in place (e.g. retcon "dies" into "gravely wounded")
        and recomputes status for whichever character(s) it touches."""
        event = self.find_persistent_event(event_id)
        if event is None:
            raise ValueError(f"No persistent event {event_id}")
        old_character = event["character"]
        event.update(fields)
        self._recompute_character_status(old_character)
        if event["character"] != old_character:
            self._recompute_character_status(event["character"])
        self.save()
        return event

    def characters_brief(self) -> str:
        lines = []
        for c in self.data["characters"]:
            status = c.get("status", "alive")
            tag = f" [status: {status}]" if status != "alive" else ""
            lines.append(f"- {c['name']} ({c['role']}){tag}: {c['description']}")
        return "\n".join(lines) if lines else "(no established characters yet)"

    def world_brief(self) -> str:
        lines = [f"- {w['name']} [{w['category']}]: {w['content']}" for w in self.data["world"]]
        return "\n".join(lines) if lines else "(no established world-building yet)"

    def research_brief(self) -> str:
        lines = [f"- {n['topic']}: {n['content']}" for n in self.data["research_notes"]]
        return "\n".join(lines) if lines else "(no research notes carried over yet)"
