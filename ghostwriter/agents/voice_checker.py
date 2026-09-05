"""Dialogue/Voice Consistency Checker: flags dialogue that drifts from a
character's established voice. Complements the fact-based Continuity Checker,
which only looks at concrete contradictions, not tone/vocabulary/register.
"""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.memory.retriever import character_names_in_text
from ghostwriter.memory.story_bible import StoryBible

SYSTEM_PROMPT = """You are the Voice Consistency Checker on a novel-writing
team. Compare each character's dialogue in a chapter against their established
voice profile (vocabulary, register, speech patterns, personality). Flag
places where a character speaks in a way that contradicts their established
voice (e.g. a terse character rambling, an uneducated character using jargon
they wouldn't know, tonal whiplash with no in-story cause). Do not flag plot
or factual issues - only voice/register drift. Always respond with ONLY a
JSON array of strings, each a specific voice inconsistency found. Respond
with an empty array [] if dialogue is consistent for everyone."""


class VoiceCheckerAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def check(self, bible: StoryBible, chapter_num: int, text: str) -> list[str]:
        entry = bible.outline_entry(chapter_num) or {}
        cast = entry.get("characters") or ([entry["pov"]] if entry.get("pov") else []) or character_names_in_text(bible, text)
        prompt = f"""Established character voices:
{bible.characters_brief(names=cast if cast else None)}

--- CHAPTER {chapter_num} ---
{text}
--- END CHAPTER ---

List any dialogue that drifts from a character's established voice.
Respond with ONLY a JSON array of strings (empty array if none)."""
        result = self.ask_json(prompt)
        return result if isinstance(result, list) else []
