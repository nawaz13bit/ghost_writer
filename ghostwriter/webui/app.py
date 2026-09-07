"""Local web UI: browse a project's story bible and drive chapter/entity
revisions by typed instruction instead of the fully-automatic CLI pipeline.

Run with: python -m ghostwriter.cli serve

This module is just the composition root - it wires together the routers
under ghostwriter/webui/routers/ and mounts the static frontend. Shared
agent/config state lives in ghostwriter.webui.state; shared helpers live in
ghostwriter.webui.deps.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ghostwriter.llm_client import LLMTruncated
from ghostwriter.webui.routers import chapters, entities, ideas, llm_control, outline, projects, prompts, scenes, series
from ghostwriter.webui.state import STATIC_DIR

app = FastAPI(title="ghost_writer")


@app.exception_handler(LLMTruncated)
def _llm_truncated_handler(request: Request, exc: LLMTruncated) -> JSONResponse:
    # Catches this for every synchronous AI endpoint that doesn't already
    # handle it explicitly (background-job endpoints like chapter/scene
    # drafting catch it themselves via their generic job["error"] path, so
    # they never reach here) - a cut-off generation should surface as a
    # clean 502 with an actionable message, not an unhandled 500 traceback.
    return JSONResponse(status_code=502, content={"detail": str(exc)})

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
