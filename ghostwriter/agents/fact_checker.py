"""Fact Checker: a post-hoc pass over already-finalized prose that checks its
real-world claims (real streets, buildings, agencies, historical dates,
equipment specs, etc.) against sourced web research, rather than only
checking internal consistency against the rest of the bible.

Same review-gated-flag pattern as continuity_checker.py: this only flags,
it never rewrites anything itself. Flags are persisted via
StoryBible.add_continuity_flags so they show up in the same Continuity view
and feed the same review queue as internal-consistency flags. Gated the same
way as ResearcherAgent.suggest_for_chapter - only runs for book_type
"nonfiction", when the project's real_world_setting flag is set, or when any
character/world entry is tagged is_real (a mostly-invented story that
name-drops one real place/person still gets checked for that one detail),
since a fully invented setting has nothing real to check against.
"""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.memory.story_bible import StoryBible
from ghostwriter.tools.web_search import fetch_page_text, osm_geocode, web_search, wikipedia_lookup

QUERY_SYSTEM_PROMPT = """You help fact-check a novel's finalized prose. Given
a passage of prose, identify the specific factual, historical, statistical,
technical, or real-world-setting claims a reader familiar with the real
place/period/procedure would notice if wrong - e.g. real streets, buildings,
neighborhoods, landmarks, transit routes, agency jurisdictions,
weapons/equipment specs, or historical dates/events - and propose specific
web-search queries to verify them. If the passage makes no checkable
real-world claims, respond with an empty array. Always respond with ONLY a
JSON array of strings."""

SYSTEM_PROMPT = """You are a fact-checker for a novel-writing tool. You are
given a passage of finalized prose and sourced web research gathered to
verify its real-world claims. Compare the prose against the sources and
flag ONLY claims that are actually contradicted by the sources (a wrong
street, an anachronistic date, an impossible procedure, etc.) - not stylistic
choices, not claims the sources don't address, and not deliberate fictional
license (a novel is allowed to invent street names, compress geography, or
bend a real procedure for pacing - only flag it if it reads as an
unintentional error rather than a choice the story is making). Most passages
in a well-researched novel will have zero real errors - it is correct and
expected to return an empty list in that case. Always respond with ONLY a
JSON array."""


class FactCheckerAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def __init__(self, llm, web_enabled: bool = True, max_queries: int = 5, results_per_query: int = 3, pages_per_query: int = 2):
        super().__init__(llm)
        self.web_enabled = web_enabled
        self.max_queries = max_queries
        self.results_per_query = results_per_query
        self.pages_per_query = pages_per_query

    def _gather_sources(self, queries: list[str]) -> list[dict]:
        sources: list[dict] = []
        seen_urls: set[str] = set()
        for query in queries:
            results = list(web_search(query, max_results=self.results_per_query))
            wiki = wikipedia_lookup(query)
            if wiki is not None:
                results.insert(0, wiki)
            geocode = osm_geocode(query)
            if geocode is not None and geocode.url not in seen_urls:
                seen_urls.add(geocode.url)
                sources.append({"title": geocode.title, "url": geocode.url, "text": geocode.snippet})
            for result in results[: self.pages_per_query]:
                if result.url in seen_urls:
                    continue
                text = fetch_page_text(result.url) or result.snippet
                if text:
                    seen_urls.add(result.url)
                    sources.append({"title": result.title, "url": result.url, "text": text[:3000]})
        return sources

    def check_chapter(self, bible: StoryBible, chapter_num: int, text: str) -> list[dict]:
        """Checks one finalized chapter's prose for real-world factual errors.
        Returns flags shaped like continuity_checker's (kind "chapter"), so
        they persist and render through the same Continuity view/queue."""
        if not self.web_enabled:
            return []
        if (
            bible.data.get("book_type") != "nonfiction"
            and not bible.data.get("real_world_setting")
            and not bible.has_real_entities()
        ):
            return []

        self.system_prompt = QUERY_SYSTEM_PROMPT
        query_prompt = f"""Book title: {bible.data['title']}
Genre: {bible.data['genre']}

Chapter {chapter_num} prose:
\"\"\"
{text[:6000]}
\"\"\"

Propose up to {self.max_queries} specific web-search queries to verify the
real-world claims in this passage. Respond with ONLY a JSON array of
strings."""
        try:
            queries = self.ask_json(query_prompt)
        except ValueError:
            queries = []
        finally:
            self.system_prompt = SYSTEM_PROMPT
        queries = [q for q in queries if isinstance(q, str)][: self.max_queries]
        if not queries:
            return []

        sources = self._gather_sources(queries)
        if not sources:
            return []
        sources_block = "\n\n".join(f"[Source: {s['title']} ({s['url']})]\n{s['text']}" for s in sources)

        prompt = f"""Book title: {bible.data['title']}
Genre: {bible.data['genre']}

Chapter {chapter_num} prose:
\"\"\"
{text[:6000]}
\"\"\"

Sourced web research gathered to verify this chapter's real-world claims:
{sources_block}

Respond with ONLY a JSON array, one object per claim that is actually
contradicted by the sources above (empty array if none). Each object has:
- "issue": what's wrong and what the sources say instead
- "instruction": a concrete instruction for revising the prose to fix it"""
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
            })
        return flags
