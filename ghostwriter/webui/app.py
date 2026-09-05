"""Local web UI: browse a project's story bible and drive chapter/entity
revisions by typed instruction instead of the fully-automatic CLI pipeline.

Run with: python -m ghostwriter.cli serve

This module is just the composition root - it wires together the routers
under ghostwriter/webui/routers/ and mounts the static frontend. Shared
agent/config state lives in ghostwriter.webui.state; shared helpers live in
ghostwriter.webui.deps.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ghostwriter.webui.routers import chapters, entities, ideas, llm_control, outline, projects, prompts, scenes, series
from ghostwriter.webui.state import STATIC_DIR

app = FastAPI(title="ghost_writer")

app.include_router(llm_control.router)
app.include_router(projects.router)
app.include_router(chapters.router)
app.include_router(outline.router)
app.include_router(entities.router)
app.include_router(ideas.router)
app.include_router(prompts.router)
app.include_router(scenes.router)
app.include_router(series.router)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))
