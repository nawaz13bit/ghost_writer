"""Compiles the story bible's finished chapters into a Kindle/KDP-friendly .docx.

Kindle conversion (KDP / Kindle Create) builds its navigable table of contents
from Heading 1 styles in the source docx, so the important thing is: use real
paragraph styles for chapter titles (not bold-manual-formatting), one Heading 1
per chapter, and avoid manual page-number headers/footers since Kindle reflows
text. We keep a plain-text contents list on the title page as a human-readable
bonus, but the authoritative navigation comes from the Heading 1 styles.
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from ghostwriter.memory.story_bible import StoryBible


def _style_base_document(doc: Document) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = "Georgia"
    normal.font.size = Pt(12)
    normal.paragraph_format.line_spacing = 1.15
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.first_line_indent = Pt(24)


def _add_paragraphs(doc: Document, text: str) -> None:
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        p = doc.add_paragraph(para)
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY


def export_docx(bible: StoryBible, author_name: str = "Anonymous") -> Path:
    doc = Document()
    _style_base_document(doc)

    # Title page
    title_p = doc.add_paragraph(bible.data["title"])
    title_p.style = doc.styles["Title"]
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    by_p = doc.add_paragraph(f"by {author_name}")
    by_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    by_p.paragraph_format.first_line_indent = Pt(0)

    chapters = sorted(bible.data["chapters"], key=lambda c: c["chapter_num"])
    finished = [c for c in chapters if c.get("final")]

    if finished:
        doc.add_page_break()
        contents_heading = doc.add_paragraph("Contents")
        contents_heading.style = doc.styles["Heading 1"]
        for ch in finished:
            entry = doc.add_paragraph(f"Chapter {ch['chapter_num']}: {ch.get('title', '')}")
            entry.paragraph_format.first_line_indent = Pt(0)

    for ch in finished:
        doc.add_page_break()
        heading = doc.add_paragraph(f"Chapter {ch['chapter_num']}: {ch.get('title', '')}")
        heading.style = doc.styles["Heading 1"]
        _add_paragraphs(doc, ch["final"])

    output_path = bible.project_dir / f"{bible.project_dir.name}.docx"
    doc.save(output_path)
    return output_path
