"""Live (real-LLM) smoke test: exercises one agent call through the actual
model server, to confirm end-to-end wiring beyond the no-LLM smoke_test.py.
Run from repo root with the server already up:
    python scripts/live_test.py
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ghostwriter.agents.researcher import ResearcherAgent
from ghostwriter.agents.author import AuthorAgent
from ghostwriter.agents.personas import select_personas_for_genre
from ghostwriter.config import load_config
from ghostwriter.llm_client import LLMClient
from ghostwriter.memory.story_bible import StoryBible

TEST_PROJECTS_DIR = Path(__file__).resolve().parent.parent / "projects" / "_live_test_tmp"
shutil.rmtree(TEST_PROJECTS_DIR, ignore_errors=True)

cfg = load_config()
llm = LLMClient(cfg)
print("Waiting for server...")
llm.wait_until_up(timeout_s=30.0)
print("Server is up.")

bible = StoryBible.create(
    TEST_PROJECTS_DIR,
    title="The Signal Between Stars",
    genre="sci-fi thriller",
    premise="A comms officer intercepts a signal that shouldn't exist, and tracing it unravels a conspiracy back on Earth.",
)

t0 = time.time()
researcher = ResearcherAgent(llm)
researcher.research(bible, ["deep space communication lag", "orbital relay stations"])
print(f"Researcher OK in {time.time() - t0:.1f}s, notes: {len(bible.data['research_notes'])}")
assert bible.data["research_notes"], "researcher produced no notes"
for note in bible.data["research_notes"]:
    print(f"  - {note['topic']}: {note['content'][:80]}...")

bible.add_character("Mara Voss", "protagonist", "Comms officer, methodical, hides a stutter under stress.")
bible.add_world_entry("Kessler Station", "location", "A relay station orbiting Europa, chronically understaffed.")
bible.set_outline([
    {"chapter_num": 1, "title": "Static", "pov": "Mara Voss", "summary": "Mara intercepts an impossible signal."},
])

personas = select_personas_for_genre("sci-fi thriller")
print("Personas:", [p.display_name for p in personas])
author = AuthorAgent(llm, personas)
t0 = time.time()
draft = author.draft_chapter(bible, 1, target_words=300)
print(f"Author OK in {time.time() - t0:.1f}s, draft length: {len(draft.split())} words")
assert len(draft.split()) > 50, f"draft too short: {draft!r}"
print("--- DRAFT EXCERPT ---")
print(draft[:500])

shutil.rmtree(TEST_PROJECTS_DIR, ignore_errors=True)
print("\nALL LIVE TESTS PASSED")
