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

    def suggest_reveal(
        self,
        bible: StoryBible,
        character: dict,
        unlock_chapter_num: int,
        section: str | None = None,
        freeform: str = "",
    ) -> dict:
        """Drafts a plot-gated reveal for this character - a fact appropriate
        to surface once the given outline chapter is reached - for the writer
        to review/edit before saving. Uses that chapter's outline plus the
        character's established sections and already-planned reveals so it
        doesn't repeat or contradict anything."""
        entry = bible.outline_entry(unlock_chapter_num)
        chapter_context = ""
        if entry:
            chapter_context = (
                f"\nChapter {unlock_chapter_num} (\"{entry.get('title', '')}\") storyline: "
                f"{entry.get('summary', '')}\n{entry.get('outline', '')}"
            )

        existing = character.get("sections") or {}
        filled_lines = "\n".join(
            f"{self.SECTION_LABELS.get(k, k)}: {v}" for k, v in existing.items() if (v or "").strip()
        )

        reveals = sorted(character.get("reveals") or [], key=lambda r: r.get("unlock_chapter_num", 0))
        already_revealed = "\n".join(
            f"- (unlocks ch. {r.get('unlock_chapter_num')}) {r.get('text', '')}" for r in reveals
        )

        section_hint = f'\nThe writer wants this filed under the "{section}" section.' if section else ""
        freeform_hint = f'\nThe writer\'s guidance for this reveal: "{freeform}"' if freeform.strip() else ""

        prompt = f"""Character: {character.get('name', '')} ({character.get('role', '')})
Description: {character.get('description', '')}

Established structured sections (do not contradict):
{filled_lines or "(none yet)"}

Reveals already planned for this character (do not repeat or contradict these):
{already_revealed or "(none yet)"}
{chapter_context}
{section_hint}{freeform_hint}

Draft ONE new plot-gated reveal: a fact about this character that should only
become known to the reader once Chapter {unlock_chapter_num} is reached -
something that fits naturally with what's happening in that chapter's
storyline above, without contradicting the character's established sections
or other reveals above.
Respond with ONLY a single JSON object like:
{{"text": "...", "section": "..."}}
where "section" is one of {list(self.SECTION_LABELS)} (whichever section this
fact would eventually update) or "" if it's a general plot fact not tied to
one of those sections."""
        result = self.ask_json_object(prompt)
        return {
            "text": (result.get("text") or "").strip(),
            "section": (result.get("section") or "").strip(),
        }

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

    def resync_sections(
        self, bible: StoryBible, character: dict, new_facts: str, chapter_num: int | None = None
    ) -> dict:
        """Given a newly-established fact about this character (from a
        finalized chapter), drafts UPDATED text for whichever structured
        sections that fact actually touches - filled or still blank. For an
        already-filled section this folds the new fact into the existing
        text rather than discarding it; for a blank section it drafts fresh
        text from the fact alone. Returns a dict of only the sections that
        changed - the writer reviews/edits each one before saving, same as
        draft_sections.

        chapter_num is the source chapter's book position, not its drafting/
        finalization order - chapters are often finalized out of sequence, so
        a fact from chapter 5 finalized AFTER chapter 9 is not necessarily the
        character's most current state. When known, the prompt tells the AI
        where this fact falls so it can phrase the addition as "by chapter 5"
        rather than as the newest development, and avoid overwriting a
        chronologically-later detail that's already reflected in the
        section."""
        existing = character.get("sections") or {}
        filled = {k: v for k, v in existing.items() if (v or "").strip()}
        all_keys = list(self.SECTION_LABELS)

        filled_lines = "\n".join(f"{self.SECTION_LABELS[k]}: {v}" for k, v in filled.items())
        blank_labels = [self.SECTION_LABELS[k] for k in all_keys if k not in filled]

        position_note = ""
        if chapter_num is not None:
            position_note = f"""
This fact comes from Chapter {chapter_num}. Chapters are sometimes drafted and
finalized out of book order, so this is not necessarily the character's most
recent state - the sections above may already include details from a later
chapter (higher chapter number) that were finalized earlier. If a section
already reflects a later chapter, fold this fact in as something true "by
chapter {chapter_num}" without contradicting or erasing the later detail;
only treat this fact as the newest development if nothing already there
implies a later chapter."""

        prompt = f"""Character: {character.get('name', '')} ({character.get('role', '')})
Description: {character.get('description', '')}

Current structured sections:
{filled_lines or "(none filled in yet)"}

Still-blank sections: {", ".join(blank_labels) or "(none)"}

A newly finalized chapter established this new fact about the character:
\"\"\"
{new_facts}
\"\"\"
{position_note}

Decide which of these sections (filled or blank) this fact actually belongs
in (e.g. a new scar or outfit detail -> Appearance; a new bond or
falling-out -> Relationships; a revealed fear or want -> Goals /
Motivation). For an already-filled section it touches, rewrite that
section's full text folding the new fact in naturally alongside what's
already there - do not just append the raw fact. For a still-blank section
it touches, draft fresh text from the fact alone. Do not touch sections the
fact doesn't affect. If the fact doesn't clearly belong in any section,
respond with {{}}.
Respond with ONLY a JSON object using a subset of these keys: {all_keys},
like {{"{all_keys[0]}": "..."}}"""

        result = self.ask_json_object(prompt)
        return {k: result[k] for k in all_keys if isinstance(result.get(k), str) and result[k].strip()}
