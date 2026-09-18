"""World Builder: establishes setting, locations, rules of the world, and factions."""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.memory.story_bible import StoryBible

SYSTEM_PROMPT = """You are the World Builder on a novel-writing team.
Your job is to define the story's setting: locations, history, social/political
structure, and (if speculative fiction) the rules of magic/technology. Keep
entries concrete and usable by an author drafting scenes, not abstract worldbuilding
lore-dumps. Always respond with ONLY a JSON array of objects, each with keys
"name", "category" (e.g. "location", "history", "faction", "rules-of-the-world",
"culture"), and "content" (2-5 sentences)."""


class WorldBuilderAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def build(self, bible: StoryBible, voice_prompt: str | None = None) -> None:
        system_prompt = f"{voice_prompt}\n\n{self.system_prompt}" if voice_prompt else self.system_prompt
        engine_brief = bible.story_engine_brief()
        engine_section = f"\n{engine_brief}\n" if engine_brief else ""
        prompt = f"""Book title: {bible.data['title']}
Genre: {bible.data['genre']}
Premise: {bible.data['premise']}
{engine_section}
Research notes so far:
{bible.research_brief()}

World-building already established (from this book or earlier books in the
series, if any) - do not duplicate these, only add genuinely new entries:
{bible.world_brief()}

Create 6-10 NEW world-building entries: key locations, relevant history,
factions/social structure, and (if applicable) rules of magic/technology.
Respond with ONLY a JSON array like:
[{{"name": "...", "category": "...", "content": "..."}}, ...]"""

        entries = self.ask_json(prompt, system_prompt=system_prompt)
        for entry in entries:
            bible.add_world_entry(entry["name"], entry["category"], entry["content"])

    def suggest_one(self, bible: StoryBible, freeform: str, voice_prompt: str | None = None) -> dict:
        """Drafts a single new world entry from a freeform description, for
        the writer to review/edit before saving - used by the "describe it,
        let AI fill the fields" New World Entry flow."""
        system_prompt = f"{voice_prompt}\n\n{self.system_prompt}" if voice_prompt else self.system_prompt
        engine_brief = bible.story_engine_brief()
        engine_section = f"\n{engine_brief}\n" if engine_brief else ""
        prompt = f"""Book title: {bible.data['title']}
Genre: {bible.data['genre']}
Premise: {bible.data['premise']}
{engine_section}
World-building already established - do not duplicate one of these:
{bible.world_brief()}

The writer's description of the new world entry:
\"\"\"
{freeform}
\"\"\"

Draft this ONE world-building entry, concrete and usable by an author drafting
scenes, filling in only what's needed without contradicting what the writer
wrote. Respond with ONLY a single JSON object like:
{{"name": "...", "category": "...", "content": "..."}}"""
        result = self.ask_json(prompt, system_prompt=system_prompt)
        if isinstance(result, list):
            result = result[0]
        return result
