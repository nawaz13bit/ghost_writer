"""Discuss agent: the "Discuss" side of the universal prompt bar's Discuss/Do
toggle. Where the normal "Do" path (RouterAgent.classify) always produces one
or more concrete tasks headed straight for a review-gated create/revise flow,
this agent just talks - it offers thoughts, options, and open questions about
the book, without generating anything to approve/reject.

Multi-turn: the caller passes the whole back-and-forth so far (not just the
latest message) so the writer can iterate on an idea across several
exchanges instead of each reply starting cold.

If a suggestion firms up into something concrete enough to actually build,
this agent may propose a ready-to-submit instruction - but it never builds
it itself. The UI hands that instruction to the exact same universal-prompt
classify+dispatch pipeline "Do" uses, so nothing skips the review gate.
"""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.memory.story_bible import StoryBible

SYSTEM_PROMPT = """You are a brainstorming partner for a novel-writing tool.
The writer wants to think out loud about their book - explore ideas, weigh
options, get a second opinion - WITHOUT you generating any concrete change
for them to approve. Respond conversationally, in plain prose (no JSON, no
markdown headers). Ask questions back when it would help. Offer specific,
opinionated suggestions rather than vague menus of options.

If, and only if, the discussion has converged on one concrete, buildable
change the writer seems ready to act on, end your reply with a final line of
the exact form:
SUGGESTED_PROMPT: <a single self-contained instruction, written as if the
writer typed it into the universal prompt bar themselves>
Omit that line entirely otherwise - most replies should not have one."""

SUGGESTION_PREFIX = "SUGGESTED_PROMPT:"


class DiscussAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def discuss(self, bible: StoryBible, history: list[dict]) -> dict:
        """history is a list of {"role": "writer"|"assistant", "text": str},
        oldest first, ending with the writer's newest message. Returns
        {"reply": str, "suggested_prompt": str | None}."""
        transcript = "\n\n".join(
            f"{'Writer' if turn.get('role') == 'writer' else 'You'}: {turn.get('text', '')}"
            for turn in history
        )
        instructions = f"""Book: {bible.data['title']} ({bible.data['genre']})
Premise: {bible.data.get('premise', '')}

Conversation so far:
{transcript}

Reply to the writer's latest message."""
        raw = self.ask(instructions)
        reply = raw.strip()
        suggested_prompt = None
        lines = reply.splitlines()
        if lines and lines[-1].strip().startswith(SUGGESTION_PREFIX):
            suggested_prompt = lines[-1].split(SUGGESTION_PREFIX, 1)[1].strip() or None
            reply = "\n".join(lines[:-1]).strip()
        return {"reply": reply, "suggested_prompt": suggested_prompt}
