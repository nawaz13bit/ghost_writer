"""Outliner: builds the chapter-by-chapter plot structure."""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.memory.story_bible import StoryBible

SYSTEM_PROMPT = """You are the Outliner on a novel-writing team.
Your job is to design a chapter-by-chapter plot with rising tension, clear
turning points, and a satisfying structure across however many acts the
request specifies. Each chapter should end on a hook or meaningful beat, and
should be scoped to fit tightly within its word budget - no padding, every
beat earns its place. Always respond with ONLY a JSON array of objects, each
with keys "chapter_num" (int, starting at 1), "act" (e.g. "Act 1"), "title",
"pov" (character name), "characters" (array of every established character
name, from the cast given, who appears on the page in this chapter -
include the POV character too), "summary" (2-3 sentences: a short logline of
the chapter), and "outline" (a freeform beat-by-beat outline of what happens
in the chapter, from open to close - however many beats and however long it
needs to be for this chapter; make the central conflict/turn feel propulsive
and intense, with concrete stakes, and land on a hook, reveal, or turn that
pulls the reader into the next chapter)."""


class OutlinerAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def build(
        self,
        bible: StoryBible,
        num_chapters: int,
        num_acts: int = 3,
        words_per_chapter: int | None = None,
        voice_prompt: str | None = None,
    ) -> None:
        system_prompt = f"{voice_prompt}\n\n{self.system_prompt}" if voice_prompt else self.system_prompt
        engine_brief = bible.story_engine_brief()
        engine_section = f"\n{engine_brief}\n" if engine_brief else ""
        act_label = "three-act" if num_acts == 3 else f"{num_acts}-act"
        chapters_per_act = num_chapters // num_acts
        pacing_section = (
            f"\nEach chapter will be drafted to roughly {words_per_chapter} words - scope each chapter's "
            f"outline to fit that tightly, so pacing stays fast and every chapter feels action-packed "
            f"rather than padded.\n"
            if words_per_chapter else ""
        )
        prompt = f"""Book title: {bible.data['title']}
Genre: {bible.data['genre']}
Premise: {bible.data['premise']}
{engine_section}
World-building:
{bible.world_brief()}

Characters:
{bible.characters_brief()}

Recap of earlier books in this series (if any - do not contradict these events):
{bible.data.get("series_recap") or "(This is not part of a series.)"}
{pacing_section}
Design a {num_chapters}-chapter outline with a coherent {act_label} structure
(roughly {chapters_per_act} chapters per act - label each chapter's "act" field
"Act 1", "Act 2", etc. up through "Act {num_acts}").
If this book continues a series, open in a way consistent with the recap above
rather than re-introducing already-established characters/world from scratch.
Respond with ONLY a JSON array of exactly {num_chapters} objects like:
[{{"chapter_num": 1, "act": "Act 1", "title": "...", "pov": "...", "summary": "...",
"outline": "..."}}, ...]"""

        outline = self.ask_json_list(prompt, required_key="chapter_num", system_prompt=system_prompt)
        outline.sort(key=lambda c: c["chapter_num"])
        bible.set_outline(outline)

    def regenerate_chapter(
        self,
        bible: StoryBible,
        chapter_num: int,
        extra_instruction: str | None = None,
        final_text: str | None = None,
        voice_prompt: str | None = None,
    ) -> dict:
        """Regenerates a single outline entry's title/act/summary in place,
        keeping it consistent with the rest of the outline and with whatever
        has already been drafted/approved so it doesn't retroactively
        contradict finished work. An optional extra_instruction (e.g. a
        flagged continuity issue elsewhere in the bible) is folded into the
        regeneration so the entry accounts for it.

        If final_text is given (the chapter's actual finalized prose, e.g.
        after a hand-edit), the entry is reconciled to describe what the
        text actually does rather than regenerated from outline context
        alone - this is the "sync outline to the finalized chapter" path."""
        system_prompt = f"{voice_prompt}\n\n{self.system_prompt}" if voice_prompt else self.system_prompt
        entry = bible.outline_entry(chapter_num)
        if entry is None:
            raise ValueError(f"No outline entry for chapter {chapter_num}")

        engine_brief = bible.story_engine_brief()
        engine_section = f"\n{engine_brief}\n" if engine_brief else ""
        continuity_brief = bible.continuity_and_progress_brief(exclude_chapter_num=chapter_num)
        continuity_section = f"\n{continuity_brief}\n" if continuity_brief else ""
        other_entries = [e for e in bible.data["outline"] if e["chapter_num"] != chapter_num]
        other_entries.sort(key=lambda e: e["chapter_num"])
        rest_of_outline = "\n".join(
            f"Chapter {e['chapter_num']} ({e.get('act', 'no act')}) - {e.get('title', '')}: {e.get('summary', '')}"
            for e in other_entries
        ) or "(No other outline entries yet.)"

        extra_section = f"\nSpecifically, this needs to change: {extra_instruction}\n" if extra_instruction else ""
        words_per_chapter = bible.data.get("chapter_target_words")
        pacing_section = (
            f"\nThis chapter will be drafted to roughly {words_per_chapter} words - scope the outline "
            f"to fit that tightly and keep it propulsive.\n" if words_per_chapter else ""
        )

        if final_text:
            text_section = f"\nThe chapter's actual finalized text (this is the source of truth - describe what THIS says, not what was originally planned):\n\"\"\"\n{final_text}\n\"\"\"\n"
            task_line = (
                f"Rewrite ONLY Chapter {chapter_num}'s outline entry so it accurately describes the finalized "
                f"text above, since the text was hand-edited and may no longer match the old outline entry "
                f"(currently titled \"{entry.get('title', '')}\" in {entry.get('act', 'no act')}, "
                f"summarized as: {entry.get('summary', '')})."
            )
        else:
            text_section = ""
            task_line = (
                f"Regenerate ONLY Chapter {chapter_num}, currently titled \"{entry.get('title', '')}\"\n"
                f"in {entry.get('act', 'no act')}, currently summarized as: {entry.get('summary', '')}"
            )

        prompt = f"""Book: {bible.data['title']} ({bible.data['genre']})
Premise: {bible.data['premise']}
{engine_section}
Rest of the outline (for context - keep the regenerated chapter consistent with this):
{rest_of_outline}
{continuity_section}{text_section}
{task_line}
{extra_section}{pacing_section}
Keep its chapter_num ({chapter_num}), title ("{entry.get('title', '')}"), and act
({entry.get('act', 'no act')}) unless the text/outline clearly no longer matches
them. Do not contradict chapters already drafted/approved or the open continuity
issues above (if any).
Respond with ONLY a single JSON object like:
{{"chapter_num": {chapter_num}, "act": "...", "title": "...", "pov": "...",
"characters": ["every established character name who appears on the page"],
"summary": "...", "outline": "..."}}"""

        result = self.ask_json_object(prompt, system_prompt=system_prompt)
        result["chapter_num"] = chapter_num
        return bible.update_outline_entry(
            chapter_num,
            title=result.get("title", entry.get("title", "")),
            act=result.get("act", entry.get("act")),
            summary=result.get("summary", entry.get("summary", "")),
            outline=result.get("outline", entry.get("outline", "")),
            characters=result.get("characters", entry.get("characters", [])),
        )

    def elaborate_act_summary(
        self,
        bible: StoryBible,
        act_name: str,
        extra_instruction: str | None = None,
        voice_prompt: str | None = None,
    ) -> str:
        """Expands an act's freeform summary (what happens across this act,
        before it's broken into individual chapter outlines) using the act's
        own chapter outline entries as context, so the result stays
        consistent with what's already planned rather than inventing a new
        throughline."""
        system_prompt = f"{voice_prompt}\n\n{self.system_prompt}" if voice_prompt else self.system_prompt
        entry = bible.act_entry(act_name) or {}
        current_summary = entry.get("summary", "")
        engine_brief = bible.story_engine_brief()
        engine_section = f"\n{engine_brief}\n" if engine_brief else ""
        chapters = sorted(
            (e for e in bible.data.get("outline", []) if (e.get("act") or "Unassigned act") == act_name),
            key=lambda e: e["chapter_num"],
        )
        chapters_brief = "\n".join(
            f"Chapter {e['chapter_num']} - {e.get('title', '')}: {e.get('summary', '')}"
            for e in chapters
        ) or "(No chapters outlined in this act yet.)"
        extra_section = f"\nSpecifically, incorporate this: {extra_instruction}\n" if extra_instruction else ""

        prompt = f"""Book: {bible.data['title']} ({bible.data['genre']})
Premise: {bible.data['premise']}
{engine_section}
{act_name}'s chapters:
{chapters_brief}

Current act summary (may be brief or empty): {current_summary or "(none yet)"}
{extra_section}
Write an elaborated summary of {act_name} as a whole (3-6 sentences): the
throughline connecting its chapters, the arc it completes, and how it sets
up the next act. Stay consistent with the chapter summaries above - describe
what's already planned, don't invent new plot.
Respond with ONLY a single JSON object like: {{"summary": "..."}}"""

        result = self.ask_json_object(prompt, system_prompt=system_prompt)
        return result.get("summary", current_summary)

    def suggest_new_entry(
        self, bible: StoryBible, freeform: str, chapter_num: int, voice_prompt: str | None = None
    ) -> dict:
        """Drafts title/act/summary for a brand-new outline entry at
        chapter_num from a freeform description, for the writer to
        review/edit before saving - used by the "describe it, let AI fill
        the fields" New Outline Entry flow."""
        system_prompt = f"{voice_prompt}\n\n{self.system_prompt}" if voice_prompt else self.system_prompt
        engine_brief = bible.story_engine_brief()
        engine_section = f"\n{engine_brief}\n" if engine_brief else ""
        entries = sorted(bible.data.get("outline", []), key=lambda e: e["chapter_num"])
        rest_of_outline = "\n".join(
            f"Chapter {e['chapter_num']} ({e.get('act', 'no act')}) - {e.get('title', '')}: {e.get('summary', '')}"
            for e in entries
        ) or "(No outline entries yet.)"

        prompt = f"""Book: {bible.data['title']} ({bible.data['genre']})
Premise: {bible.data['premise']}
{engine_section}
Characters:
{bible.characters_brief()}

Existing outline (for context - fit the new chapter in without contradicting these):
{rest_of_outline}

The writer's description of the new Chapter {chapter_num}:
\"\"\"
{freeform}
\"\"\"

Draft this ONE outline entry. Infer which act it belongs in from the
surrounding chapters unless the description says otherwise.
Respond with ONLY a single JSON object like:
{{"act": "...", "title": "...", "characters": ["established character names appearing on the page"],
"summary": "...", "outline": "..."}}"""

        return self.ask_json_object(prompt, system_prompt=system_prompt)

    def revise_outline(self, bible: StoryBible, instruction: str, voice_prompt: str | None = None) -> list[dict]:
        """Drafts a revised version of the ENTIRE outline per a freeform
        instruction (e.g. "add a subplot about X", "compress Act 2",
        "move the betrayal earlier") - for the writer to review/edit before
        saving via the "Revise Whole Outline" flow. Chapters that already
        have drafted/approved prose must keep their chapter_num and stay
        consistent with what's already written; the model is free to
        add/remove/reorder/renumber everything else."""
        system_prompt = f"{voice_prompt}\n\n{self.system_prompt}" if voice_prompt else self.system_prompt
        engine_brief = bible.story_engine_brief()
        engine_section = f"\n{engine_brief}\n" if engine_brief else ""
        continuity_brief = bible.continuity_and_progress_brief()
        continuity_section = f"\n{continuity_brief}\n" if continuity_brief else ""
        entries = sorted(bible.data.get("outline", []), key=lambda e: e["chapter_num"])
        current_outline = "\n".join(
            f"Chapter {e['chapter_num']} ({e.get('act', 'no act')}) - {e.get('title', '')}: {e.get('summary', '')}"
            for e in entries
        ) or "(No outline entries yet.)"

        prompt = f"""Book: {bible.data['title']} ({bible.data['genre']})
Premise: {bible.data['premise']}
{engine_section}
Characters:
{bible.characters_brief()}

Current full outline:
{current_outline}
{continuity_section}
The writer's instruction for revising the WHOLE outline:
\"\"\"
{instruction}
\"\"\"

Produce the complete revised outline reflecting this instruction. You may
add, remove, reorder, retitle, and renumber chapters as needed - EXCEPT any
chapter listed above as already drafted/approved must keep its exact
chapter_num and stay consistent with what's already written for it; you may
still lightly adjust its act/title/summary wording if the surrounding
changes require it, but do not contradict its established events.
Respond with ONLY a JSON array covering the ENTIRE revised outline, like:
[{{"chapter_num": 1, "act": "Act 1", "title": "...",
"characters": ["established character names appearing on the page"],
"summary": "...", "outline": "..."}}, ...]"""

        result = self.ask_json_list(prompt, required_key="chapter_num", system_prompt=system_prompt)
        result.sort(key=lambda c: c["chapter_num"])
        return result

    def plan_scenes(
        self, bible: StoryBible, chapter_num: int, extra_instruction: str | None = None,
        voice_prompt: str | None = None,
    ) -> list[dict]:
        """Explodes a chapter's outline beats into 2-4 scene cards (beats,
        emotional_state, transition, pov if it varies from the chapter POV),
        for the writer to review/edit before applying. The first scene's
        transition is informed by the previous chapter's ending, since that's
        the mechanism that makes "is this a direct continuation or a hard
        cut" an explicit field instead of an inference buried in prose."""
        system_prompt = f"{voice_prompt}\n\n{self.system_prompt}" if voice_prompt else self.system_prompt
        entry = bible.outline_entry(chapter_num)
        if entry is None:
            raise ValueError(f"No outline entry for chapter {chapter_num}")

        prev_recap = bible.previous_chapters_summary(chapter_num, max_chapters=1)
        extra_section = f"\nSpecifically, incorporate this: {extra_instruction}\n" if extra_instruction else ""
        transitions = ", ".join(f'"{t}"' for t in StoryBible.SCENE_TRANSITIONS)

        known_locations = [w["name"] for w in bible.data["world"] if w.get("category") == "location"]
        locations_section = (
            f"\nKnown locations already established in this book: {', '.join(known_locations)}. "
            "Prefer one of these for \"location\" when the scene is set there; if the scene needs "
            "a new place, invent a short name for it.\n"
            if known_locations else ""
        )

        prompt = f"""Book: {bible.data['title']} ({bible.data['genre']})
Premise: {bible.data['premise']}

Chapter {chapter_num} - {entry.get('title', '')} (POV: {entry.get('pov', '')}):
{entry.get('outline', '')}

Previous chapter's ending (for judging the first scene's transition):
{prev_recap}
{extra_section}{locations_section}
Break this chapter into 2-4 scenes, each a distinct beat/location/time unit.
For the FIRST scene, set "transition" based on how it opens relative to the
previous chapter's ending: {transitions}. Later scenes' "transition" describes
how each opens relative to the scene before it in THIS chapter.
Respond with ONLY a JSON array like:
[{{"beats": "what happens in this scene, from open to close", "pov": "{entry.get('pov', '')}",
"emotional_state": "e.g. curious -> afraid", "transition": "continuous",
"location": "name of where this scene takes place"}}, ...]"""

        result = self.ask_json_list(prompt, required_key="beats", system_prompt=system_prompt)
        return result
