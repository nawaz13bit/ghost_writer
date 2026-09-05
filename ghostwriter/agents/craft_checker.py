"""Craft Checker: a narrow developmental-editing pass that flags show-vs-tell
violations, purple/overwrought prose, and point-of-view slips. Distinct from
CopyEditorAgent (mechanical grammar/typos only) and EditorAgent.revise
(rewrites for quality but doesn't report). Same review-gated-flag shape as
fact_checker.py/pacing_checker.py, feeding StoryBible.add_critique_flags."""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.agents.personas import CRITIQUE_EDITORS
from ghostwriter.memory.story_bible import StoryBible

SYSTEM_PROMPT = f"""You are {CRITIQUE_EDITORS['craft']}, the Craft Editor on
a novel-writing team. Read a chapter draft and flag prose-craft problems: telling instead of showing an
emotion or reaction, purple/overwrought description, and point-of-view slips
(head-hopping, or narration knowing something the POV character couldn't).
Do not flag grammar/typos (a copyeditor's job), pacing, or plot/continuity
issues - only these craft problems. Most competently drafted chapters
deserve few or zero flags. Always respond with ONLY a JSON array, one object
per craft problem found (empty array if none). Each object has:
- "issue": what's told-not-shown, overwrought, or a POV slip, and where
- "instruction": a concrete instruction for revising the prose to fix it"""


class CraftCheckerAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def check_chapter(self, bible: StoryBible, chapter_num: int, text: str) -> list[dict]:
        entry = bible.outline_entry(chapter_num) or {}
        prompt = f"""Book: {bible.data['title']} ({bible.data['genre']})
Chapter {chapter_num} POV: {entry.get('pov', '(n/a)')}

--- CHAPTER {chapter_num} ---
{text}
--- END CHAPTER ---

Respond with ONLY a JSON array of craft problems (empty array if none)."""
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
                "category": "craft",
            })
        return flags
