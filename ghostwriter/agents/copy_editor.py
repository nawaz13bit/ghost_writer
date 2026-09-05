"""Copy Editor: final grammar/punctuation/typo pass, distinct from prose-style revision.

Runs last, right before a chapter is considered final, so it doesn't get
undone by later prose-quality or continuity rewrites.
"""
from __future__ import annotations

from ghostwriter.agents.base import Agent, strip_echoed_delimiters
from ghostwriter.memory.story_bible import StoryBible

SYSTEM_PROMPT = """You are the Copy Editor on a novel-writing team. You do a
final line-level pass: fix grammar, punctuation, spelling, typos, doubled
words, and repeated-word tics (the same distinctive word/phrase used too many
times nearby). Do not change sentence structure, style, voice, or content
beyond what's needed to fix these mechanical issues. Output only the corrected
chapter text, no commentary, no markdown."""


class CopyEditorAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def copyedit(self, bible: StoryBible, chapter_num: int, text: str) -> str:
        prompt = f"""--- CHAPTER {chapter_num} ---
{text}
--- END CHAPTER ---

Do a final copyedit pass: grammar, punctuation, spelling, typos, and repeated-word
tics only. Output only the corrected chapter text."""
        result = self.ask(prompt, max_tokens=max(self.llm.default_max_tokens, int(len(text.split()) * 2.5)))
        return strip_echoed_delimiters(result)
