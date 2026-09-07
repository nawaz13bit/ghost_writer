"""Timeline Extractor: figures out what new plot events a finished chapter
establishes, as structured timeline entries rather than prose appended to a
character's description. Runs alongside BibleManagerAgent at chapter finalize
so the Timeline stays a queryable, per-character/per-world-entry event
ledger instead of falling behind unless a writer remembers to log events by
hand.

This agent only PROPOSES - it never writes to the bible itself. Proposals go
through the same create/review queue as every other AI change.
"""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.memory.story_bible import StoryBible

SYSTEM_PROMPT = """You are the Timeline Keeper on a novel-writing team. You
read a finished chapter and identify NEW, concrete plot events it
establishes - things that happened, not facts about who someone is (that's
the Bible Manager's job). A good timeline event is something you could point
to on a chronology: a death, an arrival, a discovery, a battle, a betrayal, a
decision with lasting consequences - not scenery or mood.

Do not restate events already in the existing timeline. Skip minor beats
that don't matter to later chapters. Always respond with ONLY a JSON array
of objects, each with keys:
- "name": a short title for the event, e.g. "The Siege of Kell"
- "story_date": the in-world date/time if the text gives one, else ""
- "description": 1-2 sentences describing what happened
- "characters": array of character names (from the existing character
  bible) directly involved in this event - empty array if none are named
  characters
- "locations": array of world-entry names (factions, locations, objects,
  or items from the existing world bible) central to this event - empty
  array if none apply

Respond with an empty array [] if this chapter establishes no new
timeline-worthy event."""


class TimelineExtractorAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def propose_from_chapter(self, bible: StoryBible, chapter_num: int, text: str) -> list[dict]:
        """Returns a list of proposed new timeline events for the writer to
        review - does not touch the bible. Each item: {name, story_date,
        description, characters, chapter_num}.

        The character/world/timeline reference dump is batched (see
        Agent.ask_json_batched) instead of sent whole in one prompt, for the
        same reason as bible_manager: on a long-running project it can grow
        past the model's context window on its own. Unlike bible_manager's
        facts, an event found in the chapter doesn't depend on which bible
        entries happen to share its batch, so the *same* event is expected
        to come back from every batch - proposals are deduped by name below
        rather than merged/concatenated."""
        entries = bible.characters_brief().split("\n") + bible.world_brief().split("\n") + bible.timeline_brief().split("\n")

        def build_prompt(bible_chunk: str) -> str:
            return f"""Existing character/world/timeline reference (a portion of it, for
matching "characters"/"locations" names and avoiding already-logged events):
{bible_chunk}

--- CHAPTER {chapter_num} ---
{text}
--- END CHAPTER ---

List any new plot-significant events established in this chapter. Respond
with ONLY a JSON array like:
[{{"name": "...", "story_date": "...", "description": "...", "characters": ["..."], "locations": ["..."]}}, ...]"""

        events = self.ask_json_batched(entries, build_prompt)

        known_characters = {c["name"].lower(): c["name"] for c in bible.data["characters"]}
        known_world = {w["name"].lower(): w["name"] for w in bible.data["world"]}
        seen_names: set[str] = set()
        proposals = []
        for e in events:
            if not isinstance(e, dict):
                continue
            name = e.get("name")
            description = e.get("description")
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(description, str) or not description.strip():
                continue
            if name.strip().lower() in seen_names:
                continue
            seen_names.add(name.strip().lower())

            def _match(raw, known):
                out = []
                if isinstance(raw, list):
                    for v in raw:
                        if isinstance(v, str) and v.strip().lower() in known:
                            out.append(known[v.strip().lower()])
                return out

            characters = _match(e.get("characters"), known_characters)
            locations = _match(e.get("locations"), known_world)

            proposals.append({
                "name": name.strip(),
                "story_date": (e.get("story_date") or "").strip() if isinstance(e.get("story_date"), str) else "",
                "description": description.strip(),
                "characters": characters,
                "locations": locations,
                "chapter_num": chapter_num,
            })
        return proposals
