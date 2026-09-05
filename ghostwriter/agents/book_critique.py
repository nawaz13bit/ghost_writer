"""Book Critique: the book-level rollup for a whole-book critique run. A
local LLM's limited context can't hold a whole manuscript, so the whole-book
critique never re-feeds chapter prose here - it runs the narrow checkers
(pacing/stakes/craft) chapter by chapter first, then this agent synthesizes
patterns ACROSS chapters (a sagging middle act, stakes that never escalate,
a repeated tic) from just those chapters' summaries and findings, the same
map-reduce shape HistoryCompactorAgent uses for revision history. Each
resulting finding is tied to the specific chapter that most needs the fix
(from the "with fix jump" design decision), so it gets the same per-flag Fix
button as a chapter-level flag - only findings that are genuinely book-wide
with no single best chapter fall back to the first chapter, still keeping a
"Fix" jump rather than a "reason to be display-only" finding."""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.agents.personas import CRITIQUE_EDITORS
from ghostwriter.memory.story_bible import StoryBible

SYSTEM_PROMPT = f"""You are {CRITIQUE_EDITORS['book']}, the Developmental
Editor on a novel-writing team, doing a whole-book critique pass. You are given, for each chapter so far: a
short recap of what happens, and any pacing/stakes/craft problems already
flagged for that chapter individually. Your job is to find patterns that
only show up ACROSS chapters - a sagging middle act, stakes that never
escalate book-over-book, a subplot dropped and never resumed, a repeated
structural tic - not to repeat per-chapter findings you're already given.
Every finding must name the single chapter where fixing it would do the
most good (even for a book-wide pattern - pick the chapter where the fix
should start). Most books deserve a handful of findings, not one per
chapter. Always respond with ONLY a JSON array, one object per book-level
finding (empty array if none). Each object has:
- "chapter_num": the chapter number where the fix should start (integer)
- "issue": the cross-chapter pattern, in terms of that chapter specifically
- "instruction": a concrete instruction for revising that chapter to
  address the pattern"""


class BookCritiqueAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def critique_book(self, bible: StoryBible, chapter_reports: list[dict]) -> list[dict]:
        """chapter_reports: [{"chapter_num", "summary", "findings": [flag, ...]}, ...]
        for every chapter that was run through the narrow checkers."""
        if not chapter_reports:
            return []
        valid_nums = {r["chapter_num"] for r in chapter_reports}
        lines = []
        for r in sorted(chapter_reports, key=lambda r: r["chapter_num"]):
            lines.append(f"Chapter {r['chapter_num']}: {r['summary']}")
            for f in r.get("findings", []):
                lines.append(f"  - [{f.get('category', '?')}] {f['issue']}")
        log = "\n".join(lines)

        prompt = f"""Book: {bible.data['title']} ({bible.data['genre']})

Chapter-by-chapter recaps and already-flagged per-chapter findings:
{log}

Respond with ONLY a JSON array of cross-chapter findings (empty array if none)."""
        try:
            result = self.ask_json(prompt)
        except ValueError:
            return []
        if isinstance(result, dict):
            result = [result]
        if not isinstance(result, list):
            return []

        fallback_chapter = min(valid_nums)
        flags = []
        for item in result:
            if not isinstance(item, dict):
                continue
            issue = item.get("issue")
            if not isinstance(issue, str) or not issue.strip():
                continue
            chapter_num = item.get("chapter_num")
            if not isinstance(chapter_num, int) or chapter_num not in valid_nums:
                chapter_num = fallback_chapter
            instruction = item.get("instruction")
            instruction = instruction.strip() if isinstance(instruction, str) and instruction.strip() else issue.strip()
            flags.append({
                "kind": "chapter",
                "chapter_num": chapter_num,
                "target_name": None,
                "drafted": True,
                "issue": issue.strip(),
                "instruction": instruction,
                "category": "book",
            })
        return flags
