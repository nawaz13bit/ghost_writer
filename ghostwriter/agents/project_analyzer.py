"""Analyzes one freeform concept dump for a brand-new project and proposes
structured story-bible fields (title, genre, premise, tone, narrative voice,
narrative engine, themes, characters, world entries) for the writer to review
and edit before the project is actually created.

This replaces asking for title/genre/premise as separate blank inputs on
project creation - the writer just writes/pastes whatever they already have
about the book, and the AI drafts a starting point from it.
"""
from __future__ import annotations

from ghostwriter.agents.base import Agent

SYSTEM_PROMPT = """You are a developmental editor helping a novelist turn a
rough, freeform book concept - which may be a single sentence or several
paragraphs of premise, characters, world notes, and vibes all mixed together -
into a structured starting point for a new project.

Read what the writer gave you and propose:
- title: a working title (invent a fitting one if none is implied)
- genre: a short genre label
- premise: a tight one-to-two sentence pitch
- tone: a few descriptive words (e.g. "wry, melancholic, breakneck, cozy")
- narrative_voice: POV, tense, and narrator personality
- narrative_engine: what should drive momentum chapter to chapter - the
  central question, ticking clock, or escalating conflict
- themes: a list of ideas/questions the book should explore (do not state
  them as on-the-nose morals)
- characters: a list of the named characters implied or stated, each with
  name, role ("protagonist"/"antagonist"/"supporting"/etc.), and a short
  description
- world: a list of named locations/factions/objects/rules of the setting
  implied or stated, each with name, category (e.g. "location", "faction",
  "object", "rule"), and a short description

Only invent details that fill obvious gaps needed to make the fields usable -
never contradict anything the writer actually wrote. If the writer gave you
almost nothing, it's fine for characters/world to be short or empty lists
rather than inventing an entire cast from scratch. Always respond with ONLY a
single JSON object."""


class ProjectAnalyzerAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def analyze(self, freeform_prompt: str) -> dict:
        prompt = f"""The writer's concept, verbatim:
\"\"\"
{freeform_prompt}
\"\"\"

Respond with ONLY a JSON object like:
{{"title": "...", "genre": "...", "premise": "...", "tone": "...",
"narrative_voice": "...", "narrative_engine": "...",
"themes": ["...", "..."],
"characters": [{{"name": "...", "role": "...", "description": "..."}}],
"world": [{{"name": "...", "category": "...", "description": "..."}}]}}"""

        result = self.ask_json(prompt)
        if not isinstance(result, dict):
            raise ValueError("Expected a JSON object from the project analyzer")

        def as_str(key: str) -> str:
            val = result.get(key, "")
            return val.strip() if isinstance(val, str) else ""

        def as_list(key: str) -> list:
            val = result.get(key)
            return val if isinstance(val, list) else []

        themes = as_list("themes")
        themes_text = "\n".join(t.strip() for t in themes if isinstance(t, str) and t.strip())

        characters = []
        for c in as_list("characters"):
            if not isinstance(c, dict) or not isinstance(c.get("name"), str) or not c["name"].strip():
                continue
            characters.append({
                "name": c["name"].strip(),
                "role": (c.get("role") or "supporting").strip() if isinstance(c.get("role"), str) else "supporting",
                "description": (c.get("description") or "").strip() if isinstance(c.get("description"), str) else "",
            })

        world = []
        for w in as_list("world"):
            if not isinstance(w, dict) or not isinstance(w.get("name"), str) or not w["name"].strip():
                continue
            world.append({
                "name": w["name"].strip(),
                "category": (w.get("category") or "general").strip() if isinstance(w.get("category"), str) else "general",
                "description": (w.get("description") or "").strip() if isinstance(w.get("description"), str) else "",
            })

        return {
            "title": as_str("title"),
            "genre": as_str("genre"),
            "premise": as_str("premise"),
            "tone": as_str("tone"),
            "narrative_voice": as_str("narrative_voice"),
            "narrative_engine": as_str("narrative_engine"),
            "themes": themes_text,
            "characters": characters,
            "world": world,
        }

    def revise(self, bible, instruction: str) -> dict:
        """Redrafts premise/tone/narrative_voice/narrative_engine/themes per
        a freeform instruction (e.g. "make the tone darker", "tighten the
        narrative engine around a ticking clock"), for the writer to
        review/edit before saving - used by the Story Engine section's
        "Revise with instruction" flow. Title/genre are left alone since
        they're identity, not story-engine content."""
        prompt = f"""Book title: {bible.data['title']}
Genre: {bible.data['genre']}

Current story engine:
Premise: {bible.data.get('premise', '')}
Tone: {bible.data.get('tone', '')}
Narrative voice: {bible.data.get('narrative_voice', '')}
Narrative engine: {bible.data.get('narrative_engine', '')}
Themes: {bible.data.get('themes', '')}

The writer's instruction for revising these:
\"\"\"
{instruction}
\"\"\"

Redraft ONLY what the instruction calls for; leave anything it doesn't
mention as close to the original as makes sense. Respond with ONLY a single
JSON object like:
{{"premise": "...", "tone": "...", "narrative_voice": "...",
"narrative_engine": "...", "themes": "..."}}
("themes" as a single string, one theme per line, matching the input format.)"""

        result = self.ask_json_object(prompt)

        def as_str(key: str, current: str) -> str:
            val = result.get(key)
            return val.strip() if isinstance(val, str) and val.strip() else current

        return {
            "premise": as_str("premise", bible.data.get("premise", "")),
            "tone": as_str("tone", bible.data.get("tone", "")),
            "narrative_voice": as_str("narrative_voice", bible.data.get("narrative_voice", "")),
            "narrative_engine": as_str("narrative_engine", bible.data.get("narrative_engine", "")),
            "themes": as_str("themes", bible.data.get("themes", "")),
        }
