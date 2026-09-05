"""History Compactor: condenses old revision-history entries (chapter
drafts, entity revisions, scene drafts) into one short synopsis so an
append-only history list never grows without bound. Never touches canonical
text (a chapter's "final", an entity's description/content) - only the
audit trail is folded."""
from __future__ import annotations

from ghostwriter.agents.base import Agent

SYSTEM_PROMPT = """You are the History Compactor on a novel-writing team.
Old revision snapshots are being condensed to keep the project file from
growing without bound. Your job is to write a short synopsis of how the
content evolved across a batch of old revisions and why, so the writer keeps
a record of what happened without storing every full-text snapshot forever.
Focus on what changed between revisions and why (per each entry's source/
instruction), not a recap of the content itself. Be concise - a few
sentences is enough even for many entries. Respond with ONLY the synopsis
text: no preamble, no JSON, no markdown."""


class HistoryCompactorAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def compact(self, entries: list[dict], label: str) -> str:
        lines = []
        for e in entries:
            src = e.get("source", "?")
            instr = f" - {e['instruction']}" if e.get("instruction") else ""
            excerpt = (e.get("text") or "").strip().replace("\n", " ")[:200]
            lines.append(f"- [{src}]{instr}: {excerpt}")
        log = "\n".join(lines)
        prompt = f"""{label}: {len(entries)} old revisions are being condensed to save space.

Revision log (oldest first, each with a short excerpt of that revision's text):
{log}

Write a short synopsis (2-5 sentences) of how this content evolved across
these revisions and why. This becomes the permanent record replacing the
full-text snapshots above."""
        return self.ask(prompt).strip()
