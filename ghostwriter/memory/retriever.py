"""Minimal BM25 retriever over the story bible, used for continuity checks.

Kept dependency-free (no sentence-transformers/faiss) since the corpus for a
single book is small enough that a lexical index is fast and good enough for
"has this name/place/detail come up before" queries.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ghostwriter.memory.story_bible import StoryBible

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass
class Document:
    doc_id: str
    text: str
    source: str  # e.g. "character", "world", "chapter:3"


class BM25Index:
    def __init__(self, docs: list[Document], k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.k1 = k1
        self.b = b
        self._tokenized = [_tokenize(d.text) for d in docs]
        self._doc_len = [len(t) for t in self._tokenized]
        self._avg_len = sum(self._doc_len) / len(self._doc_len) if self._doc_len else 0.0

        df: dict[str, int] = {}
        for tokens in self._tokenized:
            for term in set(tokens):
                df[term] = df.get(term, 0) + 1
        n = len(docs)
        self._idf = {
            term: math.log(1 + (n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def search(self, query: str, top_k: int = 5) -> list[tuple[Document, float]]:
        if not self.docs:
            return []
        q_terms = _tokenize(query)
        scores = []
        for i, tokens in enumerate(self._tokenized):
            if not tokens:
                scores.append(0.0)
                continue
            term_counts: dict[str, int] = {}
            for t in tokens:
                term_counts[t] = term_counts.get(t, 0) + 1
            score = 0.0
            dl = self._doc_len[i]
            for term in q_terms:
                if term not in term_counts:
                    continue
                idf = self._idf.get(term, 0.0)
                tf = term_counts[term]
                denom = tf + self.k1 * (1 - self.b + self.b * dl / (self._avg_len or 1))
                score += idf * (tf * (self.k1 + 1)) / (denom or 1)
            scores.append(score)
        ranked = sorted(zip(self.docs, scores), key=lambda pair: pair[1], reverse=True)
        return [(doc, score) for doc, score in ranked[:top_k] if score > 0]


# Keyed on (story_bible.json path, include_outline, before_chapter) -> (mtime_ns, size, index).
# StoryBible.save() always rewrites the file (see _write()), which bumps both
# mtime and, almost always, size - so a change in either is a reliable signal
# the on-disk bible moved and the cached index is stale. Deliberately not
# keyed on any in-memory bible identity: callers reload StoryBible fresh per
# request, so caching here (not on the bible object) is what makes repeated
# build_index() calls for an unchanged file actually skip the rebuild.
_index_cache: dict[tuple[str, bool, int | None], tuple[int, int, "BM25Index"]] = {}


def build_index(bible: StoryBible, include_outline: bool = False, before_chapter: int | None = None) -> BM25Index:
    """`before_chapter`, when given (and `include_outline` is False), excludes
    drafted-chapter docs at or after that chapter number - keeps draft-time
    continuity retrieval from pulling in later-chapter content when chapters
    are written out of order, which would otherwise leak future plot
    developments into an earlier chapter's draft prompt."""
    path = bible.project_dir / "story_bible.json"
    try:
        st = path.stat()
    except OSError:
        return _build_index(bible, include_outline, before_chapter)
    cache_key = (str(path), include_outline, before_chapter)
    cached = _index_cache.get(cache_key)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    index = _build_index(bible, include_outline, before_chapter)
    _index_cache[cache_key] = (st.st_mtime_ns, st.st_size, index)
    return index


def _build_index(bible: StoryBible, include_outline: bool = False, before_chapter: int | None = None) -> BM25Index:
    """`include_outline=True` swaps the full-text drafted-chapter docs for
    lightweight one-line chapter/outline/scene summaries instead (used by the
    consistency checker, which needs to retrieve over chapters/outline/scenes
    too but would balloon the prompt if it pulled in full chapter prose the
    way author.py/editor.py's continuity lookups do). Default False preserves
    exact prior behavior for those two callers."""
    docs: list[Document] = []
    for c in bible.data["characters"]:
        docs.append(Document(f"character:{c['name']}", f"{c['name']} {c['role']} {c['description']}", "character"))
    for w in bible.data["world"]:
        docs.append(Document(f"world:{w['name']}", f"{w['name']} {w['category']} {w['content']}", "world"))
    for n in bible.data["research_notes"]:
        docs.append(Document(f"research:{n['topic']}", f"{n['topic']} {n['content']}", "research"))
    for t in bible.data.get("timeline", []):
        docs.append(Document(
            f"timeline:{t['name']}",
            f"{t['name']} {t.get('story_date', '')} {t.get('description', '')}",
            "timeline",
        ))
    series_recap = bible.data.get("series_recap")
    if series_recap:
        docs.append(Document("series_recap", series_recap, "series_recap"))

    if include_outline:
        chapters_by_num = {c["chapter_num"]: c for c in bible.data.get("chapters", [])}
        for e in sorted(bible.data.get("outline", []), key=lambda e: e["chapter_num"]):
            num = e["chapter_num"]
            ch = chapters_by_num.get(num)
            drafted = bool(ch and (ch.get("history") or ch.get("draft")))
            summary = (ch.get("summary") if ch else None) or e.get("summary", "")
            text = f"Chapter {num} ({e.get('act', 'no act')}) - {e.get('title', '')}: {summary}"
            source = "chapter_summary" if drafted else "outline"
            docs.append(Document(f"{source}:{num}", text, source))
            for s in e.get("scenes") or []:
                docs.append(Document(
                    f"scene:{num}:{s['scene_num']}",
                    f"Chapter {num} scene {s['scene_num']} [{s.get('transition', 'continuous')}]: {s.get('beats', '')}",
                    "scene",
                ))
        # Only open ideas are worth flagging a chapter against - a resolved
        # or dropped one is no longer a live continuity concern.
        for i in bible.data.get("ideas", []):
            if i.get("status", "open") != "open":
                continue
            docs.append(Document(f"idea:{i['id']}", f"{i.get('title', '')} {i.get('notes', '')}", "idea"))
    else:
        for num, title, text in bible.all_chapter_text():
            if before_chapter is not None and num >= before_chapter:
                continue
            docs.append(Document(f"chapter:{num}", f"{title} {text}", f"chapter:{num}"))
    return BM25Index(docs)


_NAME_STOPWORDS = {"the", "a", "an", "of", "and"}


def named_entity_hits(bible: StoryBible, text: str) -> list[Document]:
    """World/faction/character entries whose name is literally mentioned in
    `text` (e.g. this chapter's outline beats). BM25 only finds entries that
    share vocabulary with the query, so a faction like "the Kethrani Order"
    can be ranked out of the top_k if the chapter summary just says "the
    uprising" - exact-name matching guarantees it's included regardless of
    phrasing."""
    tokens = set(_tokenize(text))
    hits = []
    for w in bible.data["world"]:
        name_tokens = [t for t in _tokenize(w["name"]) if t not in _NAME_STOPWORDS]
        if name_tokens and all(t in tokens for t in name_tokens):
            hits.append(Document(f"world:{w['name']}", f"{w['name']} {w['category']} {w['content']}", "world"))
    for c in bible.data["characters"]:
        name_tokens = [t for t in _tokenize(c["name"]) if t not in _NAME_STOPWORDS]
        if name_tokens and all(t in tokens for t in name_tokens):
            hits.append(Document(f"character:{c['name']}", f"{c['name']} {c['role']} {c['description']}", "character"))
    for t in bible.data.get("timeline", []):
        name_tokens = [tok for tok in _tokenize(t["name"]) if tok not in _NAME_STOPWORDS]
        if name_tokens and all(tok in tokens for tok in name_tokens):
            hits.append(Document(
                f"timeline:{t['name']}",
                f"{t['name']} {t.get('story_date', '')} {t.get('description', '')}",
                "timeline",
            ))
    return hits


def character_names_in_text(bible: StoryBible, text: str) -> list[str]:
    """Names of characters literally mentioned in `text`, via the same exact-
    match logic as `named_entity_hits`. Used as a fallback cast when an
    outline entry has neither a "characters" nor "pov" field (schema drift on
    older entries) - without it, gating characters_brief() to the entry's
    cast would silently fall back to dumping the full roster for exactly
    those entries."""
    return [doc.doc_id.split(":", 1)[1] for doc in named_entity_hits(bible, text) if doc.source == "character"]


def continuity_context(
    bible: StoryBible, query: str, top_k: int = 5, match_text: str | None = None,
    before_chapter: int | None = None, exclude_characters: list[str] | None = None,
    exclude_chapter_nums: set[int] | None = None,
) -> str:
    """Human-readable snippet block to paste into a prompt for continuity
    grounding. `match_text` (defaults to `query`) is scanned for exact
    world/character/faction names, which are force-included ahead of the
    BM25 ranking - see `named_entity_hits`. `before_chapter` excludes drafted
    chapters at or after that number, to avoid leaking later-chapter content
    when drafting out of order - see `build_index`. `exclude_characters`
    drops character docs for those names (case-insensitive) - callers that
    also render a `characters_brief(names=...)` section for the same cast
    would otherwise get that character's description pasted into the prompt
    twice, since both pull from the same bible field. `exclude_chapter_nums`
    drops chapter docs for those chapter numbers - callers that also render a
    `character_chapter_neighbors()` section would otherwise get that same
    chapter's full text pulled in again as a redundant BM25 hit."""
    index = build_index(bible, before_chapter=before_chapter)
    forced = named_entity_hits(bible, match_text if match_text is not None else query)
    ranked = index.search(query, top_k=top_k)
    excluded = {n.strip().lower() for n in (exclude_characters or []) if n and n.strip()}
    excluded_chapters = exclude_chapter_nums or set()

    def _keep(doc: Document) -> bool:
        if doc.source.startswith("chapter:"):
            return int(doc.source.split(":", 1)[1]) not in excluded_chapters
        if doc.source != "character":
            return True
        return doc.doc_id.split(":", 1)[1].strip().lower() not in excluded

    seen: set[str] = set()
    combined: list[Document] = []
    for doc in forced:
        if doc.doc_id not in seen and _keep(doc):
            seen.add(doc.doc_id)
            combined.append(doc)
    for doc, _score in ranked:
        if doc.doc_id not in seen and _keep(doc):
            seen.add(doc.doc_id)
            combined.append(doc)

    if not combined:
        return "(no related continuity notes found)"
    lines = []
    for doc in combined:
        snippet = doc.text if len(doc.text) <= 500 else doc.text[:500] + "..."
        lines.append(f"[{doc.source}] {snippet}")
    return "\n".join(lines)
