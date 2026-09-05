"""Continuity Checker: scans the rest of the story bible - drafted chapters,
outline entries, characters, and world entries - for consequences of a change
made elsewhere (a character/world/story-engine edit, a chapter revision, or a
whole-outline revision), so the writer can review and selectively act on them
instead of hunting through the manuscript by hand.

Retrieval-gated (BM25 over `change_summary`, same machinery as author.py's
draft-time continuity lookups) rather than dumping the entire bible into the
prompt - a book-wide un-gated version scaled with total book size on every
manual check, regardless of how localized the actual change was.

This only flags - it never rewrites anything itself. Flagged items feed into
the same review queue used for multi-task universal-prompt instructions, so
each flagged chapter/outline entry/character/world entry still goes through
its own revise/review/save step.
"""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.memory.retriever import build_index, named_entity_hits
from ghostwriter.memory.story_bible import StoryBible

SYSTEM_PROMPT = """You are a continuity editor for a novel-writing tool.
Given a description of something that just changed in the story bible and a
summary of the rest of the book (outline, drafted chapters, characters,
world entries), find which of those items are now inconsistent with the
change (contradict a fact, reference an outdated trait/name/rule, or should
logically be updated to reflect it) and explain what needs to change in
each. Only flag items that are ACTUALLY affected - most items in a long book
are unrelated to any given change, and it is correct, expected, and
desirable to return an empty list for those. Always respond with ONLY a
JSON array."""

# How much of each character/world description to show the model - enough to
# catch contradictions without ballooning the prompt on a long book.
ENTITY_EXCERPT_CHARS = 400

# How many docs the BM25 index returns across all types combined (chapters,
# outline, scenes, characters, world, notes, timeline) - generous enough to
# cover a real cross-section of the book without reverting to a full dump.
RETRIEVAL_TOP_K = 40


class ContinuityCheckerAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def check(self, bible: StoryBible, change_summary: str) -> list[dict]:
        entries = sorted(bible.data.get("outline", []), key=lambda e: e["chapter_num"])
        chapters_by_num = {c["chapter_num"]: c for c in bible.data.get("chapters", [])}

        index = build_index(bible, include_outline=True)
        forced = named_entity_hits(bible, change_summary)
        ranked = index.search(change_summary, top_k=RETRIEVAL_TOP_K)

        seen: set[str] = set()
        combined = []
        for doc in forced:
            if doc.doc_id not in seen:
                seen.add(doc.doc_id)
                combined.append(doc)
        for doc, _score in ranked:
            if doc.doc_id not in seen:
                seen.add(doc.doc_id)
                combined.append(doc)

        by_source: dict[str, list[str]] = {}
        for doc in combined:
            by_source.setdefault(doc.source, []).append(doc.text)

        def section(source: str, truncate: int | None = None) -> str:
            lines = by_source.get(source)
            if not lines:
                return "(none retrieved as relevant to this change)"
            if truncate:
                lines = [l if len(l) <= truncate else l[:truncate] + "..." for l in lines]
            return "\n".join(lines)

        drafted_section = section("chapter_summary")
        outline_section = section("outline")
        characters_section = section("character", truncate=ENTITY_EXCERPT_CHARS)
        world_section = section("world", truncate=ENTITY_EXCERPT_CHARS)
        notes_section = section("research", truncate=ENTITY_EXCERPT_CHARS)
        timeline_section = section("timeline", truncate=ENTITY_EXCERPT_CHARS)
        scenes_section = section("scene")

        prompt = f"""Book: {bible.data['title']} ({bible.data['genre']})

What just changed:
\"\"\"
{change_summary}
\"\"\"

Already-drafted/approved chapters (existing prose - flag if this change
contradicts what's already written):
{drafted_section}

Not-yet-drafted outline entries (flag if this change means the planned
chapter should be updated before it's drafted):
{outline_section}

Characters (flag if this change contradicts or should update a character's
description - e.g. a plot change alters their arc, a renamed faction appears
in their backstory):
{characters_section}

World entries (flag if this change contradicts or should update a
location/faction/rule description):
{world_section}

Research notes (flag if this change contradicts or makes a note outdated -
e.g. a fact the note records no longer holds):
{notes_section}

Timeline events (flag if this change contradicts an event's date/order, or
means an event's description is now outdated):
{timeline_section}

Planned/drafted scenes within chapters (flag if this change means a scene's
beats now contradict another scene, e.g. a repeated reveal or a broken
transition):
{scenes_section}

Respond with ONLY a JSON array, one object per AFFECTED item (empty array
if nothing is affected). Each object has:
- "kind": "chapter" (drafted prose), "outline" (undrafted plan),
  "character", "world", "note", "timeline", or "scene" (a planned/drafted
  scene card within a chapter, for cross-scene contradictions)
- "chapter_num": <int> for kind chapter/outline/scene, null otherwise
- "scene_num": <int> for kind scene, null otherwise
- "target_name": the exact character/world entry/note/timeline event name for
  kind character/world/note/timeline, null otherwise
- "issue": what's now inconsistent
- "instruction": a concrete instruction for revising this item to fix it"""

        result = self.ask_json(prompt)
        if isinstance(result, dict):
            result = [result]
        if not isinstance(result, list):
            return []

        valid_nums = {e["chapter_num"] for e in entries}
        names = {
            "character": {(c.get("name") or "").lower(): c.get("name") for c in bible.data.get("characters", [])},
            "world": {(w.get("name") or "").lower(): w.get("name") for w in bible.data.get("world", [])},
            "note": {
                (n.get("name") or n.get("topic") or "").lower(): (n.get("name") or n.get("topic"))
                for n in bible.data.get("research_notes", [])
            },
            "timeline": {(t.get("name") or "").lower(): t.get("name") for t in bible.data.get("timeline", [])},
        }

        flags = []
        for item in result:
            if not isinstance(item, dict):
                continue
            issue = item.get("issue")
            if not isinstance(issue, str) or not issue.strip():
                continue
            instruction = item.get("instruction")
            instruction = instruction.strip() if isinstance(instruction, str) and instruction.strip() else issue.strip()

            kind = item.get("kind")
            if kind in ("chapter", "outline"):
                chapter_num = item.get("chapter_num")
                if not isinstance(chapter_num, int) or chapter_num not in valid_nums:
                    continue
                ch = chapters_by_num.get(chapter_num)
                drafted = bool(ch and (ch.get("history") or ch.get("draft")))
                flags.append({
                    "kind": "chapter" if drafted else "outline",
                    "chapter_num": chapter_num,
                    "target_name": None,
                    "drafted": drafted,
                    "issue": issue.strip(),
                    "instruction": instruction,
                })
            elif kind == "scene":
                chapter_num = item.get("chapter_num")
                scene_num = item.get("scene_num")
                if not isinstance(chapter_num, int) or chapter_num not in valid_nums:
                    continue
                entry = next((e for e in entries if e["chapter_num"] == chapter_num), None)
                scene_nums = {s["scene_num"] for s in (entry.get("scenes") or [])} if entry else set()
                if not isinstance(scene_num, int) or scene_num not in scene_nums:
                    continue
                flags.append({
                    "kind": "scene",
                    "chapter_num": chapter_num,
                    "scene_num": scene_num,
                    "target_name": None,
                    "drafted": False,
                    "issue": issue.strip(),
                    "instruction": instruction,
                })
            elif kind in ("character", "world", "note", "timeline"):
                raw_name = item.get("target_name")
                canonical = names[kind].get(raw_name.strip().lower()) if isinstance(raw_name, str) else None
                if not canonical:
                    continue
                flags.append({
                    "kind": kind,
                    "chapter_num": None,
                    "target_name": canonical,
                    "drafted": False,
                    "issue": issue.strip(),
                    "instruction": instruction,
                })
        return flags
