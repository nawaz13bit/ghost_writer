"""Continuity Checker: scans the rest of the story bible - drafted chapters,
outline entries, characters, and world entries - for consequences of a change
made elsewhere (a character/world/story-engine edit, a chapter revision, or a
whole-outline revision), so the writer can review and selectively act on them
instead of hunting through the manuscript by hand.

Retrieval-gated (BM25 over `change_summary`, same machinery as author.py's
draft-time continuity lookups) rather than dumping the entire bible into the
prompt - a book-wide un-gated version scaled with total book size on every
manual check, regardless of how localized the actual change was.

This only flags - it never rewrites anything itself. Flagged items feed into
the same review queue used for multi-task universal-prompt instructions, so
each flagged chapter/outline entry/character/world entry still goes through
its own revise/review/save step.
"""
from __future__ import annotations

from typing import Any, Callable

from ghostwriter.agents.base import AIOutputError, Agent, chunk_by_chars
from ghostwriter.llm_client import LLMTruncated
from ghostwriter.memory.retriever import build_index, named_entity_hits
from ghostwriter.memory.story_bible import StoryBible

SYSTEM_PROMPT = """You are a continuity editor for a novel-writing tool.
Given a description of something that just changed in the story bible and a
summary of the rest of the book (outline, drafted chapters, characters,
world entries), find which of those items are now inconsistent with the
change (contradict a fact, reference an outdated trait/name/rule, or should
logically be updated to reflect it) and explain what needs to change in
each. Only flag items that are ACTUALLY affected - most items in a long book
are unrelated to any given change, and it is correct, expected, and
desirable to return an empty list for those. Always respond with ONLY a
JSON array."""

# How much of each character/world description to show the model - enough to
# catch contradictions without ballooning the prompt on a long book.
ENTITY_EXCERPT_CHARS = 400

# How many docs the BM25 index returns across all types combined (chapters,
# outline, scenes, characters, world, notes, timeline) - generous enough to
# cover a real cross-section of the book without reverting to a full dump.
RETRIEVAL_TOP_K = 40

# Batch size for check_book's rolling sweep - same default as Agent.ask_refine.
BOOK_CHECK_BATCH_CHARS = 40_000

BOOK_CHECK_SYSTEM_PROMPT = """You are a continuity editor doing a whole-book
consistency sweep. You are given a running "story so far" summary of chapters
already checked, the next chapter(s) in book order, and the current
characters/world/timeline bible entries. You do two things: (1) extend the
story-so-far summary to fold in the new chapter(s), and (2) flag anything -
in the new chapter(s) or in the bible entries shown - that is now
inconsistent (contradicts an established fact, references an outdated
trait/name/rule, or should logically be updated). Most chapters are
consistent with everything before them - it is correct and expected to
return an empty flags list when nothing is actually affected. Always respond
with ONLY a single JSON object."""


def _uses_chrono_timeline(bible: StoryBible) -> bool:
    """True once a project actually places any event on a track/chronology -
    stays False for every existing single-timeline project (galactic-follies
    included), so the plain chapter/order rendering below is untouched for
    them."""
    return any(
        t.get("track_id") is not None or t.get("chrono_order") is not None
        for t in bible.data.get("timeline", [])
    )


def _chrono_timeline_section(bible: StoryBible) -> str:
    """Chrono-grouped-by-track rendering of the timeline for the LLM prompt,
    used instead of the flat chapter-order list once a project has any
    track_id/chrono_order set, so the model reasons about in-world sequence
    rather than drafting order."""
    view = bible.timeline_chrono_view()
    lines = []
    for lane in view["lanes"]:
        track = lane["track"]
        lane_name = track["name"] if track else "(unassigned track)"
        lines.append(f"-- Track: {lane_name} --")
        for ev in lane["events"]:
            chrono = ev.get("chrono_order")
            chrono_tag = f"[chrono #{chrono}] " if chrono is not None else "[unplaced] "
            reveal_tag = f" (reveals {ev['refers_back_to']!r})" if ev.get("refers_back_to") else ""
            desc = ev.get("description") or ""
            if len(desc) > ENTITY_EXCERPT_CHARS:
                desc = desc[:ENTITY_EXCERPT_CHARS] + "..."
            lines.append(f"  {chrono_tag}{ev['name']}{reveal_tag}: {desc}")
    for cp in view["crosspoints"]:
        lines.append(f"-- Crosspoint ({cp['type']}): {cp['from_event']} -> {cp['to_event']} --")
    return "\n".join(lines) if lines else "(none)"


class ContinuityCheckerAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def check(self, bible: StoryBible, change_summary: str) -> list[dict]:
        entries = sorted(bible.data.get("outline", []), key=lambda e: e["chapter_num"])
        chapters_by_num = {c["chapter_num"]: c for c in bible.data.get("chapters", [])}

        index = build_index(bible, include_outline=True)
        forced = named_entity_hits(bible, change_summary)
        ranked = index.search(change_summary, top_k=RETRIEVAL_TOP_K)

        seen: set[str] = set()
        combined = []
        for doc in forced:
            if doc.doc_id not in seen:
                seen.add(doc.doc_id)
                combined.append(doc)
        for doc, _score in ranked:
            if doc.doc_id not in seen:
                seen.add(doc.doc_id)
                combined.append(doc)

        by_source: dict[str, list[str]] = {}
        for doc in combined:
            by_source.setdefault(doc.source, []).append(doc.text)

        def section(source: str, truncate: int | None = None) -> str:
            lines = by_source.get(source)
            if not lines:
                return "(none retrieved as relevant to this change)"
            if truncate:
                lines = [l if len(l) <= truncate else l[:truncate] + "..." for l in lines]
            return "\n".join(lines)

        drafted_section = section("chapter_summary")
        outline_section = section("outline")
        characters_section = section("character", truncate=ENTITY_EXCERPT_CHARS)
        world_section = section("world", truncate=ENTITY_EXCERPT_CHARS)
        notes_section = section("research", truncate=ENTITY_EXCERPT_CHARS)
        timeline_section = (
            _chrono_timeline_section(bible) if _uses_chrono_timeline(bible) else section("timeline", truncate=ENTITY_EXCERPT_CHARS)
        )
        scenes_section = section("scene")
        ideas_section = section("idea", truncate=ENTITY_EXCERPT_CHARS)

        prompt = f"""Book: {bible.data['title']} ({bible.data['genre']})

What just changed:
\"\"\"
{change_summary}
\"\"\"

Already-drafted/approved chapters (existing prose - flag if this change
contradicts what's already written):
{drafted_section}

Not-yet-drafted outline entries (flag if this change means the planned
chapter should be updated before it's drafted):
{outline_section}

Characters (flag if this change contradicts or should update a character's
description - e.g. a plot change alters their arc, a renamed faction appears
in their backstory):
{characters_section}

World entries (flag if this change contradicts or should update a
location/faction/rule description):
{world_section}

Research notes (flag if this change contradicts or makes a note outdated -
e.g. a fact the note records no longer holds):
{notes_section}

Timeline events (flag if this change contradicts an event's date/order, or
means an event's description is now outdated). When events are grouped by
track and chrono order below, also flag any character who acts on, or refers
to, knowledge from a chronologically-later event on any track unless that
later event is linked back via an explicit "reveals" relationship - that
kind of unearned foreknowledge is itself a continuity error:
{timeline_section}

Planned/drafted scenes within chapters (flag if this change means a scene's
beats now contradict another scene, e.g. a repeated reveal or a broken
transition):
{scenes_section}

Open idea-backlog entries, including planted/foreshadowed threads awaiting
payoff (flag if this change makes one of these ideas outdated, contradicted,
or otherwise something the writer should revisit):
{ideas_section}

Respond with ONLY a JSON array, one object per AFFECTED item (empty array
if nothing is affected). Each object has:
- "kind": "chapter" (drafted prose), "outline" (undrafted plan),
  "character", "world", "note", "timeline", "idea" (a backlog/planted-thread
  entry), or "scene" (a planned/drafted scene card within a chapter, for
  cross-scene contradictions)
- "chapter_num": <int> for kind chapter/outline/scene, null otherwise
- "scene_num": <int> for kind scene, null otherwise
- "target_name": the exact character/world entry/note/timeline event/idea
  title for kind character/world/note/timeline/idea, null otherwise
- "issue": what's now inconsistent
- "instruction": a concrete instruction for revising this item to fix it"""

        result = self.ask_json(prompt)
        if isinstance(result, dict):
            result = [result]
        if not isinstance(result, list):
            return []

        valid_nums = {e["chapter_num"] for e in entries}
        names = {
            "character": {(c.get("name") or "").lower(): c.get("name") for c in bible.data.get("characters", [])},
            "world": {(w.get("name") or "").lower(): w.get("name") for w in bible.data.get("world", [])},
            "note": {
                (n.get("name") or n.get("topic") or "").lower(): (n.get("name") or n.get("topic"))
                for n in bible.data.get("research_notes", [])
            },
            "timeline": {(t.get("name") or "").lower(): t.get("name") for t in bible.data.get("timeline", [])},
        }
        # Idea titles aren't guaranteed unique the way the other kinds' names
        # are treated as unique keys, so this also carries the idea's id
        # through to the flag (needed to target a specific record when fixing)
        # - first-match-wins among open ideas for a duplicate title.
        ideas_by_title: dict[str, dict] = {}
        for i in bible.data.get("ideas", []):
            if i.get("status", "open") != "open":
                continue
            key = (i.get("title") or "").strip().lower()
            if key and key not in ideas_by_title:
                ideas_by_title[key] = i

        flags = []
        for item in result:
            if not isinstance(item, dict):
                continue
            issue = item.get("issue")
            if not isinstance(issue, str) or not issue.strip():
                continue
            instruction = item.get("instruction")
            instruction = instruction.strip() if isinstance(instruction, str) and instruction.strip() else issue.strip()

            kind = item.get("kind")
            if kind in ("chapter", "outline"):
                chapter_num = item.get("chapter_num")
                if not isinstance(chapter_num, int) or chapter_num not in valid_nums:
                    continue
                ch = chapters_by_num.get(chapter_num)
                drafted = bool(ch and (ch.get("history") or ch.get("draft")))
                flags.append({
                    "kind": "chapter" if drafted else "outline",
                    "chapter_num": chapter_num,
                    "target_name": None,
                    "drafted": drafted,
                    "issue": issue.strip(),
                    "instruction": instruction,
                })
            elif kind == "scene":
                chapter_num = item.get("chapter_num")
                scene_num = item.get("scene_num")
                if not isinstance(chapter_num, int) or chapter_num not in valid_nums:
                    continue
                entry = next((e for e in entries if e["chapter_num"] == chapter_num), None)
                scene_nums = {s["scene_num"] for s in (entry.get("scenes") or [])} if entry else set()
                if not isinstance(scene_num, int) or scene_num not in scene_nums:
                    continue
                flags.append({
                    "kind": "scene",
                    "chapter_num": chapter_num,
                    "scene_num": scene_num,
                    "target_name": None,
                    "drafted": False,
                    "issue": issue.strip(),
                    "instruction": instruction,
                })
            elif kind in ("character", "world", "note", "timeline"):
                raw_name = item.get("target_name")
                canonical = names[kind].get(raw_name.strip().lower()) if isinstance(raw_name, str) else None
                if not canonical:
                    continue
                flags.append({
                    "kind": kind,
                    "chapter_num": None,
                    "target_name": canonical,
                    "drafted": False,
                    "issue": issue.strip(),
                    "instruction": instruction,
                })
            elif kind == "idea":
                raw_name = item.get("target_name")
                idea = ideas_by_title.get(raw_name.strip().lower()) if isinstance(raw_name, str) else None
                if not idea:
                    continue
                flags.append({
                    "kind": "idea",
                    "chapter_num": None,
                    "target_name": idea["title"],
                    "idea_id": idea["id"],
                    "drafted": False,
                    "issue": issue.strip(),
                    "instruction": instruction,
                })
        return flags

    def check_book(
        self,
        bible: StoryBible,
        chapters: list[dict[str, Any]],
        on_batch: Callable[[int, int], None] | None = None,
        voice_checker: Any | None = None,
        on_voice_progress: Callable[[int, int], None] | None = None,
    ) -> list[dict]:
        """A true whole-book sweep, distinct from check() above: walks every
        drafted/finalized chapter in book (chapter_num) order - never
        finalization order, since chapters are often drafted out of sequence
        - keeping a rolling "story so far" summary, the same batched-context
        shape as Agent.ask_refine (same chunk_by_chars batching, each batch
        seeing only the running summary of batches before it, never the raw
        text of earlier chapters). It doesn't call ask_refine itself because
        that helper's plain-text return has nowhere to carry a structured
        flags list back out per batch - here every batch call folds the new
        chapter(s) into the summary AND returns flags in the same JSON
        response, so a contradiction is never lost between batches. Every
        batch is also shown the full characters/world/timeline briefs (not
        just the running chapter summary), so this catches a chapter
        contradicting the bible itself, not only chapter-to-chapter drift.

        chapters: [{"chapter_num", "text"}, ...] for every chapter to sweep,
        prepared by the caller (which already has the finalized/current text
        for each chapter) - this agent stays focused on the checking logic.

        on_batch, if given, is called after each batch with (chapters_done,
        total_chapters) so a caller polling this as a background job can
        drive a determinate progress bar - each batch can bundle several
        chapters, so step_index would otherwise sit at 0 for the whole sweep
        and jump to done at the end.

        Also covers the full scope check() does (outline/scene/note/idea, not
        just chapter/character/world/timeline) via the same full-dump-per-
        batch treatment as characters/world/timeline below, since these are
        global bible state rather than chapter text. voice_checker, if given,
        is run once per chapter (its check() is chapter-scoped, not
        batch-shaped, so it can't join the batch loop above) and any
        drift it finds is folded in as kind:"chapter" flags alongside the
        continuity ones; on_voice_progress, if given, is called after each
        chapter's voice pass the same way on_batch drives the continuity
        loop's progress."""
        chapters = sorted(chapters, key=lambda c: c["chapter_num"])
        valid_nums = {c["chapter_num"] for c in chapters}
        if not valid_nums:
            return []

        outline_entries = sorted(bible.data.get("outline", []), key=lambda e: e["chapter_num"])
        outline_valid_nums = {e["chapter_num"] for e in outline_entries}

        def brief(items: list[dict], truncate: int = ENTITY_EXCERPT_CHARS) -> str:
            lines = []
            for it in items:
                name = it.get("name") or ""
                desc = it.get("description") or ""
                if len(desc) > truncate:
                    desc = desc[:truncate] + "..."
                lines.append(f"{name}: {desc}")
            return "\n".join(lines) if lines else "(none)"

        characters_section = brief(bible.data.get("characters", []))
        world_section = brief(bible.data.get("world", []))
        timeline_section = (
            _chrono_timeline_section(bible) if _uses_chrono_timeline(bible) else brief(bible.data.get("timeline", []))
        )
        notes_section = brief([
            {"name": n.get("name") or n.get("topic"), "description": n.get("content")}
            for n in bible.data.get("research_notes", [])
        ])
        open_ideas = [i for i in bible.data.get("ideas", []) if i.get("status", "open") == "open"]
        ideas_section = brief([{"name": i.get("title"), "description": i.get("notes")} for i in open_ideas])
        undrafted_entries = [e for e in outline_entries if e["chapter_num"] not in valid_nums]
        outline_lines = []
        for e in undrafted_entries:
            scene_bits = "; ".join(
                f"scene {s['scene_num']}: {s.get('beats', '')}" for s in (e.get("scenes") or [])
            )
            outline_lines.append(
                f"Chapter {e['chapter_num']} \"{e['title']}\": {e.get('summary', '')}"
                + (f" [{scene_bits}]" if scene_bits else "")
            )
        outline_section = "\n".join(outline_lines) if outline_lines else "(none)"

        names = {
            "character": {(c.get("name") or "").lower(): c.get("name") for c in bible.data.get("characters", [])},
            "world": {(w.get("name") or "").lower(): w.get("name") for w in bible.data.get("world", [])},
            "timeline": {(t.get("name") or "").lower(): t.get("name") for t in bible.data.get("timeline", [])},
            "note": {
                (n.get("name") or n.get("topic") or "").lower(): (n.get("name") or n.get("topic"))
                for n in bible.data.get("research_notes", [])
            },
        }
        # Idea titles aren't guaranteed unique, same caveat as check() above -
        # first-match-wins among open ideas for a duplicate title, and the
        # flag carries the idea's id so a fix can target the exact record.
        ideas_by_title: dict[str, dict] = {}
        for i in open_ideas:
            key = (i.get("title") or "").strip().lower()
            if key and key not in ideas_by_title:
                ideas_by_title[key] = i

        self.system_prompt = BOOK_CHECK_SYSTEM_PROMPT
        items = [f"--- CHAPTER {c['chapter_num']} ---\n{c['text']}" for c in chapters]

        summary = ""
        all_flags: list[dict] = []
        chapters_done = 0
        for batch in chunk_by_chars(items, BOOK_CHECK_BATCH_CHARS):
            batch_text = "\n\n".join(batch)
            chapters_done += len(batch)
            if on_batch is not None:
                on_batch(chapters_done, len(chapters))
            prompt = f"""Book: {bible.data['title']} ({bible.data['genre']})

Story so far (summary of chapters already checked in this sweep):
{summary or "(this is the first batch - nothing checked yet)"}

Characters bible:
{characters_section}

World bible:
{world_section}

Timeline events (when grouped by track/chrono order, also flag any character
acting on knowledge from a chronologically-later event on any track unless
linked back via an explicit "reveals" relationship):
{timeline_section}

Research notes:
{notes_section}

Open idea-backlog entries, including planted/foreshadowed threads awaiting
payoff:
{ideas_section}

Not-yet-drafted outline entries (and their planned scenes, if any) - flag if
a new chapter above means one of these plans should be updated before it's
drafted:
{outline_section}

Next chapter(s) in book order, to check against everything above:
{batch_text}

Respond with ONLY a JSON object:
{{"summary": "<the story-so-far summary above, extended to also cover the new chapter(s) - keep it a readable recap, not a diff>",
"flags": [ ... one object per AFFECTED item, empty array if nothing is affected ... ]}}
Each flag object has:
- "kind": "chapter" (something in the new chapter(s) above contradicts the
  story so far or the bible entries), "character", "world", "timeline", or
  "note" (one of the bible entries above is now contradicted or should be
  updated), "idea" (an open idea/planted thread is now outdated or
  contradicted), "outline" (an undrafted chapter's plan should change), or
  "scene" (a planned scene within an undrafted chapter's plan should change)
- "chapter_num": the chapter number for kind "chapter"/"outline"/"scene",
  null otherwise
- "scene_num": the scene number for kind "scene", null otherwise
- "target_name": the exact character/world/timeline/note/idea title for
  those kinds, null otherwise
- "issue": what's now inconsistent
- "instruction": a concrete instruction for revising this item to fix it"""
            try:
                result = self.ask_json_object(prompt)
            except (ValueError, AIOutputError, LLMTruncated):
                # One batch running out of max_tokens (a long running "story
                # so far" summary plus the full bible sections can fill the
                # budget on a long book) shouldn't abort the whole sweep -
                # skip it and keep going with the last good summary, same as
                # a malformed-JSON batch above.
                continue
            new_summary = result.get("summary")
            if isinstance(new_summary, str) and new_summary.strip():
                summary = new_summary.strip()

            raw_flags = result.get("flags")
            if isinstance(raw_flags, dict):
                raw_flags = [raw_flags]
            if not isinstance(raw_flags, list):
                continue
            for item in raw_flags:
                if not isinstance(item, dict):
                    continue
                issue = item.get("issue")
                if not isinstance(issue, str) or not issue.strip():
                    continue
                instruction = item.get("instruction")
                instruction = (
                    instruction.strip() if isinstance(instruction, str) and instruction.strip() else issue.strip()
                )
                kind = item.get("kind")
                if kind == "chapter":
                    chapter_num = item.get("chapter_num")
                    if not isinstance(chapter_num, int) or chapter_num not in valid_nums:
                        continue
                    all_flags.append({
                        "kind": "chapter",
                        "chapter_num": chapter_num,
                        "target_name": None,
                        "drafted": True,
                        "issue": issue.strip(),
                        "instruction": instruction,
                    })
                elif kind in ("character", "world", "timeline", "note"):
                    raw_name = item.get("target_name")
                    canonical = names[kind].get(raw_name.strip().lower()) if isinstance(raw_name, str) else None
                    if not canonical:
                        continue
                    all_flags.append({
                        "kind": kind,
                        "chapter_num": None,
                        "target_name": canonical,
                        "drafted": False,
                        "issue": issue.strip(),
                        "instruction": instruction,
                    })
                elif kind == "idea":
                    raw_name = item.get("target_name")
                    idea = ideas_by_title.get(raw_name.strip().lower()) if isinstance(raw_name, str) else None
                    if not idea:
                        continue
                    all_flags.append({
                        "kind": "idea",
                        "chapter_num": None,
                        "target_name": idea["title"],
                        "idea_id": idea["id"],
                        "drafted": False,
                        "issue": issue.strip(),
                        "instruction": instruction,
                    })
                elif kind == "outline":
                    chapter_num = item.get("chapter_num")
                    if not isinstance(chapter_num, int) or chapter_num not in outline_valid_nums:
                        continue
                    all_flags.append({
                        "kind": "outline",
                        "chapter_num": chapter_num,
                        "target_name": None,
                        "drafted": False,
                        "issue": issue.strip(),
                        "instruction": instruction,
                    })
                elif kind == "scene":
                    chapter_num = item.get("chapter_num")
                    scene_num = item.get("scene_num")
                    if not isinstance(chapter_num, int) or chapter_num not in outline_valid_nums:
                        continue
                    entry = next((e for e in outline_entries if e["chapter_num"] == chapter_num), None)
                    scene_nums = {s["scene_num"] for s in (entry.get("scenes") or [])} if entry else set()
                    if not isinstance(scene_num, int) or scene_num not in scene_nums:
                        continue
                    all_flags.append({
                        "kind": "scene",
                        "chapter_num": chapter_num,
                        "scene_num": scene_num,
                        "target_name": None,
                        "drafted": False,
                        "issue": issue.strip(),
                        "instruction": instruction,
                    })
        if voice_checker is not None:
            for i, c in enumerate(chapters):
                issues = voice_checker.check(bible, c["chapter_num"], c["text"])
                for issue in issues:
                    if not isinstance(issue, str) or not issue.strip():
                        continue
                    all_flags.append({
                        "kind": "chapter",
                        "chapter_num": c["chapter_num"],
                        "target_name": None,
                        "drafted": True,
                        "issue": issue.strip(),
                        "instruction": f"Fix this voice/dialogue inconsistency: {issue.strip()}",
                    })
                if on_voice_progress is not None:
                    on_voice_progress(i + 1, len(chapters))
        # Every batch above re-sends the same unchanged characters/world/
        # timeline bible sections, so a single persistent bible inconsistency
        # can get flagged again on every batch - one real issue would
        # otherwise come back as N near-duplicate flags, and the caller would
        # then "fix" the same entity N times in a row. Chapter-kind flags are
        # legitimately per-chapter (each chapter_num is its own target) so
        # only bible-entity flags are deduped here, keeping the first
        # occurrence of each (kind, target_name).
        deduped: list[dict] = []
        seen_entities: set[tuple] = set()
        for flag in all_flags:
            if flag["kind"] == "chapter":
                deduped.append(flag)
                continue
            elif flag["kind"] == "outline":
                key = ("outline", flag["chapter_num"])
            elif flag["kind"] == "scene":
                key = ("scene", flag["chapter_num"], flag["scene_num"])
            elif flag["kind"] == "idea":
                key = ("idea", flag["idea_id"])
            else:
                key = (flag["kind"], flag["target_name"])
            if key in seen_entities:
                continue
            seen_entities.add(key)
            deduped.append(flag)
        return deduped

    def check_timeline_paradoxes(self, bible: StoryBible) -> list[dict]:
        """Pure-code structural check over the loopy-timeline (Pattern A) and
        parallel-track (Pattern B) fields on timeline events - no LLM call,
        so this runs cheaply on every finalize/sync alongside the existing
        timeline_extractor proposal step. Two checks, both feeding the same
        review-gated continuity_flags queue as the LLM-based check() above:

        1. `refers_back_to` (a "reveal" event pointing at an earlier one it
           explains) must point at an event with chrono_order <= its own -
           a reveal can't refer back to something that happens later.
        2. A `cause_effect` crosspoint's "from" event must have chrono_order
           <= the "to" event's, since a cause can't follow its effect -
           unless a matching `paradox_loop` crosspoint between the same two
           events marks the loop as intentional.

        Events/crosspoints missing chrono_order on either side are skipped -
        there's nothing to contradict until both ends are actually placed on
        a chronology."""
        events_by_name = {t["name"]: t for t in bible.data.get("timeline", [])}
        flags: list[dict] = []

        for event in events_by_name.values():
            ref_name = event.get("refers_back_to")
            if not ref_name:
                continue
            ref_event = events_by_name.get(ref_name)
            if ref_event is None:
                continue
            chrono = event.get("chrono_order")
            ref_chrono = ref_event.get("chrono_order")
            if chrono is None or ref_chrono is None:
                continue
            if ref_chrono > chrono:
                flags.append({
                    "kind": "timeline",
                    "chapter_num": None,
                    "target_name": event["name"],
                    "drafted": False,
                    "issue": (
                        f"'{event['name']}' (chrono #{chrono}) refers back to '{ref_name}' "
                        f"(chrono #{ref_chrono}), but that event is placed LATER in the "
                        "in-world chronology - a reveal can't point at something that "
                        "hasn't happened yet."
                    ),
                    "instruction": (
                        f"Fix the chrono_order of '{event['name']}' and/or '{ref_name}' so the "
                        "reveal points back at an earlier event, or update refers_back_to."
                    ),
                })

        loop_exempted = set()
        for cp in bible.data.get("timeline_crosspoints", []):
            if cp.get("type") == "paradox_loop":
                loop_exempted.add(frozenset((cp["from_event"], cp["to_event"])))

        for cp in bible.data.get("timeline_crosspoints", []):
            if cp.get("type") != "cause_effect":
                continue
            if frozenset((cp["from_event"], cp["to_event"])) in loop_exempted:
                continue
            from_event = events_by_name.get(cp["from_event"])
            to_event = events_by_name.get(cp["to_event"])
            if from_event is None or to_event is None:
                continue
            from_chrono = from_event.get("chrono_order")
            to_chrono = to_event.get("chrono_order")
            if from_chrono is None or to_chrono is None:
                continue
            if from_chrono > to_chrono:
                flags.append({
                    "kind": "timeline",
                    "chapter_num": None,
                    "target_name": from_event["name"],
                    "drafted": False,
                    "issue": (
                        f"Cause-effect crosspoint says '{from_event['name']}' (chrono "
                        f"#{from_chrono}) causes '{to_event['name']}' (chrono #{to_chrono}), "
                        "but the cause is placed AFTER the effect in the in-world "
                        "chronology."
                    ),
                    "instruction": (
                        f"Fix the chrono_order of '{from_event['name']}' and/or "
                        f"'{to_event['name']}', or add a paradox_loop crosspoint between "
                        "them if this is an intentional time loop."
                    ),
                })

        return flags
