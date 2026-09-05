"""Builds a standalone EPUB3 file from a project's chapters - no external
dependency (no ebooklib), just zipfile + hand-written OPF/nav/XHTML, matching
the project's existing preference for dependency-free tooling where the
format is simple enough to hand-roll (see memory/retriever.py's BM25 index
instead of an embeddings library).

KDP accepts EPUB directly as an ebook upload - unlike the plain-Markdown
export, this is something a writer can actually upload to Kindle.
"""
from __future__ import annotations

import html
import re
import uuid
import zipfile
from io import BytesIO
from typing import Any

_CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

_STYLE_CSS = """body { font-family: serif; line-height: 1.5; margin: 1em; }
h1, h2 { text-align: center; }
p { margin: 0 0 1em 0; text-indent: 1.5em; }
p:first-of-type { text-indent: 0; }
.title-page { text-align: center; margin-top: 30%; }
"""


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


_CITATION_RE = re.compile(r"\[\^([\w-]+)\]")


def _linkify_citations(escaped_html: str, numbers: dict[str, int]) -> str:
    """Replaces literal [^id] markers (already HTML-escaped, so still plain
    text at this point) with a superscript link to that source's entry on
    the bibliography page. Markers with no matching research note (a stale
    id, or the note got deleted after the chapter was drafted) are left as
    plain text rather than linking to nothing."""

    def repl(m: re.Match) -> str:
        n = numbers.get(m.group(1))
        if n is None:
            return m.group(0)
        return f'<sup><a href="bibliography.xhtml#cite-{_escape(m.group(1))}">[{n}]</a></sup>'

    return _CITATION_RE.sub(repl, escaped_html)


def _text_to_xhtml_paragraphs(text: str, citation_numbers: dict[str, int] | None = None) -> str:
    paragraphs = re.split(r"\n\s*\n", text.strip())
    out = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        escaped = _escape(p)
        if citation_numbers:
            escaped = _linkify_citations(escaped, citation_numbers)
        out.append(f"<p>{escaped}</p>")
    return "\n".join(out)


def _chapter_xhtml(title: str, body_html: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><meta charset="utf-8"/><title>{_escape(title)}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
<h1>{_escape(title)}</h1>
{body_html}
</body>
</html>"""


def _matter_xhtml(title: str, text: str) -> str:
    return _chapter_xhtml(title, _text_to_xhtml_paragraphs(text))


def _toc_xhtml(entries: list[tuple[str, str, str, str]]) -> str:
    """An in-reading-order Contents page with real hyperlinks to each
    chapter, distinct from nav.xhtml (the EPUB3 landmark nav e-readers use
    for their own TOC menu) - older Kindle firmware doesn't reliably expose
    nav.xhtml, so KDP still expects a page like this one, pointed at from
    the OPF <guide> element."""
    items = "\n".join(f'<li><a href="{filename}">{_escape(nav_title)}</a></li>' for _, filename, nav_title, _ in entries)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><meta charset="utf-8"/><title>Contents</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
<h1>Contents</h1>
<ol>
{items}
</ol>
</body>
</html>"""


def _bibliography_xhtml(cited_notes: list[tuple[int, dict[str, Any]]]) -> str:
    """Numbered source list for nonfiction/researched books, one entry per
    research note actually referenced by an inline [^id] marker somewhere in
    the manuscript - notes gathered during research but never cited in the
    prose are left out, same as a real bibliography only lists what's cited."""
    items = []
    for n, note in cited_notes:
        topic = note.get("topic") or note.get("name") or note["id"]
        parts = [f'<p id="cite-{_escape(note["id"])}">[{n}] <strong>{_escape(topic)}</strong></p>']
        sources = note.get("sources") or []
        if sources:
            for src in sources:
                src_title = src.get("title") or src.get("url") or "Source"
                url = src.get("url") or ""
                if url:
                    parts.append(f'<p class="source">{_escape(src_title)} - <a href="{_escape(url)}">{_escape(url)}</a></p>')
                else:
                    parts.append(f'<p class="source">{_escape(src_title)}</p>')
        elif note.get("content"):
            parts.append(f'<p class="source">{_escape(note["content"])}</p>')
        items.append("\n".join(parts))
    return _chapter_xhtml("Bibliography", "\n".join(items))


def build_epub(bible_data: dict[str, Any], language: str | None = None, lang_code: str = "en") -> bytes:
    """Returns the raw bytes of an EPUB3 file for the given story bible's
    chapters (dict shape matching StoryBible.data). Chapters with no drafted
    text are skipped, same as the Markdown export. Optional front/back matter
    (copyright, foreword, acknowledgments, about-the-author) is pulled from
    the same bible fields the PDF export uses - the back-cover blurb is
    deliberately excluded, it's for retailer listings, not the book itself.

    When language is given, chapter bodies are pulled from that chapter's
    translations field instead of its English text (chapters with no
    translation for that language yet are skipped), and dc:language is set
    to lang_code instead of "en". Front/back matter stays in English -
    translation only covers chapter prose."""
    title = bible_data.get("title") or "Untitled"
    author_name = bible_data.get("author_name") or ""
    chapters = sorted(bible_data.get("chapters", []), key=lambda c: c["chapter_num"])

    entries = []  # list of (id, filename, nav_title)
    manifest_items = []
    spine_items = []

    front_matter = []  # list of (id, filename, xhtml) inserted before chapters
    copyright_text = bible_data.get("copyright_text") or ""
    if copyright_text.strip():
        front_matter.append(("copyright", "copyright.xhtml", _matter_xhtml("Copyright", copyright_text)))
    foreword = bible_data.get("foreword") or ""
    if foreword.strip():
        front_matter.append(("foreword", "foreword.xhtml", _matter_xhtml("Foreword", foreword)))
    acknowledgments = bible_data.get("acknowledgments") or ""
    if acknowledgments.strip():
        front_matter.append(("acknowledgments", "acknowledgments.xhtml", _matter_xhtml("Acknowledgments", acknowledgments)))

    chapter_texts: list[tuple[dict, str]] = []
    for ch in chapters:
        if language:
            text = ((ch.get("translations") or {}).get(language) or {}).get("text", "")
        else:
            history = ch.get("history") or []
            text = history[-1]["text"] if history else ch.get("draft", "")
        if not text.strip():
            continue
        chapter_texts.append((ch, text))

    # Number citations by first appearance across the manuscript, and keep
    # only the research notes actually referenced - see _bibliography_xhtml.
    notes_by_id = {n["id"]: n for n in bible_data.get("research_notes", [])}
    citation_numbers: dict[str, int] = {}
    for _, text in chapter_texts:
        for m in _CITATION_RE.finditer(text):
            if m.group(1) in notes_by_id:
                citation_numbers.setdefault(m.group(1), len(citation_numbers) + 1)
    cited_notes = sorted(
        ((n, notes_by_id[cid]) for cid, n in citation_numbers.items() if cid in notes_by_id),
        key=lambda pair: pair[0],
    )

    for ch, text in chapter_texts:
        heading = ch.get("title") or f"Chapter {ch['chapter_num']}"
        item_id = f"chapter{ch['chapter_num']}"
        filename = f"chapter-{ch['chapter_num']}.xhtml"
        body_html = _text_to_xhtml_paragraphs(text, citation_numbers)
        entries.append((item_id, filename, f"Chapter {ch['chapter_num']}: {heading}", _chapter_xhtml(heading, body_html)))

    back_matter = []
    about_author = bible_data.get("about_author") or ""
    if about_author.strip():
        back_matter.append(("about-author", "about-author.xhtml", _matter_xhtml("About the Author", about_author)))
    if cited_notes:
        back_matter.append(("bibliography", "bibliography.xhtml", _bibliography_xhtml(cited_notes)))

    book_id = f"urn:uuid:{uuid.uuid4()}"

    manifest_items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="style" href="style.css" media-type="text/css"/>',
        '<item id="titlepage" href="title.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="toc" href="toc.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine_items = ['<itemref idref="titlepage"/>']
    nav_items = []
    for item_id, filename, _ in front_matter:
        manifest_items.append(f'<item id="{item_id}" href="{filename}" media-type="application/xhtml+xml"/>')
        spine_items.append(f'<itemref idref="{item_id}"/>')
    spine_items.append('<itemref idref="toc"/>')
    for item_id, filename, nav_title, _ in entries:
        manifest_items.append(f'<item id="{item_id}" href="{filename}" media-type="application/xhtml+xml"/>')
        spine_items.append(f'<itemref idref="{item_id}"/>')
        nav_items.append(f'<li><a href="{filename}">{_escape(nav_title)}</a></li>')
    for item_id, filename, _ in back_matter:
        manifest_items.append(f'<item id="{item_id}" href="{filename}" media-type="application/xhtml+xml"/>')
        spine_items.append(f'<itemref idref="{item_id}"/>')

    creator_xml = f"\n    <dc:creator>{_escape(author_name)}</dc:creator>" if author_name.strip() else ""

    content_opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">{book_id}</dc:identifier>
    <dc:title>{_escape(title)}</dc:title>{creator_xml}
    <dc:language>{_escape(lang_code)}</dc:language>
    <meta property="dcterms:modified">2024-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    {chr(10).join(manifest_items)}
  </manifest>
  <spine>
    {chr(10).join(spine_items)}
  </spine>
  <guide>
    <reference type="toc" title="Table of Contents" href="toc.xhtml"/>
  </guide>
</package>"""

    nav_xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><meta charset="utf-8"/><title>Table of Contents</title></head>
<body>
<nav epub:type="toc" id="toc">
<h1>Table of Contents</h1>
<ol>
{chr(10).join(nav_items)}
</ol>
</nav>
</body>
</html>"""

    byline_html = f'<p>by {_escape(author_name)}</p>' if author_name.strip() else ""
    title_xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><meta charset="utf-8"/><title>{_escape(title)}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
<div class="title-page"><h1>{_escape(title)}</h1>{byline_html}</div>
</body>
</html>"""

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", content_opf)
        zf.writestr("OEBPS/nav.xhtml", nav_xhtml)
        zf.writestr("OEBPS/style.css", _STYLE_CSS)
        zf.writestr("OEBPS/title.xhtml", title_xhtml)
        zf.writestr("OEBPS/toc.xhtml", _toc_xhtml(entries))
        for _, filename, xhtml in front_matter:
            zf.writestr(f"OEBPS/{filename}", xhtml)
        for _, filename, _, xhtml in entries:
            zf.writestr(f"OEBPS/{filename}", xhtml)
        for _, filename, xhtml in back_matter:
            zf.writestr(f"OEBPS/{filename}", xhtml)

    return buf.getvalue()
