"""Editor: revises drafts for prose quality, and checks continuity against the bible."""
from __future__ import annotations

from ghostwriter.agents.base import Agent, strip_echoed_delimiters
from ghostwriter.memory.retriever import character_names_in_text, continuity_context
from ghostwriter.memory.story_bible import StoryBible

EDIT_SYSTEM_PROMPT = """You are the Editor on a novel-writing team. You revise
chapter drafts for prose quality: tighten flabby sentences, fix pacing, sharpen
dialogue, remove repetition and cliches, and ensure the POV and tense are
consistent. Preserve the author's plot events and voice - do not change what
happens. Output only the revised chapter prose, no commentary, no markdown."""

CONTINUITY_SYSTEM_PROMPT = """You are the Continuity Checker on a novel-writing
team. Compare a chapter draft against established story-bible facts and flag
concrete contradictions (names, physical descriptions, timeline, established
rules of the world, prior events). You are also given the writer's open idea
backlog (setups, planted threads, notes-to-self) - flag a chapter that clearly
contradicts one of these ideas, or that was obviously the right place to pay
one off but ignores it entirely. Do not flag stylistic issues, and do not flag
an idea just because this chapter isn't its designated payoff chapter yet.
Always respond with ONLY a JSON array of strings, each a specific contradiction
or missed/ignored idea found. Respond with an empty array [] if there are none."""

REWRITE_SYSTEM_PROMPT = """You are the Editor on a novel-writing team, doing a
final continuity-repair pass. You are given a chapter and a list of specific
contradictions with established story-bible facts. Rewrite ONLY what's needed
to resolve each listed contradiction, changing as little else as possible -
preserve prose style, pacing, and all events that aren't contradicted. Output
only the corrected chapter text, no commentary, no markdown."""

SUMMARY_SYSTEM_PROMPT = """You summarize novel chapters in 3-5 sentences for
use as a recap by other writers on the team, covering key events, revelations,
and how the chapter ends. Output only the summary, no preamble."""


class EditorAgent(Agent):
    def revise(self, bible: StoryBible, chapter_num: int, draft: str) -> str:
        self.system_prompt = EDIT_SYSTEM_PROMPT
        entry = bible.outline_entry(chapter_num) or {}
        prompt = f"""Book: {bible.data['title']} ({bible.data['genre']})
Chapter {chapter_num} target beats: {entry.get('summary', '(n/a)')}

--- DRAFT ---
{draft}
--- END DRAFT ---

Revise this chapter for prose quality. Output only the revised chapter text."""
        result = self.ask(prompt, max_tokens=max(self.llm.default_max_tokens, int(len(draft.split()) * 2.5)))
        return strip_echoed_delimiters(result)

    def check_continuity(self, bible: StoryBible, chapter_num: int, text: str) -> list[str]:
        self.system_prompt = CONTINUITY_SYSTEM_PROMPT
        entry = bible.outline_entry(chapter_num) or {}
        cast = entry.get("characters") or ([entry["pov"]] if entry.get("pov") else []) or character_names_in_text(bible, text)
        continuity = continuity_context(
            bible, query=text[:1000], top_k=8, before_chapter=chapter_num, exclude_characters=cast,
        )
        ideas_brief = bible.open_ideas_brief()
        ideas_section = f"\nOpen ideas/setups the writer has noted (flag if contradicted or clearly ignored):\n{ideas_brief}\n" if ideas_brief else ""

        prompt = f"""Established story-bible facts that may be relevant:
{continuity}

Characters:
{bible.characters_brief(names=cast if cast else None)}
{ideas_section}
--- CHAPTER {chapter_num} DRAFT ---
{text}
--- END DRAFT ---

List any concrete contradictions with the established facts above, plus any
open idea above that this chapter contradicts or clearly ignores.
Respond with ONLY a JSON array of strings (empty array if none)."""
        result = self.ask_json(prompt)
        return result if isinstance(result, list) else []

    def fix_continuity(self, bible: StoryBible, chapter_num: int, text: str, issues: list[str]) -> str:
        self.system_prompt = REWRITE_SYSTEM_PROMPT
        issues_list = "\n".join(f"- {issue}" for issue in issues)
        prompt = f"""Contradictions to fix:
{issues_list}

--- CHAPTER {chapter_num} ---
{text}
--- END CHAPTER ---

Rewrite to fix only the listed contradictions. Output only the corrected chapter text."""
        result = self.ask(prompt, max_tokens=max(self.llm.default_max_tokens, int(len(text.split()) * 2.5)))
        return strip_echoed_delimiters(result)

    def summarize(self, bible: StoryBible, chapter_num: int, text: str) -> str:
        self.system_prompt = SUMMARY_SYSTEM_PROMPT
        prompt = f"--- CHAPTER {chapter_num} ---\n{text}\n\nSummarize in 3-5 sentences."
        return self.ask(prompt, max_tokens=max(self.llm.default_max_tokens, 300))
