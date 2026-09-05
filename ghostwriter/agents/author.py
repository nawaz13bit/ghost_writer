"""Author: drafts chapter prose from the outline, world, and character bible.

Writing voice comes from one or more Persona objects (see personas.py) - a
single genre author writing solo, or several genre authors collaborating on
the same chapter when the book blends genres.
"""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.agents.personas import Persona, build_collaborative_system_prompt
from ghostwriter.memory.retriever import character_names_in_text, continuity_context
from ghostwriter.memory.story_bible import StoryBible


class AuthorAgent(Agent):
    def __init__(self, llm, personas: list[Persona]):
        super().__init__(llm)
        self.personas = personas
        self.system_prompt = build_collaborative_system_prompt(personas)

    def draft_chapter(
        self, bible: StoryBible, chapter_num: int, target_words: int = 1800, on_delta=None,
        research_notes: list[dict] | None = None,
    ) -> str:
        entry = bible.outline_entry(chapter_num)
        if entry is None:
            raise ValueError(f"No outline entry for chapter {chapter_num}")
        nonfiction = bible.data.get("book_type") == "nonfiction"

        beats = entry.get("outline") or ""
        search_text = f"{entry['title']} {entry.get('pov', '')} {entry['summary']} {beats}"
        cast = entry.get("characters") or ([entry["pov"]] if entry.get("pov") else []) or character_names_in_text(bible, search_text)
        neighbors, neighbor_chapter_nums = bible.character_chapter_neighbors(chapter_num, cast) if cast else ("", set())
        continuity = continuity_context(
            bible, query=search_text, top_k=8, match_text=search_text, before_chapter=chapter_num,
            exclude_characters=cast, exclude_chapter_nums=neighbor_chapter_nums,
        )

        engine_brief = bible.story_engine_brief()
        engine_section = f"\n{engine_brief}\n" if engine_brief else ""

        act = bible.act_entry(entry.get("act")) if entry.get("act") else None
        act_section = f"\nCurrent act ({entry['act']}) summary: {act['summary']}\n" if act and act.get("summary") else ""

        timeline = bible.data.get("timeline") or []
        timeline_section = f"\nStory timeline (for chronology - do not contradict):\n{bible.timeline_brief()}\n" if timeline else ""

        neighbors_section = (
            f"\nOther chapters featuring this chapter's characters (may be earlier OR later in "
            f"the book if written out of order - do not contradict):\n{neighbors}\n"
        ) if neighbors else ""

        if beats:
            beats_section = f"Chapter outline (what happens, beat by beat): {beats}"
        else:
            beats_section = f"Chapter summary/beats to hit: {entry['summary']}"

        characters_section = (
            "" if nonfiction
            else f"Characters:\n{bible.characters_brief(names=cast if cast else None)}\n"
        )
        pov_line = "" if nonfiction else (f"POV character: {entry['pov']}" if entry.get('pov') else '')

        research_section = ""
        if research_notes:
            notes_block = "\n\n".join(
                f"[note id: {n['id']}] {n['topic']}\n{n['content']}" for n in research_notes
            )
            if nonfiction:
                research_section = (
                    "\nSourced research notes for this chapter (cite specific facts drawn from "
                    f"these inline using their exact [^id] marker):\n{notes_block}\n"
                )
            else:
                research_section = (
                    "\nSourced real-world research notes for this chapter (streets, buildings, "
                    "procedures, history, etc.) - use these for grounded, accurate detail. Do "
                    "not add citation markers or footnotes; this is prose, not nonfiction. "
                    "Treat them as reference, not constraint: where a real-world detail would "
                    f"work against the scene, favor the story.\n{notes_block}\n"
                )

        prompt = f"""Book: {bible.data['title']} ({bible.data['genre']})
Premise: {bible.data['premise']}
{engine_section}
Full outline (for context on where this chapter sits):
{bible.full_outline_brief(around_chapter=chapter_num)}
{act_section}
{characters_section}
Relevant world-building and continuity notes for this chapter:
{continuity}
{timeline_section}
{neighbors_section}
{research_section}
Recap of earlier books in this series (if any):
{bible.data.get("series_recap") or "(This is not part of a series.)"}

Recap of recent chapters:
{bible.previous_chapters_summary(chapter_num)}

---
Write Chapter {chapter_num}: "{entry['title']}"
{pov_line}
{beats_section}

Target length: about {target_words} words - scope the scene to fit that
tightly rather than padding it out; keep pacing tight and momentum forward.
Write the full chapter prose now."""

        max_tokens = max(self.llm.default_max_tokens, int(target_words * 2.2))
        if on_delta is not None:
            return self.ask_stream(prompt, on_delta, max_tokens=max_tokens)
        return self.ask(prompt, max_tokens=max_tokens)

    def draft_scene(
        self, bible: StoryBible, chapter_num: int, scene_num: int, target_words: int = 600, on_delta=None
    ) -> str:
        """Drafts a single scene's prose, scoped to its own beats rather than
        the whole chapter. Scenes are an internal planning/generation unit
        only - the model is explicitly told not to add a scene heading/label,
        since the output gets concatenated into one continuous chapter."""
        entry = bible.outline_entry(chapter_num)
        if entry is None:
            raise ValueError(f"No outline entry for chapter {chapter_num}")
        scene = bible.get_scene(chapter_num, scene_num)
        if scene is None:
            raise ValueError(f"No scene {scene_num} for chapter {chapter_num}")

        beats = scene.get("beats") or ""
        search_text = f"{entry['title']} {scene.get('pov') or entry.get('pov', '')} {beats}"
        cast = entry.get("characters") or ([entry["pov"]] if entry.get("pov") else []) or character_names_in_text(bible, search_text)
        neighbors, neighbor_chapter_nums = bible.character_chapter_neighbors(chapter_num, cast) if cast else ("", set())
        continuity = continuity_context(
            bible, query=search_text, top_k=8, match_text=search_text, before_chapter=chapter_num,
            exclude_characters=cast, exclude_chapter_nums=neighbor_chapter_nums,
        )

        engine_brief = bible.story_engine_brief()
        engine_section = f"\n{engine_brief}\n" if engine_brief else ""

        act = bible.act_entry(entry.get("act")) if entry.get("act") else None
        act_section = f"\nCurrent act ({entry['act']}) summary: {act['summary']}\n" if act and act.get("summary") else ""

        timeline = bible.data.get("timeline") or []
        timeline_section = f"\nStory timeline (for chronology - do not contradict):\n{bible.timeline_brief()}\n" if timeline else ""

        neighbors_section = (
            f"\nOther chapters featuring this chapter's characters (may be earlier OR later in "
            f"the book if written out of order - do not contradict):\n{neighbors}\n"
        ) if neighbors else ""

        prior_scenes = sorted(
            (s for s in (entry.get("scenes") or []) if s["scene_num"] < scene_num and s.get("draft")),
            key=lambda s: s["scene_num"],
        )
        if prior_scenes:
            prior_section = "\n\nEarlier scenes already drafted in this chapter (continue directly from here - do not re-introduce or repeat their events):\n" + \
                "\n\n".join(s["draft"] for s in prior_scenes)
        else:
            prior_section = ""

        transition_note = {
            "continuous": "This scene continues directly from the previous scene/chapter with no time gap.",
            "same-day": "This scene happens later the same day as what came before.",
            "time-skip": "Some time has passed since what came before - establish the gap briefly without belaboring it.",
            "pov-shift": "This scene shifts point of view from what came before.",
        }.get(scene.get("transition", "continuous"), "")

        pov = scene.get("pov") or entry.get("pov", "")

        location_section = ""
        location_directive = ""
        loc_name = scene.get("location")
        if loc_name:
            loc_entry = bible.find_world_entry(loc_name)
            if loc_entry:
                location_section = f"\nSetting for this scene - {loc_entry['name']}:\n{loc_entry['content']}\n"
                location_directive = (
                    " Ground the scene's sensory detail and atmosphere in this specific "
                    "setting rather than a generic backdrop."
                )
            else:
                location_section = f"\nSetting for this scene: {loc_name}\n"

        prompt = f"""Book: {bible.data['title']} ({bible.data['genre']})
Premise: {bible.data['premise']}
{engine_section}
Characters:
{bible.characters_brief(names=cast if cast else None)}
{act_section}
Relevant world-building and continuity notes for this scene:
{continuity}
{location_section}
{timeline_section}
{neighbors_section}
This is scene {scene_num} of Chapter {chapter_num}: "{entry['title']}".
{f"POV character: {pov}" if pov else ''}
{f"Emotional arc across this scene: {scene['emotional_state']}" if scene.get('emotional_state') else ''}{location_directive}
{transition_note}
{prior_section}

What happens in this scene: {beats}

Target length: about {target_words} words. Write ONLY this scene's prose -
no scene heading, number, or label of any kind, since this text will be
concatenated directly with the surrounding scenes into one continuous
chapter. Write the prose now."""

        max_tokens = max(self.llm.default_max_tokens, int(target_words * 2.2))
        if on_delta is not None:
            return self.ask_stream(prompt, on_delta, max_tokens=max_tokens)
        return self.ask(prompt, max_tokens=max_tokens)


def stitch_scenes(bible: StoryBible, chapter_num: int) -> str:
    """Concatenates a chapter's drafted scenes into one continuous chapter
    text. Plain concatenation for v1 - scenes are written with continuity
    context (prior-scene recap, transition notes) already baked in, so seams
    should mostly read fine without an extra smoothing pass."""
    entry = bible.outline_entry(chapter_num)
    if entry is None:
        raise ValueError(f"No outline entry for chapter {chapter_num}")
    scenes = sorted(entry.get("scenes") or [], key=lambda s: s["scene_num"])
    parts = [s["draft"] for s in scenes if s.get("draft")]
    return "\n\n".join(parts)
