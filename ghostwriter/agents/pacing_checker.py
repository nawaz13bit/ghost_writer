"""Pacing Checker: a narrow developmental-editing pass that flags scenes
which drag (over-lingering on low-stakes beats) or rush (major events
compressed past the point of landing). Distinct from continuity/voice/fact
checkers, which only look at correctness against the bible or real world -
this looks at whether the chapter's pacing serves the story. Report-only,
same review-gated-flag shape as fact_checker.py, so findings feed the same
Fix-with-AI queue via StoryBible.add_critique_flags."""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.agents.personas import CRITIQUE_EDITORS
from ghostwriter.memory.story_bible import StoryBible

SYSTEM_PROMPT = f"""You are {CRITIQUE_EDITORS['pacing']}, the Pacing Editor on
a novel-writing team. Read a chapter draft and judge its pacing: scenes that drag on past the point their
beat has landed, scenes that rush a major event so fast it doesn't register,
or a chapter that spends too much time on a minor beat at the expense of its
actual turning point. Do not flag prose style, grammar, or factual/continuity
issues - only pacing. Most well-paced chapters deserve zero flags. Always
respond with ONLY a JSON array, one object per pacing problem found (empty
array if none). Each object has:
- "issue": what's dragging or rushed, and where
- "instruction": a concrete instruction for revising the prose to fix it"""


class PacingCheckerAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def check_chapter(self, bible: StoryBible, chapter_num: int, text: str) -> list[dict]:
        entry = bible.outline_entry(chapter_num) or {}
        prompt = f"""Book: {bible.data['title']} ({bible.data['genre']})
Chapter {chapter_num} target beats: {entry.get('summary', '(n/a)')}

--- CHAPTER {chapter_num} ---
{text}
--- END CHAPTER ---

Respond with ONLY a JSON array of pacing problems (empty array if none)."""
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
                "category": "pacing",
            })
        return flags
