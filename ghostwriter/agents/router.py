"""Router: classifies a freeform "universal prompt" - which may describe one
task or several distinct tasks in the same block of text - into which
section(s) of the story bible it targets and what action to take in each, so
the writer can type instructions without navigating to a section first.

This only classifies - it does not draft or revise anything itself. The
webui endpoint that calls it dispatches each classified task, in order, to
the same per-section agent methods/endpoints already used when the writer
navigates to a section manually, so each task still produces exactly the
same kind of draft/review/save step as the manual path - multitasking just
queues several of those steps instead of collapsing everything into one.
"""
from __future__ import annotations

from ghostwriter.agents.base import Agent
from ghostwriter.memory.story_bible import StoryBible

VALID_ACTIONS = {
    "create_character",
    "create_world",
    "create_note",
    "create_timeline_event",
    "create_outline_entry",
    "revise_outline_whole",
    "revise_outline_entry",
    "revise_engine",
    "revise_character",
    "revise_world",
    "revise_note",
    "revise_timeline_event",
    "revise_chapter",
    "plan_scenes",
    "revise_scene",
    "unclear",
}

SYSTEM_PROMPT = """You are a router for a novel-writing tool. Given a
writer's freeform instruction and a summary of their book's current state,
decide which single action it calls for. Always respond with ONLY a JSON
object."""


class RouterAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    def classify(self, bible: StoryBible, prompt: str) -> list[dict]:
        characters = [c["name"] for c in bible.data["characters"]]
        world = [w["name"] for w in bible.data["world"]]
        notes = [n.get("name") or n.get("topic") for n in bible.data["research_notes"]]
        timeline_events = [t["name"] for t in bible.data.get("timeline", [])]
        drafted_chapters = sorted(
            c["chapter_num"] for c in bible.data["chapters"] if c.get("history") or c.get("draft")
        )
        outline_chapters = sorted(e["chapter_num"] for e in bible.data.get("outline", []))
        next_chapter_num = (max(outline_chapters) + 1) if outline_chapters else 1

        instructions = f"""Book: {bible.data['title']} ({bible.data['genre']})

Existing characters: {", ".join(characters) or "(none)"}
Existing world entries: {", ".join(world) or "(none)"}
Existing notes: {", ".join(n for n in notes if n) or "(none)"}
Existing timeline events: {", ".join(timeline_events) or "(none)"}
Outline chapter numbers: {", ".join(str(n) for n in outline_chapters) or "(none)"}
Chapters with drafted/approved prose: {", ".join(str(n) for n in drafted_chapters) or "(none)"}
Next unused chapter number: {next_chapter_num}

The writer's instruction (it may describe ONE task, or SEVERAL distinct,
unrelated tasks run together in the same text - e.g. "add a character named
X ... also make chapter 3 darker ... and rename the tone to wry" is three
separate tasks). Split it into however many distinct tasks it actually
contains - do not invent tasks that aren't there, and do not split a single
task's own description into pieces:
\"\"\"
{prompt}
\"\"\"

For EACH distinct task, decide ONE action:
- "create_character" - describes a brand-new character to add
- "create_world" - describes a brand-new location/faction/object/rule to add
- "create_note" - asks to research/look into a NEW topic (setting, technical,
  period, terminology detail) not already in the notes listed above
- "create_timeline_event" - describes a brand-new story event/date to add to
  the timeline (must NOT already match one of the timeline events above)
- "create_outline_entry" - describes a brand-new chapter to add to the outline
- "revise_outline_whole" - wants to restructure/change the outline broadly
  (add/remove/reorder multiple chapters, shift a subplot, compress an act)
  rather than describing one new chapter
- "revise_outline_entry" - wants to change the PLAN for one specific chapter
  that is in the outline but NOT drafted yet (chapter number must be in the
  outline list above but not the drafted list)
- "revise_engine" - wants to change premise/tone/narrative voice/narrative
  engine/themes
- "revise_character" - wants to change an EXISTING character (must name or
  clearly imply one of the existing characters listed above)
- "revise_world" - wants to change an EXISTING world entry (must name or
  clearly imply one of the existing world entries listed above)
- "revise_note" - wants to change an EXISTING note (must name or clearly
  imply one of the existing notes listed above)
- "revise_timeline_event" - wants to change an EXISTING timeline event (must
  name or clearly imply one of the existing timeline events listed above)
- "revise_chapter" - wants to change the drafted PROSE of a specific,
  already-drafted chapter (must give or clearly imply a chapter number from
  the drafted list above)
- "plan_scenes" - wants a chapter broken into scenes / re-planned at the
  scene level (must give or clearly imply an outline chapter number)
- "revise_scene" - wants to change a SPECIFIC scene within a chapter (must
  give or clearly imply both a chapter number and a scene number, e.g.
  "scene 2 of chapter 3")
- "unclear" - LAST RESORT ONLY: the instruction genuinely cannot be mapped
  to any action above even with a generous reading. When you use it, include
  a "question" - ONE short, specific question whose answer would let you
  route the task (e.g. "Which character do you mean - Mara or the
  innkeeper?", "Should this change the drafted prose of chapter 3 or the
  outline plan for it?").

Prefer a decisive best-effort mapping over "unclear". If the writer names
something that doesn't exist yet but describes it, treat it as the matching
create_* action. Match names loosely (case, partial names, obvious aliases
map to the existing entries above).

Respond with ONLY a JSON array, one object per distinct task (a single
object in the array if there's only one task), like:
[{{"action": "...", "instruction": "the part of the writer's text describing just this one task, lightly cleaned up so it reads as a standalone instruction on its own", "target_name": "... or null (only for revise_character/revise_world/revise_note/revise_timeline_event - must exactly match an existing name above)", "chapter_num": <int or null (for revise_chapter/revise_outline_entry/plan_scenes/revise_scene)>, "scene_num": <int or null (only for revise_scene)>, "question": "only when action is unclear - one short clarifying question"}}]"""

        raw_results = self.ask_json(instructions)
        if isinstance(raw_results, dict):
            raw_results = [raw_results]
        if not isinstance(raw_results, list) or not raw_results:
            raw_results = [{}]

        # Loose name resolution: exact, else case-insensitive, else unique
        # substring match - so "the innkeeper" still resolves to "The Innkeeper
        # at Dell" instead of bouncing the whole task back as unclear.
        name_pools = {
            "characters": characters,
            "world": world,
            "research_notes": [n for n in notes if n],
            "timeline": timeline_events,
        }

        def resolve_name(kind: str, raw: str | None) -> tuple[str | None, list[str]]:
            """Returns (resolved_name, candidates). candidates is only
            populated when raw matched more than one entry (ambiguous) -
            callers use that to ask which one was meant, instead of silently
            falling through to "no match" and creating a duplicate entity."""
            if not raw:
                return None, []
            pool = name_pools[kind]
            if raw in pool:
                return raw, []
            lowered = raw.lower()
            ci = [n for n in pool if n.lower() == lowered]
            if len(ci) == 1:
                return ci[0], []
            partial = [n for n in pool if lowered in n.lower() or n.lower() in lowered]
            if len(partial) == 1:
                return partial[0], []
            candidates = ci or partial
            return None, (candidates if len(candidates) > 1 else [])

        REVISE_TO_CREATE = {
            "revise_character": "create_character",
            "revise_world": "create_world",
            "revise_note": "create_note",
            "revise_timeline_event": "create_timeline_event",
        }
        REVISE_KIND = {
            "revise_character": "characters",
            "revise_world": "world",
            "revise_note": "research_notes",
            "revise_timeline_event": "timeline",
        }

        tasks = []
        next_num = next_chapter_num
        for result in raw_results:
            if not isinstance(result, dict):
                continue

            action = result.get("action")
            if action not in VALID_ACTIONS:
                action = "unclear"

            target_name = result.get("target_name")
            target_name = target_name.strip() if isinstance(target_name, str) and target_name.strip() else None

            chapter_num = result.get("chapter_num")
            chapter_num = chapter_num if isinstance(chapter_num, int) else None

            scene_num = result.get("scene_num")
            scene_num = scene_num if isinstance(scene_num, int) else None

            task_instruction = result.get("instruction")
            task_instruction = task_instruction.strip() if isinstance(task_instruction, str) and task_instruction.strip() else prompt

            question = result.get("question")
            question = question.strip() if isinstance(question, str) and question.strip() else None

            if action in REVISE_KIND:
                resolved, candidates = resolve_name(REVISE_KIND[action], target_name)
                if resolved is not None:
                    target_name = resolved
                elif candidates:
                    # Multiple existing entries matched - ask which one rather
                    # than silently creating a duplicate.
                    action = "unclear"
                    question = question or (
                        f"\"{target_name}\" could mean more than one existing entry - "
                        f"which did you mean: {', '.join(candidates)}?"
                    )
                    target_name = None
                else:
                    # Adapt instead of rejecting: revising something that
                    # doesn't exist yet means the writer wants it to exist.
                    action = REVISE_TO_CREATE[action]
                    target_name = None

            if action == "revise_chapter" and (chapter_num is None or chapter_num not in drafted_chapters):
                if chapter_num is not None and chapter_num in outline_chapters:
                    # Chapter is planned but not drafted - revise the plan.
                    action = "revise_outline_entry"
                else:
                    action = "unclear"
                    question = question or (
                        f"Chapter {chapter_num} isn't in the outline yet - which chapter did you mean, "
                        "or should this become a new outline entry?"
                        if chapter_num is not None
                        else "Which chapter should this apply to?"
                    )
            if action == "revise_outline_entry" and (chapter_num is None or chapter_num not in outline_chapters):
                action = "unclear"
                question = question or "Which outline chapter should this apply to?"

            if action == "plan_scenes" and (chapter_num is None or chapter_num not in outline_chapters):
                action = "unclear"
                question = question or "Which outline chapter should this plan scenes for?"

            if action == "revise_scene":
                entry = bible.outline_entry(chapter_num) if chapter_num is not None else None
                scene_nums = {s["scene_num"] for s in (entry.get("scenes") or [])} if entry else set()
                if entry is None or scene_num is None or scene_num not in scene_nums:
                    action = "unclear"
                    question = question or "Which chapter and scene number should this apply to?"

            entry_next_chapter_num = None
            if action == "create_outline_entry":
                entry_next_chapter_num = next_num
                next_num += 1

            tasks.append({
                "action": action,
                "instruction": task_instruction,
                "target_name": target_name,
                "chapter_num": chapter_num,
                "scene_num": scene_num if action == "revise_scene" else None,
                "next_chapter_num": entry_next_chapter_num,
                "question": question if action == "unclear" else None,
            })

        if not tasks:
            tasks = [{"action": "unclear", "instruction": prompt, "target_name": None, "chapter_num": None, "scene_num": None, "next_chapter_num": None, "question": None}]

        return tasks
