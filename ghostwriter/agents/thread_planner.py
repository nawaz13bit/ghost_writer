"""Thread Planner: looks FORWARD from a just-finished chapter to the next few
outline entries and proposes seed/foreshadowing notes the current chapter
should plant so an upcoming chapter can pay them off. Complements the Timeline
Extractor and Bible Manager, which only look backward at what already
happened.

This agent only PROPOSES - it never writes to the bible itself. Proposals
become ordinary idea-backlog entries (category="planted_thread") through the
same create/review queue as every other AI change.

It does not resolve ideas - an idea is only ever marked resolved when the
writer explicitly integrates it into a chapter/outline entry and that entry
is finalized (see pendingIdeaIntegration in editor.js).
"""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.memory.story_bible import StoryBible

LOOKAHEAD_CHAPTERS = 3

SYSTEM_PROMPT = """You are the Thread Planner on a novel-writing team. You
read a finished chapter along with summaries of the next few upcoming
chapters, and suggest 0-3 small setups the just-finished chapter could plant
(a detail, a line of dialogue, an object, a passing mention) so that one of
those upcoming chapters can pay it off with proper foreshadowing instead of
introducing it cold.

Only propose a thread if it would genuinely improve foreshadowing - it is
completely fine, and often correct, to propose nothing. Do not propose
anything the chapter already plants. Always respond with ONLY a JSON array
of objects, each with keys:
- "note": 1-2 sentences describing what to plant and why, written as an
  instruction to the author (e.g. "Have Mira notice the locked drawer in
  passing - she'll need to have seen it before chapter 9's break-in")
- "seeds_chapter_num": the chapter number (from the list of upcoming
  chapters given) this plants toward
- "characters": array of character names (from the existing character
  bible) this note is about, if any - empty array if none apply

Respond with an empty array [] if no forward-planting is warranted."""


class ThreadPlannerAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def propose_from_chapter(self, bible: StoryBible, chapter_num: int, text: str) -> list[dict]:
        """Returns a list of proposed planted-thread notes for the writer to
        review - does not touch the bible. Each item: {note, characters,
        linked_kind: "outline", linked_id}."""
        # Skip chapters that already have a planted-thread seed, resolved or
        # not - without this, re-running a finalize/book-sweep over the same
        # chapters (the book sweep in particular re-walks every finalized
        # chapter each time it's run) keeps re-proposing plants for a chapter
        # whose earlier plant was already resolved, piling up duplicate ideas.
        already_seeded = {
            i["linked_id"] for i in bible.data["ideas"]
            if i.get("category") == "planted_thread"
            and i.get("linked_kind") == "outline"
        }
        upcoming = [
            e for e in sorted(bible.data["outline"], key=lambda o: o["chapter_num"])
            if e["chapter_num"] > chapter_num and e["chapter_num"] not in already_seeded
        ][:LOOKAHEAD_CHAPTERS]
        if not upcoming:
            return []

        upcoming_brief = "\n".join(
            f"- Chapter {e['chapter_num']}{': ' + e['title'] if e.get('title') else ''}: "
            f"{e.get('summary') or '(no summary yet)'}"
            for e in upcoming
        )

        # The character bible is batched (see Agent.ask_json_batched) for the
        # same reason as bible_manager/timeline_extractor: it's the one piece
        # here that grows unboundedly over a long project, while upcoming_brief
        # stays small (LOOKAHEAD_CHAPTERS). Suggestions are naturally scoped to
        # whichever characters are in the current batch, so cross-batch
        # duplicates are rarer than in timeline_extractor - still deduped by
        # exact note text below as a safety net.
        character_entries = bible.characters_brief().split("\n")

        def build_prompt(character_chunk: str) -> str:
            return f"""Existing character bible (a portion of it, for matching "characters" names):
{character_chunk}

Upcoming chapters (what's coming next - only these are valid seeds_chapter_num values):
{upcoming_brief}

--- CHAPTER {chapter_num} (just finished) ---
{text}
--- END CHAPTER ---

Suggest 0-3 setups this chapter should plant for one of the upcoming
chapters above. Respond with ONLY a JSON array like:
[{{"note": "...", "seeds_chapter_num": <int>, "characters": ["..."]}}, ...]"""

        proposals_raw = self.ask_json_batched(character_entries, build_prompt)

        valid_chapter_nums = {e["chapter_num"] for e in upcoming}
        known_characters = {c["name"].lower(): c["name"] for c in bible.data["characters"]}
        seen_notes: set[str] = set()
        proposals = []
        for p in proposals_raw:
            if not isinstance(p, dict):
                continue
            note = p.get("note")
            if not isinstance(note, str) or not note.strip():
                continue
            seeds_chapter_num = p.get("seeds_chapter_num")
            if not isinstance(seeds_chapter_num, int) or seeds_chapter_num not in valid_chapter_nums:
                continue
            if note.strip().lower() in seen_notes:
                continue
            seen_notes.add(note.strip().lower())

            characters = []
            raw_chars = p.get("characters")
            if isinstance(raw_chars, list):
                for v in raw_chars:
                    if isinstance(v, str) and v.strip().lower() in known_characters:
                        characters.append(known_characters[v.strip().lower()])

            proposals.append({
                "note": note.strip(),
                "characters": characters,
                "linked_kind": "outline",
                "linked_id": seeds_chapter_num,
            })
        return proposals

    # Resolution used to be auto-detected here (an LLM judgment call on
    # whether a chapter's text "paid off" an open planted thread), but that
    # guess had no link back to any real writer action and mass-resolved
    # unrelated ideas across a whole book. Removed - an idea is now only
    # marked resolved when the writer explicitly integrates it into a
    # chapter/outline entry and that entry is finalized (see
    # pendingIdeaIntegration in editor.js).
