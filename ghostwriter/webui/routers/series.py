"""Series-level endpoints: browsing the shared canon (characters/world/books)
a series accumulates across its books, and reverting/editing/reapplying the
persistent status-change events (deaths, etc.) that follow characters from
one book into the next."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ghostwriter.memory.series_bible import SeriesBible
from ghostwriter.webui.state import cfg

router = APIRouter(tags=["series"])


def _load_series(slug: str) -> SeriesBible:
    try:
        return SeriesBible.load_by_slug(cfg["paths"]["series_dir"], slug)
    except FileNotFoundError:
        raise HTTPException(404, f"No series {slug!r}")


@router.get("/api/series")
def list_series() -> list[str]:
    series_dir = Path(cfg["paths"]["series_dir"])
    if not series_dir.exists():
        return []
    return sorted(
        p.name for p in series_dir.iterdir()
        if p.is_dir() and (p / "series_bible.json").exists()
    )


@router.get("/api/series/{slug}")
def get_series(slug: str) -> dict[str, Any]:
    return _load_series(slug).data


def _series_real_dir(series_dir: Path, slug: str) -> Path:
    """The series' current real (non-junction) directory: series_dir/slug
    itself, or wherever a prior _relocate_series call moved it to if that
    path is now a directory junction pointing elsewhere. Mirrors
    projects.py's _project_real_dir."""
    link_path = series_dir / slug
    if link_path.parent != series_dir:
        raise HTTPException(404, f"No series {slug!r}")
    if os.path.isjunction(link_path):
        return link_path.resolve()
    return link_path


def _relocate_series(series_dir: Path, slug: str, destination: str) -> Path:
    """Physically moves a series' folder to `destination` (an absolute
    parent folder), leaving a directory junction at the usual
    series_dir/slug path so every other route keeps working unchanged.
    Mirrors projects.py's _relocate_project."""
    link_path = series_dir / slug
    real_dir = _series_real_dir(series_dir, slug)
    if not (real_dir / "series_bible.json").exists():
        raise HTTPException(404, f"No series {slug!r}")

    dest = Path(destination.strip())
    if not dest.is_absolute():
        raise HTTPException(400, "Location must be an absolute folder path")
    if dest == real_dir.parent:
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
        shutil.move(str(new_dir), str(real_dir))  # keep series reachable at its usual path
        raise HTTPException(500, f"Could not create a link back to {link_path}: {exc.stderr}")

    return new_dir


class MoveSeriesRequest(BaseModel):
    destination: str


@router.post("/api/series/{slug}/move")
def move_series(slug: str, req: MoveSeriesRequest) -> dict[str, Any]:
    """Relocates an existing series' folder to a different location on disk.
    See _relocate_series for how the series stays reachable at its usual
    series_dir/slug path afterward."""
    series_dir = Path(cfg["paths"]["series_dir"])
    new_dir = _relocate_series(series_dir, slug, req.destination)
    return {"slug": slug, "location": str(new_dir)}


class UpdatePersistentEventRequest(BaseModel):
    event_name: str | None = None
    description: str | None = None
    status: str | None = None
    character: str | None = None


@router.post("/api/series/{slug}/events/{event_id}/revert")
def revert_event(slug: str, event_id: int) -> dict[str, Any]:
    series = _load_series(slug)
    try:
        return series.set_event_active(event_id, False)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/api/series/{slug}/events/{event_id}/reapply")
def reapply_event(slug: str, event_id: int) -> dict[str, Any]:
    series = _load_series(slug)
    try:
        return series.set_event_active(event_id, True)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/api/series/{slug}/events/{event_id}")
def update_event(slug: str, event_id: int, req: UpdatePersistentEventRequest) -> dict[str, Any]:
    series = _load_series(slug)
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        return series.update_persistent_event(event_id, **fields)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
