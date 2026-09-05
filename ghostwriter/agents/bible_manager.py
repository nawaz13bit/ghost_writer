"""Bible Manager: figures out what the character and world bibles should
learn from a finished chapter. Runs a single LLM pass over the chapter,
comparing it against BOTH bibles at once so one pass can correctly route a
new fact to the right place - e.g. telling a named character apart from the
faction/culture/species they belong to, instead of two independent agents
each guessing from only half the picture.

This agent only PROPOSES - it never writes to the bible itself. Proposals
go through the same revise/create review queue as every other AI change, so
the writer approves (or edits, or skips) each one before it lands.
"""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.memory.story_bible import StoryBible

SYSTEM_PROMPT = """You are the Bible Manager on a novel-writing team. You
read a finished chapter and compare it against the existing character bible
and world bible. Identify only NEW, concrete, established facts and sort
each into exactly one of three buckets:

- "character": a brand-new named individual who appears, or a new
  physical/behavioral/backstory/relationship detail for an EXISTING named
  individual character. Not groups, not species, not organizations - only a
  specific person/individual being.
- "faction": a group identity - a species, race, culture, tribe, order,
  organization, government, guild, or other collective the characters
  belong to or are shaped by. If several characters share a species or
  affiliation, that shared identity belongs here, not under any one of
  them as a character fact.
- "world": everything else about the setting - locations, geography,
  history/timeline, technology, magic/physics rules. Not groups of people.

Do not restate facts already in either bible. Always respond with ONLY a
JSON array of objects, each with keys:
- "bucket": "character", "faction", or "world"
- "name": the character/faction/place name
- "role": only for bucket "character" - reuse the existing role for an
  existing character, or a short role like "supporting" for a new one
- "new_facts": 1-3 sentences of NEW information only

Respond with an empty array [] if nothing new was established."""


class BibleManagerAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def propose_from_chapter(self, bible: StoryBible, chapter_num: int, text: str) -> list[dict]:
        """Returns a list of proposed bible updates for the writer to review -
        does not touch the bible. Each item: {bucket, name, exists,
        target_name (canonical existing name, only if exists), role_or_category,
        new_facts}."""
        prompt = f"""Existing character bible:
{bible.characters_brief()}

Existing world bible (includes factions/cultures/species, filed under their
own category such as "faction" or "culture", as well as places/history/
rules under other categories):
{bible.world_brief()}

--- CHAPTER {chapter_num} ---
{text}
--- END CHAPTER ---

List any new characters, new facts about existing characters, new
factions/cultures/species, or new world-building facts established in this
chapter. Respond with ONLY a JSON array like:
[{{"bucket": "character", "name": "...", "role": "...", "new_facts": "..."}}, ...]"""
        updates = self.ask_json(prompt)
        if isinstance(updates, dict):
            updates = [updates]
        if not isinstance(updates, list):
            return []

        proposals = []
        for u in updates:
            if not isinstance(u, dict):
                continue
            bucket = u.get("bucket")
            if bucket not in ("character", "faction", "world"):
                continue
            name = u.get("name")
            new_facts = u.get("new_facts")
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(new_facts, str) or not new_facts.strip():
                continue
            name = name.strip()

            if bucket == "character":
                existing = bible.find_character(name)
                role_or_category = (u.get("role") or "").strip() or "supporting"
            else:
                existing = bible.find_world_entry(name)
                role_or_category = "faction" if bucket == "faction" else "misc"

            proposals.append({
                "bucket": bucket,
                "name": name,
                "exists": existing is not None,
                "target_name": existing.get("name") if existing else None,
                "role_or_category": role_or_category,
                "new_facts": new_facts.strip(),
            })
        return proposals
