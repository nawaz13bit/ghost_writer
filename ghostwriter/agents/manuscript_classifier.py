"""Classifies raw text blocks pulled from an imported manuscript file as
finished prose, an outline/synopsis note, character notes, worldbuilding
notes, or miscellaneous notes.

Deliberately never asked to reproduce or rewrite the block text itself -
only to label it - so a small local model can't silently mangle the
writer's actual prose. The importer slices the original text by block_id
after classification.
"""
from __future__ import annotations

from ghostwriter.agents.base import Agent

SYSTEM_PROMPT = """You are a manuscript triage assistant helping a writer
import their own raw files into a book-writing tool. You will see a numbered
list of short previews of text blocks extracted from one of the writer's
files. For each block, decide what kind of content it is:

- "prose": finished (or near-finished) narrative chapter text meant to be
  read as-is.
- "outline": a chapter plan, summary, or synopsis describing what
  happens/will happen - not full prose.
- "character": notes describing a specific character - personality,
  backstory, appearance, relationships, arc.
- "world": notes describing a setting, location, faction, object, rule of
  the world, or other lore - not tied to one character.
- "notes": anything else - freeform brainstorming, todos, title ideas, or a
  fragment you can't confidently classify.

Also guess a chapter_num (integer) if the block is clearly tied to a
specific chapter, and a short title if one is evident; use null for either
when you can't tell. For "character" and "world" blocks, also guess
entity_name: the specific character's name, or the place/faction/thing's
name - use null if the block doesn't clearly center on one named entity
(e.g. mixed notes about several things). Never invent chapter numbers,
titles, or entity names that aren't implied by the block itself. Always
respond with ONLY a JSON array, one object per block, using the same
block_id you were given."""


class ManuscriptClassifierAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def classify(self, file_name: str, blocks: list[dict]) -> dict[int, dict]:
        lines = []
        for i, b in enumerate(blocks):
            preview = " ".join(b["text"].split())[:220]
            heading = b["heading"] or "(no heading)"
            lines.append(f'{i}. heading: "{heading}" | preview: "{preview}"')

        prompt = f"""File: {file_name}
Below are text blocks extracted from this file, in order. Classify each one.

{chr(10).join(lines)}

Respond with ONLY a JSON array like:
[{{"block_id": 0, "type": "prose", "chapter_num": 3, "title": "The Storm", "entity_name": null}}, ...]
"type" must be "prose", "outline", "character", "world", or "notes".
chapter_num, title, and entity_name should be null when you can't tell."""

        try:
            result = self.ask_json(prompt)
        except ValueError:
            return {}

        by_id: dict[int, dict] = {}
        if not isinstance(result, list):
            return by_id
        valid_types = ("prose", "outline", "character", "world", "notes")
        for item in result:
            if not isinstance(item, dict):
                continue
            try:
                bid = int(item["block_id"])
            except (KeyError, TypeError, ValueError):
                continue
            kind = item.get("type") if item.get("type") in valid_types else "notes"
            raw_num = item.get("chapter_num")
            chapter_num = int(raw_num) if isinstance(raw_num, (int, float)) and not isinstance(raw_num, bool) else None
            raw_title = item.get("title")
            title = raw_title.strip() if isinstance(raw_title, str) and raw_title.strip() else None
            raw_entity = item.get("entity_name")
            entity_name = raw_entity.strip() if isinstance(raw_entity, str) and raw_entity.strip() else None
            by_id[bid] = {
                "type": kind,
                "chapter_num": chapter_num,
                "title": title,
                "entity_name": entity_name,
            }
        return by_id
