"""Lifecycle control for the local llama.cpp server (start/stop/status)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ghostwriter.config import _find_server_exe_in, available_models, set_folders, set_model
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


@router.get("/folders")
def llm_folders() -> dict[str, Any]:
    return {
        "llama_cpp_dir": cfg["llama"].get("llama_cpp_dir"),
        "models_dir": cfg["llama"].get("models_dir"),
        "server_exe": cfg["llama"].get("server_exe"),
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
    """Switches the active model_path (and, when the new model isn't the same
    family as the old one, clears draft_model_path - the current MTP
    speculative-decoding draft model was built specifically for the Gemma
    main model and would misbehave or crash the server if paired with a
    different architecture like Qwen or Phi). Requires the server to be
    stopped first so the running process's actual loaded model never
    disagrees with what cfg says. Updates the shared cfg dict in place (so
    the next /api/llm/start picks it up immediately) and persists to
    config.yaml so the choice survives a webui restart."""
    global _llama_process
    if llm.is_up() or (_llama_process is not None and _llama_process.poll() is None):
        raise HTTPException(409, "Stop the LLM server before switching models.")

    path = Path(body.path)
    if not path.is_file() or path.suffix.lower() != ".gguf":
        raise HTTPException(400, "Not a valid .gguf model file.")
    resolved = str(path.resolve())
    known = {m["path"] for m in available_models(cfg)}
    if resolved not in known:
        raise HTTPException(400, "Not one of the available models.")

    current_draft = cfg["llama"].get("draft_model_path")
    same_family = bool(current_draft) and (
        ("gemma" in Path(resolved).name.lower()) == ("gemma" in Path(current_draft).name.lower())
    )
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
