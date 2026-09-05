"""Character Builder: creates the cast with distinct voices, goals, and arcs."""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.memory.story_bible import StoryBible

SYSTEM_PROMPT = """You are the Character Builder on a novel-writing team.
Your job is to design a cast of characters with distinct voices, concrete goals,
flaws, and arcs that create real conflict. Avoid cliches; give each character
at least one specific, non-obvious trait or contradiction. Always respond with
ONLY a JSON array of objects, each with keys "name", "role" (e.g. "protagonist",
"antagonist", "supporting"), and "description" (voice, goals, flaws, arc,
physical/behavioral details an author needs for consistency - 3-6 sentences)."""


class CharacterBuilderAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def build(self, bible: StoryBible) -> None:
        engine_brief = bible.story_engine_brief()
        engine_section = f"\n{engine_brief}\n" if engine_brief else ""
        prompt = f"""Book title: {bible.data['title']}
Genre: {bible.data['genre']}
Premise: {bible.data['premise']}
{engine_section}
World-building so far:
{bible.world_brief()}

Characters already established (from this book or earlier books in the
series, if any) - do not duplicate these; only add genuinely new characters
this book needs (new antagonist, new supporting cast, etc.):
{bible.characters_brief()}

Create 4-7 NEW characters this book needs (if this continues a series and the
existing cast already covers protagonist/antagonist, focus on new supporting
characters or a new threat) with distinct voices and concrete arcs.
Respond with ONLY a JSON array like:
[{{"name": "...", "role": "...", "description": "..."}}, ...]"""

        characters = self.ask_json(prompt)
        for c in characters:
            bible.add_character(c["name"], c["role"], c["description"])

    def suggest_one(self, bible: StoryBible, freeform: str) -> dict:
        """Drafts a single new character from a freeform description, for the
        writer to review/edit before saving - used by the "describe it, let
        AI fill the fields" New Character flow."""
        engine_brief = bible.story_engine_brief()
        engine_section = f"\n{engine_brief}\n" if engine_brief else ""
        prompt = f"""Book title: {bible.data['title']}
Genre: {bible.data['genre']}
Premise: {bible.data['premise']}
{engine_section}
Characters already established - do not duplicate one of these:
{bible.characters_brief()}

The writer's description of the new character:
\"\"\"
{freeform}
\"\"\"

Draft this ONE character with a distinct voice and concrete arc, filling in
only what's needed to make the entry usable without contradicting what the
writer wrote. Respond with ONLY a single JSON object like:
{{"name": "...", "role": "...", "description": "..."}}"""
        result = self.ask_json(prompt)
        if isinstance(result, list):
            result = result[0]
        return result

    SECTION_LABELS = {
        "appearance": "Appearance",
        "personality": "Personality",
        "background": "Background",
        "goals_motivation": "Goals / Motivation",
        "relationships": "Relationships",
        "arc": "Arc",
    }

    def draft_sections(self, bible: StoryBible, character: dict) -> dict:
        """Drafts only the currently-empty structured sections (appearance,
        personality, background, goals/motivation, relationships, arc) for
        a character, using its description and any already-filled sections
        as context so it doesn't contradict them. Returns a dict of just the
        newly-drafted sections - the writer reviews/edits before saving."""
        existing = character.get("sections") or {}
        missing = [k for k in StoryBible.CHARACTER_SECTION_KEYS if not (existing.get(k) or "").strip()]
        if not missing:
            return {}

        filled_lines = "\n".join(
            f"{self.SECTION_LABELS[k]}: {v}" for k, v in existing.items() if (v or "").strip()
        )
        missing_labels = [self.SECTION_LABELS[k] for k in missing]

        prompt = f"""Book title: {bible.data['title']}
Genre: {bible.data['genre']}

Character: {character.get('name', '')} ({character.get('role', '')})
Description: {character.get('description', '')}

Already-filled sections (stay consistent with these, do not contradict):
{filled_lines or "(none yet)"}

Draft ONLY these missing sections: {", ".join(missing_labels)}.
Keep each section concrete and specific (2-4 sentences), consistent with the
description and any already-filled sections above.
Respond with ONLY a single JSON object with exactly these keys: {missing},
like {{"{missing[0]}": "..."}}"""

        result = self.ask_json_object(prompt)
        return {k: result[k] for k in missing if isinstance(result.get(k), str) and result[k].strip()}

    def resync_sections(self, bible: StoryBible, character: dict, new_facts: str) -> dict:
        """Given a newly-established fact about this character (from a
        finalized chapter), drafts UPDATED text for whichever already-filled
        structured sections that fact actually touches - unlike
        draft_sections, this can overwrite non-empty sections, folding the
        new fact into the existing text rather than just filling blanks.
        Returns a dict of only the sections that changed - the writer
        reviews/edits each one before saving, same as draft_sections."""
        existing = character.get("sections") or {}
        filled = {k: v for k, v in existing.items() if (v or "").strip()}
        if not filled:
            return {}

        filled_lines = "\n".join(f"{self.SECTION_LABELS[k]}: {v}" for k, v in filled.items())

        prompt = f"""Character: {character.get('name', '')} ({character.get('role', '')})
Description: {character.get('description', '')}

Current structured sections:
{filled_lines}

A newly finalized chapter established this new fact about the character:
\"\"\"
{new_facts}
\"\"\"

Decide which of the sections above (if any) this fact actually belongs in
(e.g. a new scar or outfit detail -> Appearance; a new bond or falling-out
-> Relationships; a revealed fear or want -> Goals / Motivation). For each
section it touches, rewrite that section's full text folding the new fact
in naturally alongside what's already there - do not just append the raw
fact, and do not touch sections the new fact doesn't affect. If the fact
doesn't clearly belong in any of these sections, respond with {{}}.
Respond with ONLY a JSON object using a subset of these keys: {list(filled)},
like {{"{next(iter(filled))}": "..."}}"""

        result = self.ask_json_object(prompt)
        return {k: result[k] for k in filled if isinstance(result.get(k), str) and result[k].strip()}
