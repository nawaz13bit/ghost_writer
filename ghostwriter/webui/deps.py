"""Small shared helpers used across multiple routers: loading a project's
story bible, resolving a length category into concrete chapter/word counts,
and reading a chapter's current text."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException

from ghostwriter.agents.author import AuthorAgent
from ghostwriter.agents.personas import select_personas_for_genre
from ghostwriter.memory.story_bible import StoryBible
from ghostwriter.webui.state import cfg, history_compactor, llm


def load_bible(slug: str) -> StoryBible:
    path = Path(cfg["paths"]["projects_dir"]) / slug / "story_bible.json"
    if not path.exists():
        raise HTTPException(404, f"No project {slug!r}")
    return StoryBible.load(cfg["paths"]["projects_dir"], slug, dir_slug=slug)


def get_author(bible: StoryBible) -> AuthorAgent:
    personas = select_personas_for_genre(bible.data["genre"], bible.data.get("book_type", "fiction"))
    return AuthorAgent(llm, personas)


def require_chapter(bible: StoryBible, chapter_num: int) -> dict[str, Any]:
    ch = bible.get_chapter(chapter_num)
    if ch is None:
        raise HTTPException(404, f"No chapter {chapter_num}")
    return ch


def current_chapter_text(ch: dict[str, Any]) -> str:
    history = ch.get("history") or []
    if history:
        return history[-1]["text"]
    return ch.get("draft", "")


def maybe_compact_history(bible: StoryBible, history: list[dict[str, Any]], label: str) -> int:
    """Call after appending to a chapter/entity/scene's history list. If it's
    over StoryBible.HISTORY_CAP, folds everything but the most recent entries
    into one LLM-written synopsis entry and saves. Returns how many entries
    were folded away (0 if compaction didn't fire), so callers can surface a
    "N old revisions condensed" status message to the writer."""
    if not StoryBible.history_needs_compaction(history):
        return 0
    synopsis = history_compactor.compact(history[:-StoryBible.HISTORY_KEEP_TAIL], label)
    compacted_count = StoryBible.compact_history(history, synopsis)
    bible.save()
    return compacted_count


def resolve_length(
    category: dict[str, Any],
    num_chapters: int | None,
    chapter_target_words: int | None,
    total_word_target: int | None,
) -> tuple[int, int, int]:
    """Merges a length-category preset with any of the writer's explicit
    overrides (all independently optional, so any combination of total
    words / chapters / words-per-chapter can be pinned while the rest is
    derived) into a concrete (chapters, words_per_chapter, total_words)."""
    chapters = num_chapters or category["chapters"]
    if chapter_target_words:
        words_per_chapter = chapter_target_words
    elif total_word_target:
        words_per_chapter = max(1, total_word_target // chapters)
    else:
        words_per_chapter = category["words_per_chapter"]
    total_words = total_word_target or (chapters * words_per_chapter)
    return chapters, words_per_chapter, total_words
