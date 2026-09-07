"""Small shared helpers used across multiple routers: loading a project's
story bible, resolving a length category into concrete chapter/word counts,
and reading a chapter's current text."""
from __future__ import annotations

import functools
import inspect
import threading
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, TypeVar

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


# StoryBible.save() always does a full-document read-modify-write with no
# locking or merge: every mutating endpoint does load_bible() (a fresh,
# independent in-memory copy) then eventually bible.save() (an atomic
# overwrite of the *entire* file, not just what it touched). If two such
# handlers' load-mutate-save windows overlap for the same project, the one
# that saves last silently clobbers the other's change with no error. The
# fix is a per-slug re-entrant lock held for the handler's whole
# load-mutate-save span, so overlapping requests on the same project
# serialize instead of racing (RLock so a handler that calls another
# lock-wrapped helper internally, on the same thread, doesn't deadlock).
_bible_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
_bible_locks_guard = threading.Lock()


def _get_bible_lock(slug: str) -> threading.RLock:
    with _bible_locks_guard:
        return _bible_locks[slug]


@contextmanager
def bible_lock(slug: str):
    lock = _get_bible_lock(slug)
    with lock:
        yield


_F = TypeVar("_F", bound=Callable[..., Any])


def with_bible_lock(func: _F) -> _F:
    """Decorator for a route handler or background-job worker that loads,
    mutates, and saves a project's StoryBible. Wraps the whole call in that
    project's bible_lock so it can't race another such call on the same
    project. Finds the project slug from a `slug` argument (by keyword, as
    FastAPI calls sync route handlers, or by position, as job-worker
    functions are called from a plain `threading.Thread`)."""
    try:
        slug_index = list(inspect.signature(func).parameters).index("slug")
    except ValueError:
        raise TypeError(f"{func!r} has no 'slug' parameter to lock on") from None

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if "slug" in kwargs:
            slug = kwargs["slug"]
        else:
            slug = args[slug_index]
        with bible_lock(slug):
            return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


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
