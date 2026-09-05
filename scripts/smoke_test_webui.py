"""End-to-end smoke test for the web UI backend, against the real LLM server.
Creates a throwaway project, drafts/revises/diffs/approves chapter 1, then
confirms story_bible.json reflects it correctly. Cleans up after itself.

Run from repo root: python scripts/smoke_test_webui.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from ghostwriter.webui.app import app
from ghostwriter.webui.state import cfg

client = TestClient(app)

projects_dir = Path(cfg["paths"]["projects_dir"])
slug = "webui-smoke-test"
shutil.rmtree(projects_dir / slug, ignore_errors=True)

try:
    r = client.post("/api/projects", json={
        "title": "WebUI Smoke Test", "genre": "sci-fi thriller",
        "premise": "A lighthouse keeper picks up a signal from a ship that sank a century ago.",
        "length_category": "novella",
    })
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == slug
    print("Project create OK.")

    r = client.get(f"/api/projects/{slug}")
    assert r.status_code == 200
    data = r.json()
    assert data["characters"] and data["world"] and data["outline"], "bible not populated"
    print(f"Bible populated: {len(data['characters'])} characters, {len(data['world'])} world entries, "
          f"{len(data['outline'])} outline entries.")

    r = client.post(f"/api/projects/{slug}/chapters/1/draft")
    assert r.status_code == 200, r.text
    draft_entry = r.json()
    assert draft_entry["source"] == "draft" and draft_entry["text"]
    print(f"Draft OK ({len(draft_entry['text'].split())} words).")

    r = client.post(f"/api/projects/{slug}/chapters/1/revise", json={
        "instruction": "Make the opening paragraph noticeably more tense and ominous."
    })
    assert r.status_code == 200, r.text
    revised = r.json()
    assert revised["source"] == "instruction"
    assert revised["text"] != draft_entry["text"], "instruction revise did not change the text"
    assert any(seg["op"] in ("insert", "delete") for seg in revised["diff"]), "diff has no changes"
    print("Instruction revise OK, diff has changes.")

    r = client.get(f"/api/projects/{slug}/chapters/1/diff", params={"from_id": 0, "to_id": 1})
    assert r.status_code == 200, r.text
    assert r.json()["diff"], "diff endpoint returned nothing"
    print("Diff-by-id endpoint OK.")

    r = client.post(f"/api/projects/{slug}/chapters/1/approve", json={"history_id": revised["id"]})
    assert r.status_code == 200, r.text
    approved_ch = r.json()
    assert approved_ch["approved"] is True
    assert approved_ch["final"], "approve did not set final text"
    assert approved_ch.get("summary"), "approve did not summarize"
    print("Approve OK: final/approved/summary set (editor+continuity+copyedit chain ran).")

    # Re-approve to trigger the needs_recheck cascade onto later approved chapters.
    r = client.post(f"/api/projects/{slug}/chapters/2/draft")
    assert r.status_code == 200, r.text
    r = client.post(f"/api/projects/{slug}/chapters/2/approve", json={"history_id": 0})
    assert r.status_code == 200, r.text

    r = client.post(f"/api/projects/{slug}/chapters/1/revise", json={
        "instruction": "The lighthouse keeper loses his left hand in an accident during this chapter."
    })
    assert r.status_code == 200, r.text
    new_rev = r.json()
    r = client.post(f"/api/projects/{slug}/chapters/1/approve", json={"history_id": new_rev["id"]})
    assert r.status_code == 200, r.text

    r = client.get(f"/api/projects/{slug}")
    ch2 = next(c for c in r.json()["chapters"] if c["chapter_num"] == 2)
    assert ch2.get("needs_recheck") is True, "re-approving chapter 1 did not cascade needs_recheck to chapter 2"
    print("needs_recheck cascade OK.")

    r = client.post(f"/api/projects/{slug}/chapters/2/check-continuity")
    assert r.status_code == 200, r.text
    print(f"Check-continuity OK: {len(r.json()['issues'])} issue(s) surfaced.")

    r = client.get(f"/api/projects/{slug}")
    ch2 = next(c for c in r.json()["chapters"] if c["chapter_num"] == 2)
    assert ch2.get("needs_recheck") is False, "check-continuity did not clear needs_recheck"
    print("needs_recheck cleared after check OK.")

    char_name = data["characters"][0]["name"]
    r = client.post(f"/api/projects/{slug}/entities/characters/{char_name}/revise", json={
        "instruction": "Add one sentence noting a nervous habit this character has."
    })
    assert r.status_code == 200, r.text
    entity_rev = r.json()
    assert entity_rev["text"]
    r = client.post(f"/api/projects/{slug}/entities/characters/{char_name}/approve", json={
        "history_id": entity_rev["id"]
    })
    assert r.status_code == 200, r.text
    assert r.json()["approved"] is True
    print("Entity revise/approve OK.")

    r = client.post(f"/api/projects/{slug}/entities/characters", json={
        "name": "Test Newcomer", "role": "supporting", "description": "A minor character added via the UI.",
    })
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Test Newcomer"
    r = client.post(f"/api/projects/{slug}/entities/world", json={
        "name": "Test Outpost", "category": "location", "content": "A remote research outpost.",
    })
    assert r.status_code == 200, r.text
    print("Create character/world entry OK.")

    r = client.delete(f"/api/projects/{slug}/entities/characters/Test Newcomer")
    assert r.status_code == 200, r.text
    r = client.delete(f"/api/projects/{slug}/entities/world/Test Outpost")
    assert r.status_code == 200, r.text
    r = client.get(f"/api/projects/{slug}")
    data2 = r.json()
    assert not any(c["name"] == "Test Newcomer" for c in data2["characters"])
    assert not any(w["name"] == "Test Outpost" for w in data2["world"])
    print("Delete character/world entry OK.")

    print("\nALL WEB UI SMOKE TESTS PASSED")
finally:
    shutil.rmtree(projects_dir / slug, ignore_errors=True)
