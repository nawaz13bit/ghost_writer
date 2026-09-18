"""Project-level endpoints: create/import/export/rename/delete, series
attachment, the overview (premise/tone/voice/themes) fields, and the
universal-prompt router that classifies a freeform instruction into tasks
for the UI to dispatch."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from ghostwriter.agents.base import AIOutputError
from ghostwriter.llm_client import LLMCancelled
from ghostwriter.length_categories import DEFAULT_LENGTH_CATEGORY, LENGTH_CATEGORIES
from ghostwriter.memory.series_bible import SeriesBible
from ghostwriter.memory.story_bible import StoryBible, slugify
from ghostwriter.export.pdf_export import build_pdf
from ghostwriter.tools.epub_export import build_epub
from ghostwriter.tools.manuscript_import import CHAPTER_EXTENSIONS, import_manuscript
from ghostwriter.webui.deps import current_chapter_text, get_author, load_bible, resolve_length, with_bible_lock
from ghostwriter.webui.state import blurb_agent, cfg, discuss_agent, outliner, project_analyzer, researcher, router_agent, translators, world_builder, character_builder

logger = logging.getLogger(__name__)

router = APIRouter(tags=["projects"])


class NewProjectRequest(BaseModel):
    title: str
    genre: str
    premise: str
    length_category: str = DEFAULT_LENGTH_CATEGORY
    num_chapters: int | None = None
    chapter_target_words: int | None = None
    total_word_target: int | None = None
    location: str | None = None


class NewCharacterRequest(BaseModel):
    name: str
    role: str = "supporting"
    description: str = ""
    is_real: bool = False


class NewWorldEntryRequest(BaseModel):
    name: str
    category: str = "general"
    content: str = ""
    is_real: bool = False


@router.get("/api/length-categories")
def get_length_categories() -> dict[str, Any]:
    return {"categories": LENGTH_CATEGORIES, "default": DEFAULT_LENGTH_CATEGORY}


@router.get("/api/projects")
def list_projects() -> list[str]:
    projects_dir = Path(cfg["paths"]["projects_dir"])
    if not projects_dir.exists():
        return []
    return sorted(
        p.name for p in projects_dir.iterdir()
        if p.is_dir() and (p / "story_bible.json").exists()
    )


@router.get("/api/projects/{slug}")
def get_project(slug: str) -> dict[str, Any]:
    bible = load_bible(slug)
    return {**bible.data, "series_sync_pending": bible.series_sync_pending()}


@router.get("/api/projects/{slug}/export")
def export_manuscript(slug: str, language: str | None = None) -> Response:
    """Assembles every chapter that has any text (draft or later revision) into
    one Markdown file, in chapter order, for the writer to take out of
    ghost_writer entirely. Unfinalized chapters are included too - export is
    about getting text out, not about only shipping approved work.

    When language is given, pulls each chapter's translated text instead
    (chapters with no translation for that language are skipped)."""
    bible = load_bible(slug)
    chapters = sorted(bible.data.get("chapters", []), key=lambda c: c["chapter_num"])
    title = bible.data.get("title") or slug
    parts = [f"# {title}\n"]
    for ch in chapters:
        if language:
            text = ((ch.get("translations") or {}).get(language) or {}).get("text", "")
        else:
            text = current_chapter_text(ch)
        if not text.strip():
            continue
        heading = ch.get("title") or f"Chapter {ch['chapter_num']}"
        parts.append(f"\n\n## Chapter {ch['chapter_num']}: {heading}\n\n{text}")
    content = "".join(parts)
    suffix = f".{language}" if language else ""
    filename = f"{slug}{suffix}.md"
    return Response(
        content=content,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/projects/{slug}/export-epub")
def export_epub(slug: str, language: str | None = None) -> Response:
    """Same chapter-inclusion rule as the Markdown export, but packaged as a
    real EPUB3 file KDP can accept directly as an ebook upload. When language
    is given, uses that chapter's translated text and sets dc:language to its
    ISO code instead of "en"."""
    bible = load_bible(slug)
    lang_code = "en"
    if language:
        if language not in translators:
            raise HTTPException(400, f"Unknown language {language!r}")
        lang_code = translators[language].language.lang_code
    content = build_epub(bible.data, language=language, lang_code=lang_code)
    suffix = f".{language}" if language else ""
    filename = f"{slug}{suffix}.epub"
    return Response(
        content=content,
        media_type="application/epub+zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/projects/{slug}/export-pdf")
def export_pdf(slug: str, language: str | None = None) -> Response:
    """Print-ready PDF with title/copyright/foreword/acknowledgments/contents
    front matter and an about-the-author back page, built from the same bible
    fields as the EPUB export. Unlike the other exports, only chapters with
    finalized text are included - page numbers only mean something for text
    that isn't going to move around anymore.

    Translated PDF export is only available for Latin-script languages -
    reportlab's default fonts can't render Japanese/Russian/Bengali without a
    bundled Unicode font, which this app doesn't ship, so those languages are
    rejected here rather than silently producing a PDF full of missing
    glyphs; use the EPUB or Markdown export for those instead."""
    bible = load_bible(slug)
    if language:
        if language not in translators:
            raise HTTPException(400, f"Unknown language {language!r}")
        if not translators[language].language.pdf_supported:
            raise HTTPException(
                400,
                f"{translators[language].language.name} PDF export isn't supported (non-Latin script) - "
                "use the EPUB or Markdown export instead",
            )
    content = build_pdf(bible.data, language=language)
    suffix = f".{language}" if language else ""
    filename = f"{slug}{suffix}.pdf"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/api/projects/{slug}")
def delete_project(slug: str) -> dict[str, Any]:
    projects_dir = Path(cfg["paths"]["projects_dir"])
    project_dir = (projects_dir / slug).resolve()
    if project_dir.parent != projects_dir.resolve() or not (project_dir / "story_bible.json").exists():
        raise HTTPException(404, f"No project {slug!r}")
    shutil.rmtree(project_dir)
    return {"deleted": slug}


def _project_real_dir(projects_dir: Path, slug: str) -> Path:
    """The project's current real (non-junction) directory: projects_dir/slug
    itself, or wherever a prior _relocate_project call moved it to if that
    path is now a directory junction pointing elsewhere."""
    link_path = projects_dir / slug
    # Containment check on the *link* path itself (not its resolved target -
    # a junction's target is expected to live elsewhere, that's the point of
    # relocation). A bare slug always joins to exactly projects_dir/slug; this
    # only trips if slug smuggled in path separators or "..", which slugify()
    # already prevents at creation time - this is defense-in-depth for any
    # caller that didn't go through slugify().
    if link_path.parent != projects_dir:
        raise HTTPException(404, f"No project {slug!r}")
    if os.path.isjunction(link_path):
        return link_path.resolve()
    return link_path


def _relocate_project(projects_dir: Path, slug: str, destination: str) -> Path:
    """Physically moves a project's folder to `destination` (an absolute
    parent folder - e.g. a different drive the writer wants to store it on),
    leaving a directory junction at the usual projects_dir/slug path. Every
    other route (list/load/delete/rename/...) only ever looks at
    projects_dir/slug, so the junction is what lets them keep working with
    zero changes even though the data now lives elsewhere. Junctions (unlike
    symlinks) need no elevated Windows privilege and work across drives."""
    link_path = projects_dir / slug
    real_dir = _project_real_dir(projects_dir, slug)
    if not (real_dir / "story_bible.json").exists():
        raise HTTPException(404, f"No project {slug!r}")

    dest = Path(destination.strip())
    if not dest.is_absolute():
        raise HTTPException(400, "Location must be an absolute folder path")
    if dest == real_dir.parent:
        # Already there - a no-op, not an error. Matters most for the
        # create/finalize flow: it relocates the folder before generating the
        # outline, so if outline generation fails (e.g. no LLM server running)
        # and the writer retries with the same location, this must not block
        # the retry.
        return real_dir
    dest.mkdir(parents=True, exist_ok=True)
    new_dir = dest / slug
    if new_dir.exists():
        raise HTTPException(400, f"{new_dir} already exists")

    was_junction = os.path.isjunction(link_path)
    shutil.move(str(real_dir), str(new_dir))
    if was_junction:
        link_path.rmdir()
    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(new_dir)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        shutil.move(str(new_dir), str(real_dir))  # keep project reachable at its usual path
        raise HTTPException(500, f"Could not create a link back to {link_path}: {exc.stderr}")

    return new_dir


class MoveProjectRequest(BaseModel):
    destination: str


@router.post("/api/projects/{slug}/move")
def move_project(slug: str, req: MoveProjectRequest) -> dict[str, Any]:
    """Relocates an existing project's folder to a different location on
    disk. See _relocate_project for how the project stays reachable at its
    usual projects_dir/slug path afterward."""
    projects_dir = Path(cfg["paths"]["projects_dir"])
    new_dir = _relocate_project(projects_dir, slug, req.destination)
    return {"slug": slug, "location": str(new_dir)}


class RenameProjectRequest(BaseModel):
    new_title: str


@router.post("/api/projects/{slug}/rename")
@with_bible_lock
def rename_project(slug: str, req: RenameProjectRequest) -> dict[str, Any]:
    """Renames the book title and, to keep the folder/slug in sync (it's the
    stable ID used in URLs and file paths), moves the project directory to
    match. Series books keep their `{series_slug}__` directory prefix so the
    rename can't collide with a standalone project of the same title."""
    new_title = req.new_title.strip()
    if not new_title:
        raise HTTPException(400, "Title is required")
    bible = load_bible(slug)

    series_title = bible.data.get("series_title")
    new_book_slug = slugify(new_title)
    new_slug = f"{slugify(series_title)}__{new_book_slug}" if series_title else new_book_slug

    projects_dir = Path(cfg["paths"]["projects_dir"])
    if new_slug != slug:
        new_dir = projects_dir / new_slug
        if new_dir.exists():
            raise HTTPException(400, f"A project already exists at {new_slug!r} - pick a different title")
        bible.project_dir.rename(new_dir)
        bible = StoryBible.load(projects_dir, new_title, dir_slug=new_slug)

    bible.data["title"] = new_title
    bible.save()
    return {"slug": new_slug, "title": new_title}


class SeriesAttachRequest(BaseModel):
    series_title: str
    book_num: int = 1


@router.post("/api/projects/{slug}/series")
@with_bible_lock
def attach_project_to_series(slug: str, req: SeriesAttachRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    if bible.data.get("series_title"):
        raise HTTPException(400, f"Project is already part of series {bible.data['series_title']!r}")
    series_title = req.series_title.strip()
    if not series_title:
        raise HTTPException(400, "Series name is required")

    series = SeriesBible.load_or_create(
        cfg["paths"]["series_dir"], series_title, bible.data.get("genre", ""), bible.data.get("premise", "")
    )
    try:
        bible.attach_to_series(series, req.book_num)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return {
        "slug": bible.project_dir.name,
        "series_title": bible.data["series_title"],
        "series_book_num": bible.data["series_book_num"],
    }


@router.post("/api/projects/{slug}/series/sync")
@with_bible_lock
def sync_project_to_series(slug: str) -> dict[str, Any]:
    """Re-pushes this book's current characters/world/notes/timeline (including
    any status-changing events, e.g. a character death) up into the series
    bible, without waiting for the book to finish - so a later book started
    from the series canon, or the Series Bible page, sees it right away."""
    bible = load_bible(slug)
    if not bible.data.get("series_title"):
        raise HTTPException(400, "Project is not part of a series")
    series = SeriesBible.load(cfg["paths"]["series_dir"], bible.data["series_title"])
    bible.sync_to_series(series)
    return {"slug": slug, "series_title": bible.data["series_title"]}


class OverviewEditRequest(BaseModel):
    premise: str = ""
    tone: str = ""
    narrative_voice: str = ""
    narrative_engine: str = ""
    themes: str = ""
    real_world_setting: bool = False
    chapter_target_words: int | None = None
    total_word_target: int | None = None
    author_name: str = ""
    blurb: str = ""
    query_letter: str = ""
    copyright_text: str = ""
    foreword: str = ""
    acknowledgments: str = ""
    about_author: str = ""


@router.post("/api/projects/{slug}/overview")
@with_bible_lock
def edit_overview(slug: str, req: OverviewEditRequest) -> dict[str, Any]:
    bible = load_bible(slug)
    bible.data["premise"] = req.premise.strip()
    bible.data["tone"] = req.tone.strip()
    bible.data["narrative_voice"] = req.narrative_voice.strip()
    bible.data["narrative_engine"] = req.narrative_engine.strip()
    bible.data["themes"] = req.themes.strip()
    bible.data["real_world_setting"] = req.real_world_setting
    if req.chapter_target_words:
        bible.data["chapter_target_words"] = req.chapter_target_words
    if req.total_word_target:
        bible.data["total_word_target"] = req.total_word_target
    bible.data["author_name"] = req.author_name.strip()
    bible.data["blurb"] = req.blurb.strip()
    bible.data["query_letter"] = req.query_letter.strip()
    bible.data["copyright_text"] = req.copyright_text.strip()
    bible.data["foreword"] = req.foreword.strip()
    bible.data["acknowledgments"] = req.acknowledgments.strip()
    bible.data["about_author"] = req.about_author.strip()
    bible.save()
    return bible.data


@router.post("/api/projects/{slug}/generate-blurb")
def generate_blurb(slug: str) -> dict[str, str]:
    """Generates a back-cover blurb and a literary-agent query letter from
    the bible's premise/characters/outline. Review-gated like every other AI
    write in this app: returns the draft for the writer to look over in the
    Book details fields, doesn't save it until they hit Save themselves."""
    bible = load_bible(slug)
    try:
        author = get_author(bible)
        return blurb_agent.generate(bible, voice_prompt=author.system_prompt)
    except AIOutputError as exc:
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")


class ReviseOverviewRequest(BaseModel):
    instruction: str


@router.post("/api/projects/{slug}/overview/revise")
def revise_overview(slug: str, req: ReviseOverviewRequest) -> dict[str, Any]:
    """Drafts a revision of premise/tone/narrative_voice/narrative_engine/themes
    per an instruction. Does NOT save - the writer reviews/edits the result
    and confirms via the normal Save button, same as every other AI draft."""
    if not req.instruction.strip():
        raise HTTPException(400, "An instruction is required")
    bible = load_bible(slug)
    try:
        return project_analyzer.revise(bible, req.instruction)
    except AIOutputError as exc:
        logger.exception("Story engine revise failed for project %r", slug)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again or rephrase the instruction.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")


class UniversalPromptRequest(BaseModel):
    prompt: str


@router.post("/api/projects/{slug}/universal-prompt")
def universal_prompt(slug: str, req: UniversalPromptRequest) -> dict[str, Any]:
    """Classifies a freeform instruction - which may describe one task or
    several distinct tasks - so the writer doesn't have to navigate to a
    section first. The UI works through the returned tasks one at a time,
    dispatching each to whichever section's existing create/revise flow
    already handles it."""
    if not req.prompt.strip():
        raise HTTPException(400, "A prompt is required")
    bible = load_bible(slug)
    try:
        tasks = router_agent.classify(bible, req.prompt)
    except AIOutputError as exc:
        logger.exception("Universal prompt routing failed for project %r", slug)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again or rephrase the instruction.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")
    return {"tasks": tasks}


class DiscussTurn(BaseModel):
    role: str  # "writer" | "assistant"
    text: str


class UniversalDiscussRequest(BaseModel):
    history: list[DiscussTurn]


@router.post("/api/projects/{slug}/universal-discuss")
def universal_discuss(slug: str, req: UniversalDiscussRequest) -> dict[str, Any]:
    """The "Discuss" side of the universal prompt bar's Discuss/Do toggle:
    talks through an idea across the whole passed-in transcript instead of
    generating a concrete task. May end with one suggested instruction the
    writer can send to /universal-prompt, but never dispatches anything
    itself - a suggestion still goes through the normal classify+review path."""
    if not req.history or not req.history[-1].text.strip():
        raise HTTPException(400, "A message is required")
    bible = load_bible(slug)
    try:
        result = discuss_agent.discuss(bible, [turn.model_dump() for turn in req.history])
    except AIOutputError as exc:
        logger.exception("Universal discuss failed for project %r", slug)
        raise HTTPException(502, f"The AI returned an unexpected response ({exc}) - try again or rephrase.")
    except LLMCancelled:
        raise HTTPException(409, "Stopped by user.")
    return result


@router.post("/api/projects")
def create_project(req: NewProjectRequest) -> dict[str, Any]:
    category = LENGTH_CATEGORIES.get(req.length_category)
    if category is None:
        raise HTTPException(400, f"Unknown length_category {req.length_category!r}")
    chapters, words_per_chapter, total_words = resolve_length(
        category, req.num_chapters, req.chapter_target_words, req.total_word_target
    )

    slug = slugify(req.title)
    bible = StoryBible.load_or_create(cfg["paths"]["projects_dir"], req.title, req.genre, req.premise)
    if req.location:
        _relocate_project(Path(cfg["paths"]["projects_dir"]), slug, req.location)
    if "chapter_target_words" not in bible.data:
        bible.data["length_category"] = req.length_category
        bible.data["chapter_target_words"] = words_per_chapter
        bible.data["total_word_target"] = total_words
        bible.save()

    if not bible.data.get("research_done"):
        researcher.research(bible, None)
        bible.data["research_done"] = True
        bible.save()
    if not bible.data.get("world_built"):
        author = get_author(bible)
        world_builder.build(bible, voice_prompt=author.system_prompt)
        bible.data["world_built"] = True
        bible.save()
    if not bible.data.get("characters_built"):
        author = get_author(bible)
        character_builder.build(bible, voice_prompt=author.system_prompt)
        bible.data["characters_built"] = True
        bible.save()
    if not bible.data["outline"]:
        author = get_author(bible)
        outliner.build(bible, chapters, category.get("acts", 3), words_per_chapter=words_per_chapter, voice_prompt=author.system_prompt)
        bible.save()

    return {"slug": slug}


class AnalyzeConceptRequest(BaseModel):
    prompt: str


@router.post("/api/projects/analyze")
def analyze_concept(req: AnalyzeConceptRequest) -> dict[str, Any]:
    """Analyzes one freeform concept dump into suggested new-project fields,
    without creating anything - the writer reviews/edits the draft in the UI
    and only /api/projects/finalize actually persists a project."""
    if not req.prompt.strip():
        raise HTTPException(400, "Concept text is required")
    return project_analyzer.analyze(req.prompt)


class FinalizeProjectRequest(BaseModel):
    title: str
    genre: str
    premise: str
    book_type: str = "fiction"
    tone: str = ""
    narrative_voice: str = ""
    narrative_engine: str = ""
    themes: str = ""
    real_world_setting: bool = False
    length_category: str = DEFAULT_LENGTH_CATEGORY
    num_chapters: int | None = None
    chapter_target_words: int | None = None
    total_word_target: int | None = None
    characters: list[NewCharacterRequest] = []
    world: list[NewWorldEntryRequest] = []
    location: str | None = None


@router.post("/api/projects/finalize")
def finalize_project(req: FinalizeProjectRequest) -> dict[str, Any]:
    """Creates a project from a reviewed/edited analysis draft (or from
    manually-filled fields if the writer skipped analysis) - skips the
    auto research/world/character generation from /api/projects since the
    writer already supplied that content, but still generates the outline."""
    category = LENGTH_CATEGORIES.get(req.length_category)
    if category is None:
        raise HTTPException(400, f"Unknown length_category {req.length_category!r}")
    if not req.premise.strip():
        raise HTTPException(400, "A premise is required")
    if req.book_type not in ("fiction", "nonfiction"):
        raise HTTPException(400, f"Unknown book_type {req.book_type!r}")
    chapters, words_per_chapter, total_words = resolve_length(
        category, req.num_chapters, req.chapter_target_words, req.total_word_target
    )

    slug = slugify(req.title)
    bible = StoryBible.load_or_create(cfg["paths"]["projects_dir"], req.title, req.genre, req.premise)
    if req.location:
        _relocate_project(Path(cfg["paths"]["projects_dir"]), slug, req.location)
    bible.data["book_type"] = req.book_type
    bible.data["tone"] = req.tone.strip()
    bible.data["narrative_voice"] = req.narrative_voice.strip()
    bible.data["narrative_engine"] = req.narrative_engine.strip()
    bible.data["themes"] = req.themes.strip()
    bible.data["real_world_setting"] = req.real_world_setting
    bible.data["length_category"] = req.length_category
    bible.data["chapter_target_words"] = words_per_chapter
    bible.data["total_word_target"] = total_words
    bible.data["research_done"] = True
    bible.data["world_built"] = True
    bible.data["characters_built"] = True
    bible.save()

    for c in req.characters:
        if bible.find_character(c.name) is None:
            bible.add_character(c.name, c.role, c.description, c.is_real)
    for w in req.world:
        if bible.find_world_entry(w.name) is None:
            bible.add_world_entry(w.name, w.category, w.content, w.is_real)

    if not bible.data["outline"]:
        author = get_author(bible)
        outliner.build(bible, chapters, category.get("acts", 3), words_per_chapter=words_per_chapter, voice_prompt=author.system_prompt)
        bible.save()

    return {"slug": slug}


@router.post("/api/import")
async def import_project(
    title: str = Form(...),
    genre: str = Form(...),
    premise: str = Form(...),
    length_category: str = Form(DEFAULT_LENGTH_CATEGORY),
    chapter_target_words: int | None = Form(None),
    total_word_target: int | None = Form(None),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    """Imports uploaded .docx/.md/.txt files into a new or existing project.
    Files are staged into a temp dir (flattened - subfolders from a
    webkitdirectory picker aren't preserved, only used to dedupe names) since
    import_manuscript works off a directory on disk."""
    if length_category not in LENGTH_CATEGORIES:
        raise HTTPException(400, f"Unknown length_category {length_category!r}")

    slug = slugify(title)
    existing_path = Path(cfg["paths"]["projects_dir"]) / slug / "story_bible.json"
    if existing_path.exists():
        prior = StoryBible.load(cfg["paths"]["projects_dir"], title)
        prior_characters = len(prior.data["characters"])
        prior_world = len(prior.data["world"])
        prior_notes = len(prior.data["research_notes"])
    else:
        prior_characters = prior_world = prior_notes = 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        seen_names: set[str] = set()
        staged = 0
        for f in files:
            name = Path(f.filename or "").name
            if Path(name).suffix.lower() not in CHAPTER_EXTENSIONS:
                continue
            dest_name = name
            n = 1
            while dest_name in seen_names:
                stem, suffix = Path(name).stem, Path(name).suffix
                dest_name = f"{stem}__{n}{suffix}"
                n += 1
            seen_names.add(dest_name)
            (tmp_dir / dest_name).write_bytes(await f.read())
            staged += 1

        if staged == 0:
            raise HTTPException(400, "No .docx/.md/.txt files found in the upload.")

        try:
            # import_manuscript is a long, fully synchronous pipeline (disk I/O,
            # docx parsing, per-block LLM classification calls) - run it off the
            # event loop thread so it doesn't stall other requests (e.g. polling
            # an in-progress draft/finalize job) for the duration of the import.
            bible = await asyncio.to_thread(
                import_manuscript,
                dir_path=tmp_dir,
                title=title,
                genre=genre,
                premise=premise,
                projects_dir=cfg["paths"]["projects_dir"],
                cfg=cfg,
                length_category=length_category,
                chapter_target_words=chapter_target_words,
                total_word_target=total_word_target,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    pending_chapters = sum(1 for ch in bible.data["chapters"] if not ch.get("approved"))
    return {
        "slug": bible.project_dir.name,
        "chapters": len(bible.data["chapters"]),
        "pending_chapters": pending_chapters,
        "outline_entries": len(bible.data["outline"]),
        "new_characters": len(bible.data["characters"]) - prior_characters,
        "new_world_entries": len(bible.data["world"]) - prior_world,
        "note_entries": len(bible.data["research_notes"]) - prior_notes,
    }
