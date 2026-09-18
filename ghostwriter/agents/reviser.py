"""Reviser: applies a human-written instruction to an existing piece of text
(a chapter, a character description, a world entry) without a fresh full
rewrite. Generic across entity types - the caller supplies a context_label
describing what kind of text this is."""
from __future__ import annotations

from ghostwriter.agents.base import Agent, strip_echoed_delimiters

SYSTEM_PROMPT = """You are the Revision Assistant on a novel-writing team. You
are given an existing piece of text and an instruction describing a change to
make. Scale the depth of your rewrite to the scope of the instruction:

- A narrow, targeted instruction (fix a name, adjust one detail, tweak a
  line) means a minimal edit - change only what it asks and preserve
  everything else word-for-word.
- A sweeping instruction (change the tone, shift POV, make it darker,
  restructure a scene, cut it down) means a genuine rewrite - transform as
  much of the text as it takes to fully deliver the instruction. Do not be
  timid: a surface pass that leaves the old voice/structure intact is a
  failure to follow the instruction.

In both cases preserve plot events, facts, and details the instruction does
not touch - depth of rewrite is not license to invent or drop story content.
Output only the revised text, no commentary, no markdown, no preamble like
"Here is the revised text"."""


class ReviserAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def revise(
        self, context_label: str, text: str, instruction: str, on_delta=None,
        research_notes: list[dict] | None = None, voice_prompt: str | None = None,
    ) -> str:
        # voice_prompt lets a caller (chapter revise) ground the edit in the
        # project's author persona(s) instead of this agent's generic,
        # voice-agnostic prompt - bible/character/idea edits don't need it,
        # but chapter prose should keep sounding like the same author.
        system_prompt = f"{voice_prompt}\n\n{self.system_prompt}" if voice_prompt else self.system_prompt
        research_section = ""
        if research_notes:
            notes_block = "\n\n".join(
                f"[note id: {n['id']}] {n['topic']}\n{n['content']}" for n in research_notes
            )
            research_section = (
                "\nSourced real-world research notes (streets, buildings, procedures, "
                "history, etc.) for the new details this instruction introduces - use "
                "these for grounded, accurate detail. Do not add citation markers or "
                "footnotes. Treat them as reference, not constraint: where a real-world "
                f"detail would work against the scene, favor the story.\n{notes_block}\n"
            )
        prompt = f"""This is {context_label}.

--- CURRENT TEXT ---
{text}
--- END CURRENT TEXT ---

Instruction: {instruction}
{research_section}
Apply this instruction and output only the revised text."""
        max_tokens = max(self.llm.default_max_tokens, int(len(text.split()) * 2.5))
        if on_delta is not None:
            result = self.llm.chat_stream(system_prompt, prompt, on_delta, max_tokens=max_tokens)
        else:
            result = self.llm.chat(system_prompt, prompt, max_tokens=max_tokens)
        return strip_echoed_delimiters(result)
