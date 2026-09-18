"""Blurb/Query Letter Agent: generates the back-cover blurb (retailer/back-
cover marketing copy) and a literary-agent query letter from the bible plus
whatever chapters are finished so far. Distinct from
StoryBible.full_synopsis_brief (spoiler-full, for series continuity) - both
outputs here are deliberately spoiler-light, the way real jacket copy and
query letters are written, so they're generated from the premise/characters/
outline rather than a full chapter dump."""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.memory.story_bible import StoryBible

SYSTEM_PROMPT = """You are a publishing-industry copywriter who writes two
things for novelists: back-cover blurbs and query letters to literary
agents. Both are spoiler-light (hook the reader/agent, don't give away the
ending or major twists) and written in the book's genre conventions.

A back-cover blurb: 100-200 words, punchy, present tense, sells the premise
and stakes, ends on a hook or question - the kind of copy that goes on a
retailer listing or the back cover.

A query letter: the standard three-part format agents expect - a one-line
hook, a short (150-250 word) synopsis of the premise/protagonist/central
conflict/stakes (spoiler-light, no ending), and a brief metadata paragraph
(title, genre, approximate word count, and a one-line author bio if given -
omit anything not provided rather than inventing it). Do not invent
comparable titles/comps unless explicitly told to.

Always respond with ONLY a JSON object with two keys: "blurb" and
"query_letter", each a plain-text string (no markdown formatting)."""


class BlurbAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def generate(self, bible: StoryBible, voice_prompt: str | None = None) -> dict[str, str]:
        system_prompt = f"{voice_prompt}\n\n{self.system_prompt}" if voice_prompt else self.system_prompt
        word_count = sum(ch.get("word_count", 0) for ch in bible.data.get("chapters", []))
        bio_line = f"Author bio: {bible.data['about_author']}" if bible.data.get("about_author") else "Author bio: (none given - omit the bio line)"
        prompt = f"""Title: {bible.data['title']}
Genre: {bible.data['genre']}
Premise: {bible.data['premise']}
Tone: {bible.data.get('tone') or '(unspecified)'}
Approximate manuscript word count so far: {word_count or '(not yet drafted)'}
{bio_line}

Main characters:
{bible.characters_brief()}

Outline (for context only - do not spoil the ending in either output):
{bible.full_outline_brief()}

Write a back-cover blurb and a query letter for this book. Respond with
ONLY a JSON object: {{"blurb": "...", "query_letter": "..."}}."""
        result = self.ask_json_object(prompt, system_prompt=system_prompt)
        blurb = result.get("blurb")
        query_letter = result.get("query_letter")
        return {
            "blurb": blurb.strip() if isinstance(blurb, str) else "",
            "query_letter": query_letter.strip() if isinstance(query_letter, str) else "",
        }
