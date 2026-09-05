"""Imports an already-drafted manuscript (a folder of .docx/.md/.txt files)
into a story_bible.json, so a project started outside ghost_writer can be
continued/edited through the pipeline or the web UI.

Files aren't assumed to be one clean chapter each - a single file may mix
finished prose, outline/synopsis notes, and unrelated freeform thoughts. Each
file is split into blocks (on headings and blank-line runs) and, if
llama-server is reachable, a ManuscriptClassifierAgent labels each block as
prose/outline/notes. The classifier only ever labels blocks - it never
reproduces or rewrites their text - so the writer's original words always
reach the story bible unmodified.

Routing:
  - prose blocks become a chapter revision (source="import") plus the
    chapter's "final" text, so pipeline.py's resume logic won't re-draft
    them - but "approved" is deliberately left unset, so they show up as
    pending review in the web UI rather than silently counting as
    human-approved.
  - outline blocks become/update an outline entry (title + summary) without
    touching chapter text.
  - character blocks with a recognizable entity_name are appended to that
    character's description (creating the character if new); world blocks
    with a recognizable entity_name are appended to that world entry's
    content the same way. Multiple blocks about the same person/place
    across files merge into one entry instead of duplicating it.
  - everything else (notes, brainstorming, skeleton/outline fragments the
    classifier can't tie to a specific chapter number, or anything at all if
    llama-server isn't running) is filed as a research_notes entry - nothing
    is discarded, and it shows up for review/deletion in the web UI's Notes
    panel like any other entity (previously these were dumped into World,
    which mixed unsorted scratch material in with real world-building
    content).

If you want the pipeline to keep drafting chapters beyond what was imported,
extend "outline" in story_bible.json yourself with entries for the remaining
chapter numbers (already-final chapters are skipped by chapter_num either
way).
"""
from __future__ import annotations

import re
from pathlib import Path

from docx import Document

from ghostwriter.agents.manuscript_classifier import ManuscriptClassifierAgent
from ghostwriter.length_categories import DEFAULT_LENGTH_CATEGORY, LENGTH_CATEGORIES
from ghostwriter.llm_client import LLMClient
from ghostwriter.memory.story_bible import StoryBible

CHAPTER_EXTENSIONS = {".docx", ".md", ".markdown", ".txt"}
_CHAPTER_PREFIXED_RE = re.compile(r"(?:chap(?:ter)?|ch)[\s_\-]*0*(\d+)", re.IGNORECASE)
_CHAPTER_LEADING_RE = re.compile(r"^\s*0*(\d+)(?=[\s_\-.:)]|$)")


def find_chapter_files(dir_path: Path, recursive: bool = False) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    return sorted(
        p for p in dir_path.glob(pattern)
        if p.is_file() and p.suffix.lower() in CHAPTER_EXTENSIONS and not p.name.startswith("~$")
    )


def extract_chapter_number(text: str) -> int | None:
    """Only trusts a number explicitly prefixed by chapter/ch, or one that
    leads the string outright (the "01 - Title" / "1. Title" convention).
    Deliberately does NOT scan for any stray digit run mid-title (e.g. a
    year or percentage mentioned in the chapter's actual title), since that
    caused unrelated chapters to collide on the same chapter_num and
    silently overwrite each other during import.
    """
    m = _CHAPTER_PREFIXED_RE.search(text)
    if m:
        return int(m.group(1))
    m = _CHAPTER_LEADING_RE.match(text.strip())
    return int(m.group(1)) if m else None


_ENTITY_HEADER_RE = re.compile(r"^\s*([A-Z][\w' \-]{1,60}?)\s*[:\-–—]\s*(.*)$")


def _split_entity_notes(text: str) -> list[tuple[str | None, str]]:
    """A character/world block can still be several people/places separated
    by a single blank line (block-splitting only breaks on >=2 blank lines,
    to avoid fragmenting ordinary prose). Split those apart here so e.g. two
    characters described back-to-back in one note file don't get merged
    into a single character's description. Each paragraph that opens with
    "Name - ..." / "Name: ..." is treated as its own entity; anything else
    is left attached to the block's already-detected entity_name.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) <= 1:
        return [(None, text)]

    results: list[tuple[str | None, str]] = []
    for para in paragraphs:
        first_line, _, rest = para.partition("\n")
        m = _ENTITY_HEADER_RE.match(first_line)
        if m:
            name = m.group(1).strip()
            body = (m.group(2) + ("\n" + rest if rest else "")).strip()
            results.append((name, body or para))
        else:
            results.append((None, para))
    return results


def title_from_filename(path: Path) -> str:
    stem = re.sub(r"^[\d_\-.\s]+", "", path.stem)
    stem = re.sub(r"[_\-]+", " ", stem).strip()
    return stem.title() if stem else path.stem


def naive_summary(text: str, max_chars: int = 280) -> str:
    flat = " ".join(text.split())
    if len(flat) <= max_chars:
        return flat
    return flat[:max_chars].rsplit(" ", 1)[0] + "..."


# -- block extraction: split each file into (heading, body) chunks on
# headings and blank-line runs, deterministically, in plain Python. Content
# is never touched by the LLM at this stage. ------------------------------

def _docx_elements(path: Path) -> list[tuple[str, str]]:
    doc = Document(str(path))
    elements = []
    for p in doc.paragraphs:
        text = p.text.strip()
        style = (p.style.name or "").lower()
        if text and style.startswith(("heading", "title")):
            elements.append(("heading", text))
        elif not text:
            elements.append(("blank", ""))
        else:
            elements.append(("text", text))
    return elements


def _md_elements(path: Path) -> list[tuple[str, str]]:
    text = path.read_text(encoding="utf-8")
    elements = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            elements.append(("heading", stripped.lstrip("#").strip()))
        elif not stripped:
            elements.append(("blank", ""))
        else:
            elements.append(("text", line))
    return elements


def _txt_elements(path: Path) -> list[tuple[str, str]]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    elements = []
    n = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            elements.append(("blank", ""))
            continue
        prev_blank = i == 0 or not lines[i - 1].strip()
        next_blank = i + 1 >= n or not lines[i + 1].strip()
        if prev_blank and next_blank and len(stripped) < 100:
            elements.append(("heading", stripped))
        else:
            elements.append(("text", line))
    return elements


_CHAPTER_LINE_RE = re.compile(r"^\s*(?:chap(?:ter)?|ch\.?)\s*[:#]?\s*0*\d+\b", re.IGNORECASE)


def _group_into_blocks(elements: list[tuple[str, str]]) -> list[dict]:
    """Batch-uploaded files often bundle several already-written chapters into
    one document with "Chapter N" typed as ordinary body text rather than a
    Word heading style, and with only a single blank line between chapters.
    Relying solely on style/blank-run detection silently merged those into
    one giant block that got assigned a single chapter_num, clobbering all
    but the last chapter in the batch. A text line that itself looks like a
    chapter heading is therefore treated as a block boundary too.
    """
    blocks: list[dict] = []
    heading: str | None = None
    buf: list[str] = []
    blank_run = 0

    def flush():
        body = "\n".join(buf).strip()
        body = re.sub(r"\n{3,}", "\n\n", body)
        if heading or body:
            blocks.append({"heading": heading, "text": body})

    for kind, text in elements:
        if kind == "heading" or (kind == "text" and _CHAPTER_LINE_RE.match(text)):
            flush()
            heading = text
            buf = []
            blank_run = 0
        elif kind == "blank":
            blank_run += 1
            buf.append("")
            if blank_run >= 2:
                flush()
                heading = None
                buf = []
                blank_run = 0
        else:
            blank_run = 0
            buf.append(text)
    flush()
    return blocks


def extract_blocks(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        elements = _docx_elements(path)
    elif suffix in (".md", ".markdown"):
        elements = _md_elements(path)
    else:
        elements = _txt_elements(path)
    return _group_into_blocks(elements)


def import_manuscript(
    dir_path: Path,
    title: str,
    genre: str,
    premise: str,
    projects_dir: Path,
    cfg: dict | None = None,
    length_category: str = DEFAULT_LENGTH_CATEGORY,
    recursive: bool = False,
    chapter_target_words: int | None = None,
    total_word_target: int | None = None,
) -> StoryBible:
    if length_category not in LENGTH_CATEGORIES:
        raise ValueError(f"Unknown length_category {length_category!r}; choices: {list(LENGTH_CATEGORIES)}")

    files = find_chapter_files(dir_path, recursive)
    if not files:
        raise ValueError(f"No .docx/.md/.txt files found in {dir_path}")

    bible = StoryBible.load_or_create(projects_dir, title, genre, premise)
    if "chapter_target_words" not in bible.data:
        category = LENGTH_CATEGORIES[length_category]
        bible.data["length_category"] = length_category
        bible.data["chapter_target_words"] = category["words_per_chapter"]
    if chapter_target_words:
        bible.data["chapter_target_words"] = chapter_target_words
    if total_word_target:
        bible.data["total_word_target"] = total_word_target

    classifier = None
    if cfg is not None:
        llm = LLMClient(cfg)
        if llm.is_up():
            classifier = ManuscriptClassifierAgent(llm)
        else:
            print("llama-server not reachable: everything found will be filed as notes "
                  "for manual review instead of auto-sorted into prose/outline. Start the "
                  "server and re-run import to classify automatically.")
    else:
        print("No LLM config given: everything found will be filed as notes for manual review.")

    known_nums = {c["chapter_num"] for c in bible.data["chapters"]} | {o["chapter_num"] for o in bible.data["outline"]}

    entries: list[dict] = []
    for path in files:
        blocks = extract_blocks(path)
        if not blocks:
            print(f"  {path.name}: no text extracted, skipping.")
            continue

        labels = classifier.classify(path.name, blocks) if classifier is not None else {}
        note_idx = 0
        for i, block in enumerate(blocks):
            label = labels.get(i, {"type": "notes", "chapter_num": None, "title": None, "entity_name": None})
            kind = label["type"]
            chapter_num = label["chapter_num"]
            title = label["title"] or block["heading"]
            entity_name = label.get("entity_name") or (block["heading"] if kind in ("character", "world") else None)

            if chapter_num is None and kind in ("prose", "outline"):
                chapter_num = extract_chapter_number(block["heading"] or "") or extract_chapter_number(path.stem)

            if kind == "prose" and not block["text"].strip():
                # A bare heading with no body isn't a finished chapter.
                kind = "outline"

            if kind in ("character", "world") and not entity_name:
                # Can't file it under a named entity - fall back to a note.
                kind = "notes"

            if not title:
                if kind == "notes":
                    note_idx += 1
                    title = f"{title_from_filename(path)} - note {note_idx}"
                else:
                    title = title_from_filename(path)

            if kind in ("character", "world"):
                for sub_name, sub_text in _split_entity_notes(block["text"]):
                    entries.append({
                        "file": path.name,
                        "kind": kind,
                        "chapter_num": None,
                        "title": sub_name or title,
                        "entity_name": sub_name or entity_name,
                        "text": sub_text,
                    })
            else:
                entries.append({
                    "file": path.name,
                    "kind": kind,
                    "chapter_num": chapter_num,
                    "title": title,
                    "entity_name": entity_name,
                    "text": block["text"],
                })

    for entry in entries:
        if entry["chapter_num"] is not None:
            known_nums.add(entry["chapter_num"])

    next_num = 1
    for entry in entries:
        if entry["kind"] in ("prose", "outline") and entry["chapter_num"] is None:
            while next_num in known_nums:
                next_num += 1
            entry["chapter_num"] = next_num
            known_nums.add(next_num)

    prose_count = outline_count = character_count = world_count = notes_count = 0
    with bible.batch_save():
        for entry in entries:
            kind, cn, entry_title, text, source_file, entity_name = (
                entry["kind"], entry["chapter_num"], entry["title"], entry["text"], entry["file"], entry["entity_name"],
            )
            if kind == "prose":
                bible.add_chapter_revision(cn, text, source="import")
                bible.upsert_chapter(cn, title=entry_title, final=text, summary=naive_summary(text), word_count=len(text.split()))
                if bible.outline_entry(cn) is None:
                    bible.data["outline"].append({"chapter_num": cn, "title": entry_title, "summary": naive_summary(text)})
                prose_count += 1
                print(f"  [{source_file}] chapter {cn} prose: {entry_title!r} ({len(text.split())} words) "
                      f"- added as a pending revision; review & approve in the web UI.")
            elif kind == "outline":
                summary = naive_summary(text) if text.strip() else entry_title
                existing = bible.outline_entry(cn)
                if existing:
                    existing["title"] = entry_title
                    existing["summary"] = summary
                else:
                    bible.data["outline"].append({"chapter_num": cn, "title": entry_title, "summary": summary})
                outline_count += 1
                print(f"  [{source_file}] chapter {cn} outline: {entry_title!r}")
            elif kind == "character":
                bible.append_character_facts(entity_name, text or entry_title)
                character_count += 1
                print(f"  [{source_file}] character note merged into {entity_name!r}.")
            elif kind == "world":
                bible.append_world_facts(entity_name, "general", text or entry_title)
                world_count += 1
                print(f"  [{source_file}] world note merged into {entity_name!r}.")
            else:
                bible.add_research_note(entry_title, text or entry_title)
                notes_count += 1
                print(f"  [{source_file}] filed as a note: {entry_title!r} - review under Notes in the web UI.")

        bible.data["outline"].sort(key=lambda o: o["chapter_num"])
    print(f"Done: {prose_count} prose block(s), {outline_count} outline block(s), "
          f"{character_count} character note(s), {world_count} world note(s), {notes_count} misc note(s) imported.")
    return bible
