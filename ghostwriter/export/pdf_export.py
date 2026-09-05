"""Builds a print-ready PDF: title page, copyright page, optional foreword/
acknowledgments, a page-numbered table of contents, chapters, and an optional
about-the-author page - the front/back matter that docx (Kindle-reflow) and
epub (reflowable) intentionally skip, since only a fixed-page format like this
one makes page numbers meaningful.

Front matter (foreword/acknowledgments/contents) is numbered with lowercase
roman numerals; the title and copyright pages carry no visible number, same
as most trade paperbacks. Body pages (chapters onward, including "about the
author") restart at Arabic 1 on the first chapter page.

Two-pass build (BaseDocTemplate.multiBuild) so the contents page can print
the real page number each chapter lands on.
"""
from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path
from typing import Any

from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate, Frame, NextPageTemplate, PageBreak, PageTemplate,
    Paragraph, Spacer,
)

from ghostwriter.memory.story_bible import StoryBible

_ROMAN = [
    (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
    (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
]


def _to_roman(n: int) -> str:
    out = []
    for value, symbol in _ROMAN:
        count, n = divmod(n, value)
        out.append(symbol * count)
    return "".join(out)


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("BookTitle", parent=base["Title"], fontSize=28, leading=34, spaceAfter=14),
        "byline": ParagraphStyle("Byline", parent=base["Normal"], fontSize=14, alignment=TA_CENTER, spaceBefore=6),
        "front_heading": ParagraphStyle("FrontHeading", parent=base["Heading1"], alignment=TA_CENTER, spaceAfter=16),
        "front_body": ParagraphStyle("FrontBody", parent=base["Normal"], fontSize=11, leading=16, spaceAfter=10),
        "copyright": ParagraphStyle("Copyright", parent=base["Normal"], fontSize=9.5, leading=13, alignment=TA_CENTER),
        "toc_entry": ParagraphStyle("TocEntry", parent=base["Normal"], fontSize=11, leading=18),
        "chapter_heading": ParagraphStyle("ChapterHeading", parent=base["Heading1"], spaceAfter=18),
        "body": ParagraphStyle("Body", parent=base["Normal"], fontSize=11, leading=16, spaceAfter=8,
                                alignment=TA_JUSTIFY, firstLineIndent=18),
    }


_CITATION_RE = re.compile(r"\[\^([\w-]+)\]")


def _linkify_citations(text: str, numbers: dict[str, int]) -> str:
    """Replaces inline [^id] markers with a superscript internal link to that
    source's entry on the bibliography page - mirrors epub_export.py's
    _linkify_citations, but using reportlab's <a name>/<a href="#..."> markup
    instead of HTML anchors. A marker with no matching research note (stale
    id, or the note was deleted after the chapter was drafted) is left as
    plain text rather than linking to nothing."""

    def repl(m: re.Match) -> str:
        n = numbers.get(m.group(1))
        if n is None:
            return m.group(0)
        return f'<super><a href="#cite-{m.group(1)}" color="blue">[{n}]</a></super>'

    return _CITATION_RE.sub(repl, text)


def _paragraphs(text: str, style: ParagraphStyle, citation_numbers: dict[str, int] | None = None) -> list[Paragraph]:
    out = []
    for para in text.split("\n\n"):
        para = para.strip()
        if para:
            marked = para.replace("\n", "<br/>")
            if citation_numbers:
                marked = _linkify_citations(marked, citation_numbers)
            out.append(Paragraph(marked, style))
    return out


class _PageNumberer:
    """Tracks per-section page numbering across the two BaseDocTemplate
    passes. Front-matter pages get lowercase roman numerals; the title and
    copyright pages are excluded entirely (no footer). Body pages restart at
    Arabic 1 on the first page stamped with the "body" template."""

    def __init__(self) -> None:
        self.first_front_page: int | None = None
        self.first_body_page: int | None = None

    def on_front(self, canvas, doc) -> None:
        if self.first_front_page is None:
            self.first_front_page = doc.page
        canvas.saveState()
        canvas.setFont("Helvetica", 9)
        canvas.drawCentredString(
            doc.pagesize[0] / 2, 0.6 * inch, _to_roman(doc.page - self.first_front_page + 1)
        )
        canvas.restoreState()

    def on_unnumbered(self, canvas, doc) -> None:
        pass

    def on_body(self, canvas, doc) -> None:
        if self.first_body_page is None:
            self.first_body_page = doc.page
        canvas.saveState()
        canvas.setFont("Helvetica", 9)
        canvas.drawCentredString(doc.pagesize[0] / 2, 0.6 * inch, str(doc.page - self.first_body_page + 1))
        canvas.restoreState()


def build_pdf(bible_data: dict[str, Any], language: str | None = None) -> bytes:
    """Returns the raw bytes of a print-ready PDF for the given story bible's
    chapters (dict shape matching StoryBible.data). Chapters with no
    finalized text are skipped, matching the other exports' rule of only
    shipping approved work would be too strict here - unlike the Markdown
    export this is meant to be shared/printed, so only chapters with a
    "final" text are included.

    When language is given, chapters are pulled from that chapter's
    translations field instead of "final" (chapters with no translation for
    that language are skipped). Callers must only pass a language whose
    script is coverable by reportlab's default base-14/WinAnsi fonts (Latin
    scripts) - this function does not register any Unicode font, so
    non-Latin scripts (e.g. Japanese, Russian, Bengali) would render as
    missing/garbled glyphs."""
    title = bible_data.get("title") or "Untitled"
    author_name = bible_data.get("author_name") or "Anonymous"
    chapters = sorted(bible_data.get("chapters", []), key=lambda c: c["chapter_num"])

    def _text(ch: dict[str, Any]) -> str:
        if language:
            return ((ch.get("translations") or {}).get(language) or {}).get("text", "")
        return ch.get("final", "")

    finished = [c for c in chapters if _text(c)]

    # Number citations by first appearance across the manuscript, keeping only
    # research notes actually referenced by an inline [^id] marker somewhere in
    # a finished chapter - mirrors epub_export.py's build_epub.
    notes_by_id = {n["id"]: n for n in bible_data.get("research_notes", [])}
    citation_numbers: dict[str, int] = {}
    for ch in finished:
        for m in _CITATION_RE.finditer(_text(ch)):
            if m.group(1) in notes_by_id:
                citation_numbers.setdefault(m.group(1), len(citation_numbers) + 1)
    cited_notes = sorted(
        ((n, notes_by_id[cid]) for cid, n in citation_numbers.items() if cid in notes_by_id),
        key=lambda pair: pair[0],
    )

    st = _styles()
    numberer = _PageNumberer()
    buf = BytesIO()

    page_w, page_h = letter
    margin = 1 * inch

    def _frame(frame_id: str) -> Frame:
        # Each PageTemplate gets its own Frame instance - Frame carries
        # mutable layout state across pages, and reusing one instance across
        # templates (or across the two build passes below) corrupts it.
        return Frame(margin, margin, page_w - 2 * margin, page_h - 2 * margin, id=frame_id)

    doc = BaseDocTemplate(
        buf, pagesize=letter,
        leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=margin,
        title=title, author=author_name,
    )
    doc.addPageTemplates([
        PageTemplate(id="unnumbered", frames=[_frame("unnumbered")], onPage=numberer.on_unnumbered),
        PageTemplate(id="front", frames=[_frame("front")], onPage=numberer.on_front),
        PageTemplate(id="body", frames=[_frame("body")], onPage=numberer.on_body),
    ])

    toc_entries: list[tuple[str, int]] = []  # filled on the first pass, printed on the second

    def story_for_pass() -> list:
        flow: list = [NextPageTemplate("unnumbered")]

        # Title page
        flow += [Spacer(1, 2.2 * inch), Paragraph(title, st["title"]), Paragraph(f"by {author_name}", st["byline"])]
        flow.append(PageBreak())

        # Copyright page
        copyright_text = bible_data.get("copyright_text") or f"Copyright (c) {author_name}. All rights reserved."
        flow += [Spacer(1, 4.5 * inch)] + _paragraphs(copyright_text, st["copyright"])
        flow.append(NextPageTemplate("front"))
        flow.append(PageBreak())

        foreword = bible_data.get("foreword") or ""
        if foreword.strip():
            flow.append(Paragraph("Foreword", st["front_heading"]))
            flow += _paragraphs(foreword, st["front_body"])
            flow.append(PageBreak())

        acknowledgments = bible_data.get("acknowledgments") or ""
        if acknowledgments.strip():
            flow.append(Paragraph("Acknowledgments", st["front_heading"]))
            flow += _paragraphs(acknowledgments, st["front_body"])
            flow.append(PageBreak())

        # Contents - each entry is an internal link to that chapter's heading
        # anchor (see the "chap-N" anchor added below), not just a page number.
        flow.append(Paragraph("Contents", st["front_heading"]))
        for ch in finished:
            heading = f"Chapter {ch['chapter_num']}: {ch.get('title', '')}"
            page_num = next((p for h, p in toc_entries if h == heading), None)
            linked_heading = f'<a href="#chap-{ch["chapter_num"]}" color="blue">{heading}</a>'
            label = f"{linked_heading} {'.' * 6} {page_num}" if page_num is not None else linked_heading
            flow.append(Paragraph(label, st["toc_entry"]))
        if cited_notes:
            flow.append(Paragraph('<a href="#bibliography" color="blue">Bibliography</a>', st["toc_entry"]))
        flow.append(NextPageTemplate("body"))
        flow.append(PageBreak())

        for ch in finished:
            heading = f"Chapter {ch['chapter_num']}: {ch.get('title', '')}"
            toc_entries.append((heading, 0))  # placeholder; afterFlowable fills in the real page below
            flow.append(Paragraph(f'<a name="chap-{ch["chapter_num"]}"/>{heading}', st["chapter_heading"]))
            flow += _paragraphs(_text(ch), st["body"], citation_numbers)
            flow.append(PageBreak())

        if cited_notes:
            flow.append(Paragraph('<a name="bibliography"/>Bibliography', st["front_heading"]))
            for n, note in cited_notes:
                topic = note.get("topic") or note.get("name") or note["id"]
                flow.append(Paragraph(f'<a name="cite-{note["id"]}"/>[{n}] <b>{topic}</b>', st["front_body"]))
                sources = note.get("sources") or []
                if sources:
                    for src in sources:
                        src_title = src.get("title") or src.get("url") or "Source"
                        url = src.get("url") or ""
                        if url:
                            flow.append(Paragraph(f'{src_title} - <a href="{url}" color="blue">{url}</a>', st["front_body"]))
                        else:
                            flow.append(Paragraph(src_title, st["front_body"]))
                elif note.get("content"):
                    flow.append(Paragraph(note["content"], st["front_body"]))
            flow.append(PageBreak())

        about_author = bible_data.get("about_author") or ""
        if about_author.strip():
            flow.append(Paragraph("About the Author", st["front_heading"]))
            flow += _paragraphs(about_author, st["front_body"])

        if flow and isinstance(flow[-1], PageBreak):
            flow.pop()
        return flow

    # Pass 1: build once to learn which page each chapter heading lands on
    # (afterFlowable records it), throwing the rendered bytes away.
    class _RecordingDoc(BaseDocTemplate):
        def afterFlowable(self, flowable):
            if isinstance(flowable, Paragraph) and flowable.style.name == "ChapterHeading":
                text = flowable.getPlainText()
                for i, (h, _) in enumerate(toc_entries):
                    if h == text:
                        toc_entries[i] = (h, self.page)
                        break

    recording_buf = BytesIO()
    rec_doc = _RecordingDoc(
        recording_buf, pagesize=letter,
        leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=margin,
    )
    rec_doc.addPageTemplates([
        PageTemplate(id="unnumbered", frames=[_frame("unnumbered")], onPage=lambda c, d: None),
        PageTemplate(id="front", frames=[_frame("front")], onPage=lambda c, d: None),
        PageTemplate(id="body", frames=[_frame("body")], onPage=lambda c, d: None),
    ])
    rec_doc.build(story_for_pass())

    # afterFlowable recorded absolute reportlab page numbers; the printed
    # footer restarts at Arabic 1 on the first chapter page (_PageNumberer.
    # on_body), so shift the recorded numbers to match what readers will
    # actually see at the bottom of each page.
    if toc_entries:
        offset = toc_entries[0][1] - 1
        toc_entries[:] = [(h, p - offset) for h, p in toc_entries]

    # Pass 2: real build, now with accurate contents page numbers and footers.
    doc.build(story_for_pass())

    return buf.getvalue()


def export_pdf(bible: StoryBible) -> Path:
    content = build_pdf(bible.data)
    output_path = bible.project_dir / f"{bible.project_dir.name}.pdf"
    output_path.write_bytes(content)
    return output_path
