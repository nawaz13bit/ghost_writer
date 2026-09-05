"""Researcher: gathers background/domain notes the author can draw on for verisimilitude.

When web research is enabled, this agent proposes search queries, runs them
against DuckDuckGo, fetches the top pages, and asks the model to write notes
grounded in that real text (with sources). If the web is disabled or
unreachable, it falls back to eliciting the model's own broad knowledge in a
structured way - so the pipeline still works fully offline.
"""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.memory.story_bible import StoryBible
from ghostwriter.tools.web_search import fetch_page_text, osm_geocode, web_search, wikipedia_lookup

SYSTEM_PROMPT = """You are the Researcher on a novel-writing team.
Your job is to produce concise, concrete, well-organized background notes
that a novelist can use for authenticity and texture: setting/period details,
plausible technical or professional facts, terminology, sensory details,
and common misconceptions to avoid. Be specific and avoid generic filler.
When web research findings are provided, ground your notes in those specific
facts rather than genre tropes, and prefer concrete details over vague ones.
Always respond with ONLY a JSON array of objects, each with keys
"topic" and "content" (content is 2-5 sentences)."""

QUERY_SYSTEM_PROMPT = """You help a novelist plan research. Given a book's
genre and premise, propose specific, web-searchable queries (not vague
topics) that would surface useful setting, technical, period, or terminology
details for the story. Always respond with ONLY a JSON array of strings."""


class ResearcherAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def __init__(
        self,
        llm,
        web_enabled: bool = True,
        max_queries: int = 5,
        results_per_query: int = 3,
        pages_per_query: int = 2,
    ):
        super().__init__(llm)
        self.web_enabled = web_enabled
        self.max_queries = max_queries
        self.results_per_query = results_per_query
        self.pages_per_query = pages_per_query

    def _propose_queries(self, bible: StoryBible, topics: list[str] | None) -> list[str]:
        if topics:
            return list(topics)[: self.max_queries]
        self.system_prompt = QUERY_SYSTEM_PROMPT
        prompt = f"""Book title: {bible.data['title']}
Genre: {bible.data['genre']}
Premise: {bible.data['premise']}

Propose up to {self.max_queries} specific web-search queries for research.
Respond with ONLY a JSON array of strings."""
        try:
            queries = self.ask_json(prompt)
        except ValueError:
            queries = []
        finally:
            self.system_prompt = SYSTEM_PROMPT
        return [q for q in queries if isinstance(q, str)][: self.max_queries]

    def _gather_sources(self, queries: list[str], prefer_wikipedia: bool = False) -> list[dict]:
        """Runs each query and fetches page text, keeping title/url/text
        together so callers can persist sources (for citations) instead of
        only handing the LLM a flattened prompt string. Also tries an
        OpenStreetMap geocode lookup per query - a no-op for non-place
        queries, but confirms real streets/buildings/landmarks with a
        canonical address a search snippet alone won't pin down."""
        sources: list[dict] = []
        seen_urls: set[str] = set()
        for query in queries:
            results = list(web_search(query, max_results=self.results_per_query))
            if prefer_wikipedia:
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

    @staticmethod
    def _sources_to_context(sources: list[dict]) -> str:
        return "\n\n".join(f"[Source: {s['title']} ({s['url']})]\n{s['text']}" for s in sources)

    def _gather_web_context(self, queries: list[str]) -> str:
        return self._sources_to_context(self._gather_sources(queries))

    def research(self, bible: StoryBible, topics: list[str] | None = None) -> None:
        self.system_prompt = SYSTEM_PROMPT
        already_known = bible.research_brief()
        engine_brief = bible.story_engine_brief()
        engine_section = f"\n{engine_brief}\n" if engine_brief else ""
        web_context = ""
        if self.web_enabled:
            queries = self._propose_queries(bible, topics)
            if queries:
                web_context = self._gather_web_context(queries)

        topics_hint = (
            f"Focus especially on these topics: {', '.join(topics)}."
            if topics else
            "Infer the most useful research topics yourself from the premise and genre."
        )
        web_block = (
            f"Web research findings to ground your notes in (cite specifics, don't just "
            f"restate genre tropes):\n{web_context}"
            if web_context else
            "(No web sources were available - use your own knowledge, and flag in the "
            "content when a detail should be double-checked before publishing.)"
        )
        prompt = f"""Book title: {bible.data['title']}
Genre: {bible.data['genre']}
Premise: {bible.data['premise']}
{engine_section}
Research notes already established (from this book or earlier books in the
series - do not duplicate these, only add genuinely new notes):
{already_known}

{topics_hint}

{web_block}

Produce 5-8 NEW research notes covering setting, plausible domain/technical
details, terminology, and sensory/atmospheric texture relevant to this story.
Respond with ONLY a JSON array like:
[{{"topic": "...", "content": "..."}}, ...]"""

        notes = self.ask_json(prompt)
        for note in notes:
            bible.add_research_note(note["topic"], note["content"])

    def suggest_one(self, bible: StoryBible, freeform: str) -> dict:
        """Researches and drafts a single new note from a freeform topic, for
        the writer to review/edit before saving - used to add research
        mid-project (the initial pass in research() only ever runs once, at
        project creation)."""
        self.system_prompt = SYSTEM_PROMPT
        engine_brief = bible.story_engine_brief()
        engine_section = f"\n{engine_brief}\n" if engine_brief else ""
        sources: list[dict] = []
        web_context = ""
        if self.web_enabled:
            sources = self._gather_sources([freeform])
            web_context = self._sources_to_context(sources)
        web_block = (
            f"Web research findings to ground this note in (cite specifics, don't just "
            f"restate genre tropes):\n{web_context}"
            if web_context else
            "(No web sources were available - use your own knowledge, and flag in the "
            "content when a detail should be double-checked before publishing.)"
        )
        prompt = f"""Book title: {bible.data['title']}
Genre: {bible.data['genre']}
Premise: {bible.data['premise']}
{engine_section}
Notes already established - do not duplicate one of these:
{bible.research_brief()}

The writer wants a research note on:
\"\"\"
{freeform}
\"\"\"

{web_block}

Draft this ONE research note, concrete and usable by an author for authenticity
and texture. Respond with ONLY a single JSON object like:
{{"topic": "...", "content": "..."}}"""
        result = self.ask_json(prompt)
        if isinstance(result, list):
            result = result[0]
        return {"name": result.get("topic"), "content": result.get("content"), "sources": sources}

    def suggest_for_chapter(self, bible: StoryBible, chapter_num: int, entry: dict) -> list[dict]:
        """Auto-detect pass for non-fiction drafting: reads the chapter's
        beats/summary, identifies factual claims worth sourcing, researches
        them, and returns draft notes for the writer to review/edit before
        saving - nothing is written to the bible here. Used to auto-trigger
        research during draft_chapter instead of requiring a manual
        suggest_one() call per topic."""
        if not self.web_enabled:
            return []
        if (
            bible.data.get("book_type") != "nonfiction"
            and not bible.data.get("real_world_setting")
            and not bible.has_real_entities()
        ):
            # Invented settings have nothing real to verify - skip the LLM
            # call entirely rather than paying for one to learn that.
            return []
        beats = entry.get("outline") or entry.get("summary") or ""
        self.system_prompt = QUERY_SYSTEM_PROMPT
        query_prompt = f"""Book title: {bible.data['title']}
Subject/category: {bible.data['genre']}
Premise: {bible.data['premise']}

This chapter's content:
\"\"\"
{entry.get('title', '')}
{beats}
\"\"\"

Identify the specific factual, historical, statistical, technical, or
real-world-setting claims in this chapter's content that a reader familiar
with the real place/period/procedure would notice if wrong - e.g. real
streets, buildings, neighborhoods, landmarks, transit routes, agency
jurisdictions, weapons/equipment specs, or historical dates/events - and
propose up to {self.max_queries} specific web-search queries to verify/research
them. If nothing in the chapter references anything real or verifiable
(e.g. it's set in an invented world/city), respond with an empty array.
Respond with ONLY a JSON array of strings."""
        try:
            queries = self.ask_json(query_prompt)
        except ValueError:
            queries = []
        finally:
            self.system_prompt = SYSTEM_PROMPT
        queries = [q for q in queries if isinstance(q, str)][: self.max_queries]
        if not queries:
            return []

        sources = self._gather_sources(queries, prefer_wikipedia=True)
        if not sources:
            return []
        web_context = self._sources_to_context(sources)
        already_known = bible.research_brief()
        prompt = f"""Book title: {bible.data['title']}
Subject/category: {bible.data['genre']}
Premise: {bible.data['premise']}

Research notes already established - do not duplicate these:
{already_known}

Web research findings gathered for Chapter {chapter_num} ("{entry.get('title', '')}"),
cite specifics from these sources rather than generic claims:
{web_context}

Produce one research note per distinct claim/topic that is well-supported by
the sources above. Respond with ONLY a JSON array like:
[{{"topic": "...", "content": "..."}}, ...]"""
        try:
            notes = self.ask_json(prompt)
        except ValueError:
            return []
        if not isinstance(notes, list):
            return []
        proposals = []
        for note in notes:
            if not isinstance(note, dict):
                continue
            topic, content = note.get("topic"), note.get("content")
            if not topic or not content:
                continue
            proposals.append({"topic": topic, "content": content, "sources": sources})
        return proposals
