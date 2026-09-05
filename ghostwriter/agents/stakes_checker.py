"""Stakes/Tension Checker: a narrow developmental-editing pass that flags
scenes where the reader has no clear reason to keep reading - stakes that
are unclear, tension that slackens with no consequence in sight, or a
conflict that resolves too easily/conveniently. Same review-gated-flag
shape as fact_checker.py/pacing_checker.py, feeding StoryBible.add_critique_flags."""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.agents.personas import CRITIQUE_EDITORS
from ghostwriter.memory.story_bible import StoryBible

SYSTEM_PROMPT = f"""You are {CRITIQUE_EDITORS['stakes']}, the Story Editor on
a novel-writing team, focused on stakes and tension. Read a chapter draft and judge whether the reader has
a clear reason to keep reading: are the stakes (what a character stands to
gain or lose) clear, is tension maintained or does it slacken with nothing
at risk, does a conflict resolve too easily or conveniently (deus ex
machina, no real cost). Do not flag prose style, grammar, pacing, or
factual/continuity issues - only stakes/tension. Most well-built chapters
deserve zero flags. Always respond with ONLY a JSON array, one object per
stakes/tension problem found (empty array if none). Each object has:
- "issue": what's unclear, slack, or too easily resolved, and where
- "instruction": a concrete instruction for revising the prose to fix it"""


class StakesCheckerAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def check_chapter(self, bible: StoryBible, chapter_num: int, text: str) -> list[dict]:
        entry = bible.outline_entry(chapter_num) or {}
        prompt = f"""Book: {bible.data['title']} ({bible.data['genre']})
Chapter {chapter_num} target beats: {entry.get('summary', '(n/a)')}

--- CHAPTER {chapter_num} ---
{text}
--- END CHAPTER ---

Respond with ONLY a JSON array of stakes/tension problems (empty array if none)."""
        try:
            result = self.ask_json(prompt)
        except ValueError:
            return []
        if isinstance(result, dict):
            result = [result]
        if not isinstance(result, list):
            return []

        flags = []
        for item in result:
            if not isinstance(item, dict):
                continue
            issue = item.get("issue")
            if not isinstance(issue, str) or not issue.strip():
                continue
            instruction = item.get("instruction")
            instruction = instruction.strip() if isinstance(instruction, str) and instruction.strip() else issue.strip()
            flags.append({
                "kind": "chapter",
                "chapter_num": chapter_num,
                "target_name": None,
                "drafted": True,
                "issue": issue.strip(),
                "instruction": instruction,
                "category": "stakes",
            })
        return flags
