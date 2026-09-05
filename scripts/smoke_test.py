"""Exercises story bible, retriever, personas, and docx export without the
LLM server, so config/plumbing bugs surface fast. Run from repo root:
    python scripts/smoke_test.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ghostwriter.agents.personas import select_personas_for_genre, build_collaborative_system_prompt
from ghostwriter.cli import build_parser
from ghostwriter.config import load_config
from ghostwriter.export.docx_export import export_docx
from ghostwriter.memory.retriever import continuity_context
from ghostwriter.memory.series_bible import SeriesBible
from ghostwriter.memory.story_bible import StoryBible

TEST_PROJECTS_DIR = Path(__file__).resolve().parent.parent / "projects" / "_smoke_test_tmp"
TEST_SERIES_DIR = Path(__file__).resolve().parent.parent / "series" / "_smoke_test_tmp"
shutil.rmtree(TEST_PROJECTS_DIR, ignore_errors=True)
shutil.rmtree(TEST_SERIES_DIR, ignore_errors=True)

bible = StoryBible.create(
    TEST_PROJECTS_DIR,
    title="The Signal Between Stars",
    genre="sci-fi thriller",
    premise="A comms officer intercepts a signal that shouldn't exist.",
)
bible.add_research_note("Deep space comms", "Signal lag makes real-time contact impossible past the Moon.")
bible.add_world_entry("Kessler Station", "location", "A relay station orbiting Europa, chronically understaffed.")
bible.add_character("Mara Voss", "protagonist", "Comms officer, methodical, hides a stutter under stress.")
bible.set_outline([
    {"chapter_num": 1, "title": "Static", "pov": "Mara Voss", "summary": "Mara intercepts an impossible signal."},
    {"chapter_num": 2, "title": "Relay", "pov": "Mara Voss", "summary": "Mara traces the signal's origin."},
])
bible.upsert_chapter(
    1, title="Static",
    final="Mara Voss pressed the headset tighter against her ear. The signal shouldn't exist.\n\n"
          "She traced its origin to a point past Europa, where nothing was supposed to be.",
    summary="Mara intercepts an impossible signal from beyond Europa.",
    word_count=32,
)

ctx = continuity_context(bible, "Mara Europa signal", top_k=3)
assert "Mara" in ctx or "Europa" in ctx, f"retriever returned nothing useful: {ctx!r}"
print("Retriever OK:\n" + ctx)

personas = select_personas_for_genre("sci-fi thriller")
names = [p.display_name for p in personas]
print("Personas for 'sci-fi thriller':", names)
assert len(personas) == 2, f"expected scifi+thriller personas, got {names}"
prompt = build_collaborative_system_prompt(personas)
assert all(p.display_name in prompt for p in personas)
print("Collaborative system prompt OK.")

out_path = export_docx(bible, author_name=" & ".join(names))
assert out_path.exists() and out_path.stat().st_size > 0
print(f"DOCX export OK: {out_path} ({out_path.stat().st_size} bytes)")

series = SeriesBible.create(
    TEST_SERIES_DIR,
    series_title="Kessler Cycle",
    genre="sci-fi thriller",
    premise="A relay station crew keeps intercepting signals that shouldn't exist.",
    persona_keys=["scifi", "thriller"],
)
series.absorb_book(
    1,
    title="The Signal Between Stars",
    synopsis="Mara Voss traces an impossible signal to a conspiracy back on Earth.",
    characters=bible.data["characters"],
    world=bible.data["world"],
    research_notes=bible.data["research_notes"],
)
assert series.find_character("Mara Voss") is not None, "series bible did not absorb book 1's characters"
assert series.find_world_entry("Kessler Station") is not None, "series bible did not absorb book 1's world"
assert "Mara Voss" in series.series_recap(), "series recap missing book 1 synopsis"
print("Series absorb OK:\n" + series.series_recap())

book2 = StoryBible.create_in_series(
    TEST_PROJECTS_DIR, "The Static After", "sci-fi thriller",
    "Six months later, the signal returns - and it knows Mara's name.",
    series, book_num=2,
)
assert book2.find_character("Mara Voss") is not None, "book 2 was not seeded with series characters"
assert book2.find_world_entry("Kessler Station") is not None, "book 2 was not seeded with series world"
assert "Signal Between Stars" in book2.data["series_recap"], "book 2 recap missing prior book"
print("Series-seeded book 2 OK: inherited characters/world/recap.")

expected_slug = "kessler-cycle__the-static-after"
assert book2.project_dir.name == expected_slug, (
    f"expected series-namespaced project dir {expected_slug!r}, got {book2.project_dir.name!r}"
)
print(f"Series project-dir namespacing OK: {book2.project_dir.name}")

bible.upsert_chapter(2, title="Relay", summary="Mara confronts the conspiracy's handler and shuts the relay down.")
synopsis = bible.full_synopsis_brief()
assert "Ch1" in synopsis and "Ch2" in synopsis, f"full_synopsis_brief missing chapter recaps: {synopsis!r}"
print("full_synopsis_brief OK (spoiler-full, chapter-by-chapter).")

cfg = load_config()
assert "series_dir" in cfg["paths"], "config.yaml paths.series_dir not resolved by load_config"
print(f"Config series_dir OK: {cfg['paths']['series_dir']}")

parser = build_parser()
serve_args = parser.parse_args(["serve", "--port", "8124"])
assert serve_args.port == 8124, "CLI serve --port not wired"
print("CLI serve parsing OK.")

shutil.rmtree(TEST_SERIES_DIR, ignore_errors=True)
shutil.rmtree(TEST_PROJECTS_DIR, ignore_errors=True)

print("\nALL SMOKE TESTS PASSED")
