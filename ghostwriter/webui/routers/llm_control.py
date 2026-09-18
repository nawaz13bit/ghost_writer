"""Lifecycle control for the local llama.cpp server (start/stop/status)."""
from __future__ import annotations

import os
import string
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ghostwriter.config import _find_server_exe_in, _model_family, available_models, set_folders, set_model
from ghostwriter.llm_client import cancel_all_calls
from ghostwriter.llm_server_launcher import build_command as build_llama_command
from ghostwriter.webui.state import cfg, llm

router = APIRouter(prefix="/api/llm", tags=["llm"])

_llama_process: subprocess.Popen | None = None
_last_crash_message: str | None = None


def _find_pid_on_port(port: int) -> int | None:
    """Looks up the PID of whatever's listening on the configured llama-server
    port - used to stop a server this UI didn't launch itself (e.g. left
    running from a previous restart, or started via the standalone script),
    since the writer should be able to toggle it regardless of who started it."""
    try:
        out = subprocess.check_output(
            [
                "powershell", "-NoProfile", "-Command",
                f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
                "-ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess)",
            ],
            text=True,
            timeout=10,
        ).strip()
        return int(out) if out else None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def _kill_pid(pid: int) -> bool:
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


@router.get("/status")
def llm_status() -> dict[str, Any]:
    global _llama_process, _last_crash_message
    up = llm.is_up()
    running_here = _llama_process is not None and _llama_process.poll() is None
    # If a UI-launched process has died without the server ever coming up,
    # that's a crash (e.g. the configured port was already taken) - surface
    # it once instead of leaving the frontend stuck showing "starting...".
    if _llama_process is not None and not running_here and not up:
        code = _llama_process.returncode
        _last_crash_message = (
            f"llama-server exited immediately (code {code}) - check server_log.txt. "
            "Common cause: the configured port is already in use."
        )
        _llama_process = None
    if up:
        _last_crash_message = None
    return {"up": up, "started_by_ui": running_here, "crashed": _last_crash_message}


@router.post("/cancel")
def llm_cancel() -> dict[str, Any]:
    """Emergency stop: halts whatever LLM call is in flight right now without
    killing the server (unlike /stop) - closing its connection makes
    llama.cpp abandon generation, so the writer isn't stuck watching a bad
    draft run to completion or waiting through a full server reload to
    interrupt it."""
    n = cancel_all_calls()
    return {"cancelled": n}


@router.get("/models")
def llm_models() -> dict[str, Any]:
    return {"models": available_models(cfg)}


@router.get("/browse")
def llm_browse(path: str | None = None, mode: str = "dir") -> dict[str, Any]:
    """Lists a filesystem location for the Settings > Local LLM folder/file
    pickers. The UI is a plain browser tab (no Electron/native shell), so a
    real OS file dialog isn't reachable from JS and <input type=file> never
    exposes an absolute host path - this endpoint plus a browser-side list
    modal is the local equivalent, and it works regardless of where the
    writer happens to keep their llama.cpp build or model file. mode="dir"
    (llama.cpp folder picker) lists only subfolders; mode="file" (model
    picker) also lists .gguf files. No path lists Windows drive roots."""
    if not path:
        entries = []
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.exists(root):
                entries.append({"name": root, "path": root, "is_dir": True})
        return {"cwd": None, "parent": None, "entries": entries}

    p = Path(path)
    if not p.is_dir():
        raise HTTPException(400, f"Not a folder: {path}")

    entries: list[dict[str, Any]] = []
    try:
        with os.scandir(p) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    continue
                if is_dir:
                    entries.append({"name": entry.name, "path": entry.path, "is_dir": True})
                elif mode == "file" and entry.name.lower().endswith(".gguf"):
                    entries.append({"name": entry.name, "path": entry.path, "is_dir": False})
    except PermissionError:
        raise HTTPException(403, f"Can't access: {path}")

    entries.sort(key=lambda e: (e["is_dir"] is False, e["name"].lower()))
    resolved = str(p.resolve())
    parent_path = p.resolve().parent
    parent = str(parent_path) if str(parent_path) != resolved else None
    return {"cwd": resolved, "parent": parent, "entries": entries}


@router.get("/folders")
def llm_folders() -> dict[str, Any]:
    return {
        "llama_cpp_dir": cfg["llama"].get("llama_cpp_dir"),
        "models_dir": cfg["llama"].get("models_dir"),
        "server_exe": cfg["llama"].get("server_exe"),
        "model_path": cfg["llama"].get("model_path"),
        "draft_model_path": cfg["llama"].get("draft_model_path"),
    }


class FoldersRequest(BaseModel):
    llama_cpp_dir: str | None = None
    models_dir: str | None = None


@router.post("/folders")
def llm_set_folders(body: FoldersRequest) -> dict[str, Any]:
    """Sets the llama.cpp build folder and/or an extra models folder, so the
    writer doesn't have to hand-edit config.yaml. Requires the server to be
    stopped first, same as switching models, since server_exe can change out
    from under a running process. An empty string clears that field back to
    whatever config.yaml/MODELS_DIR auto-discovery would otherwise find."""
    global _llama_process
    if llm.is_up() or (_llama_process is not None and _llama_process.poll() is None):
        raise HTTPException(409, "Stop the LLM server before changing folders.")

    llama_cpp_dir = (body.llama_cpp_dir or "").strip() or None
    models_dir = (body.models_dir or "").strip() or None

    found_exe = None
    if llama_cpp_dir:
        path = Path(llama_cpp_dir)
        if not path.is_dir():
            raise HTTPException(400, f"Not a folder: {llama_cpp_dir}")
        found_exe = _find_server_exe_in(path)
        if not found_exe:
            raise HTTPException(400, "No llama-server executable found in that folder.")

    if models_dir and not Path(models_dir).is_dir():
        raise HTTPException(400, f"Not a folder: {models_dir}")

    if llama_cpp_dir:
        cfg["llama"]["llama_cpp_dir"] = llama_cpp_dir
        cfg["llama"]["server_exe"] = found_exe
    else:
        cfg["llama"].pop("llama_cpp_dir", None)

    if models_dir:
        cfg["llama"]["models_dir"] = models_dir
    else:
        cfg["llama"].pop("models_dir", None)

    set_folders(llama_cpp_dir, models_dir)
    return {
        "llama_cpp_dir": llama_cpp_dir,
        "models_dir": models_dir,
        "server_exe": cfg["llama"].get("server_exe"),
    }


class ModelSwitchRequest(BaseModel):
    path: str


@router.post("/model")
def llm_switch_model(body: ModelSwitchRequest) -> dict[str, Any]:
    """Switches the active model_path. Requires the server to be stopped first
    so the running process's actual loaded model never disagrees with what
    cfg says. Updates the shared cfg dict in place (so the next
    /api/llm/start picks it up immediately) and persists to config.local.yaml
    so the choice survives a webui restart. Accepts any .gguf file the
    writer browsed to directly (see /api/llm/browse) rather than restricting
    to available_models()'s auto-discovered list. The writer picks the main
    model and the draft/MTP model independently via two separate loaders
    (see /api/llm/draft-model) rather than this endpoint guessing a pairing -
    if the newly picked main model's guessed family (see _model_family())
    disagrees with the currently-set draft model's, the mismatched draft is
    cleared automatically since loading it would misbehave or crash the
    server."""
    global _llama_process
    if llm.is_up() or (_llama_process is not None and _llama_process.poll() is None):
        raise HTTPException(409, "Stop the LLM server before switching models.")

    path = Path(body.path)
    if not path.is_file() or path.suffix.lower() != ".gguf":
        raise HTTPException(400, "Not a valid .gguf model file.")
    resolved = str(path.resolve())

    current_draft = cfg["llama"].get("draft_model_path")
    new_family = _model_family(path.name)
    current_draft_family = _model_family(Path(current_draft).name) if current_draft else None
    same_family = bool(current_draft) and (new_family is None or new_family == current_draft_family)
    new_draft = current_draft if same_family else None

    cfg["llama"]["model_path"] = resolved
    if new_draft is None:
        cfg["llama"].pop("draft_model_path", None)
    else:
        cfg["llama"]["draft_model_path"] = new_draft

    set_model(resolved, draft_model_path=new_draft)
    return {
        "model_path": resolved,
        "draft_model_path": new_draft,
        "cleared_draft": bool(current_draft) and new_draft is None,
    }


class DraftModelRequest(BaseModel):
    path: str | None = None


@router.post("/draft-model")
def llm_set_draft_model(body: DraftModelRequest) -> dict[str, Any]:
    """Sets (or, with path=None/empty, clears) the speculative-decoding
    draft/MTP model independently of the main model - a second explicit
    loader rather than guessing a pairing from folder siblings or filenames,
    since that guess can be wrong when a folder holds leftovers from more
    than one model family. Warns (via a "family_mismatch" hint in the
    response, not an error - the writer explicitly chose this pairing) when
    the picked file's guessed family disagrees with the current main
    model's, since a mismatched draft model will misbehave or crash the
    server. Requires the server to be stopped first, same as /model."""
    global _llama_process
    if llm.is_up() or (_llama_process is not None and _llama_process.poll() is None):
        raise HTTPException(409, "Stop the LLM server before changing the draft model.")

    draft_path = (body.path or "").strip() or None
    family_mismatch = False

    if draft_path:
        path = Path(draft_path)
        if not path.is_file() or path.suffix.lower() != ".gguf":
            raise HTTPException(400, "Not a valid .gguf model file.")
        draft_path = str(path.resolve())

        model_path = cfg["llama"].get("model_path")
        draft_family = _model_family(path.name)
        main_family = _model_family(Path(model_path).name) if model_path else None
        family_mismatch = bool(draft_family and main_family and draft_family != main_family)

    cfg["llama"]["draft_model_path"] = draft_path
    if not draft_path:
        cfg["llama"].pop("draft_model_path", None)

    set_model(cfg["llama"].get("model_path", ""), draft_model_path=draft_path)
    return {"draft_model_path": draft_path, "family_mismatch": family_mismatch}


@router.post("/start")
def llm_start() -> dict[str, Any]:
    global _llama_process, _last_crash_message
    if llm.is_up():
        return {"up": True, "started_by_ui": False, "message": "Already running."}
    if _llama_process is not None and _llama_process.poll() is None:
        return {"up": False, "started_by_ui": True, "message": "Already starting."}

    _last_crash_message = None
    cmd = build_llama_command(cfg)
    _llama_process = subprocess.Popen(
        cmd,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP") else 0,
    )
    return {"up": False, "started_by_ui": True, "message": "Starting - this can take a minute to load the model."}


@router.post("/stop")
def llm_stop() -> dict[str, Any]:
    """Stops the llama-server, whether or not this UI process is the one that
    launched it - if it's a process from an earlier UI run (e.g. after a
    restart) or started via the standalone script, fall back to finding and
    killing whatever's bound to the configured port."""
    global _llama_process
    if _llama_process is not None and _llama_process.poll() is None:
        _llama_process.terminate()
        try:
            _llama_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _llama_process.kill()
            _llama_process.wait(timeout=10)
        _llama_process = None
        return {"up": llm.is_up(), "stopped": True, "message": "Server stopped."}

    _llama_process = None
    if not llm.is_up():
        return {"up": False, "stopped": False, "message": "Not running."}

    pid = _find_pid_on_port(cfg["llama"]["port"])
    if pid is None or not _kill_pid(pid):
        return {"up": llm.is_up(), "stopped": False, "message": "Couldn't find or stop the server process."}
    return {"up": llm.is_up(), "stopped": True, "message": "Server stopped."}
