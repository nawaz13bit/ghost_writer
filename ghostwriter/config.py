"""Loads config.yaml into a plain dict, resolved relative to the repo root."""
from __future__ import annotations

import re
from pathlib import Path
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
# Personal machine settings (llama server/model folders) live here instead of
# config.yaml, so each person who clones the repo keeps their own folders in
# a file that's gitignored and never fought over / overwritten by others'
# settings. Optional: if absent, config.yaml's own llama block (or the
# models/ auto-discovery below) is used as-is.
LOCAL_CONFIG_PATH = REPO_ROOT / "config.local.yaml"
MODELS_DIR = REPO_ROOT / "models"


def _is_draft_name(name: str) -> bool:
    lower = name.lower()
    return "draft" in lower or "mtp" in lower


def _find_in_models_dir() -> tuple[str | None, str | None, str | None]:
    """Looks in MODELS_DIR (recursively, so an unzipped llama.cpp release
    subfolder is fine) for a server exe and .gguf model file(s), so the
    writer can just drop a llama.cpp build + model there instead of editing
    absolute paths into config.yaml. Returns (server_exe, model_path,
    draft_model_path), any of which may be None if not found."""
    if not MODELS_DIR.is_dir():
        return None, None, None

    server_exe = next(MODELS_DIR.rglob("llama-server.exe"), None)
    if server_exe is None:
        server_exe = next(MODELS_DIR.rglob("llama-server"), None)

    ggufs = sorted(MODELS_DIR.rglob("*.gguf"))
    draft = next((g for g in ggufs if _is_draft_name(g.name)), None)
    main = next((g for g in ggufs if g != draft), None)

    return (
        str(server_exe) if server_exe else None,
        str(main) if main else None,
        str(draft) if draft else None,
    )


def _find_server_exe_in(directory: Path) -> str | None:
    """Recursively looks for a llama-server executable under an
    arbitrary user-chosen folder (the llama.cpp build dir), same search as
    _find_in_models_dir but not restricted to MODELS_DIR."""
    if not directory.is_dir():
        return None
    exe = next(directory.rglob("llama-server.exe"), None)
    if exe is None:
        exe = next(directory.rglob("llama-server"), None)
    return str(exe) if exe else None


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    projects_dir = cfg.get("paths", {}).get("projects_dir", "projects")
    projects_path = Path(projects_dir)
    if not projects_path.is_absolute():
        projects_path = REPO_ROOT / projects_path
    cfg["paths"]["projects_dir"] = str(projects_path)

    series_dir = cfg.get("paths", {}).get("series_dir", "series")
    series_path = Path(series_dir)
    if not series_path.is_absolute():
        series_path = REPO_ROOT / series_path
    cfg["paths"]["series_dir"] = str(series_path)

    llama = cfg.setdefault("llama", {})

    # config.local.yaml (gitignored, personal) overrides config.yaml's llama
    # block field-by-field, so this machine's folders/model choice never need
    # to touch the shared config.yaml.
    if LOCAL_CONFIG_PATH.is_file():
        with open(LOCAL_CONFIG_PATH, "r", encoding="utf-8") as f:
            local_cfg = yaml.safe_load(f) or {}
        llama.update(local_cfg.get("llama", {}))

    # models/ is checked first so a dropped-in llama.cpp build + model just
    # works with no config.yaml edits; anything not found there falls back
    # to whatever config.yaml already specifies.
    found_exe, found_model, found_draft = _find_in_models_dir()
    if found_exe:
        llama["server_exe"] = found_exe
    if found_model:
        llama["model_path"] = found_model
    if found_draft:
        llama["draft_model_path"] = found_draft

    # User-configured folders (Settings > Local LLM > Folders) take priority
    # over both of the above: an explicit llama_cpp_dir always wins for
    # server_exe, and models_dir is folded into available_models()'s scan
    # (see below) rather than model_path directly.
    llama_cpp_dir = llama.get("llama_cpp_dir")
    if llama_cpp_dir:
        found = _find_server_exe_in(Path(llama_cpp_dir))
        if found:
            llama["server_exe"] = found

    return cfg


def available_models(cfg: dict) -> list[dict]:
    """Lists candidate .gguf files the writer could switch to: everything next
    to the currently configured model_path (that's where this user's models
    actually live, e.g. an external D:\\code\\models folder - the repo's own
    MODELS_DIR convention is a separate, optional drop-in path), plus
    MODELS_DIR itself if different. Draft/speculative-decoding files (name
    contains "draft" or "mtp") are excluded - those are picked automatically
    to pair with a main model, not selected directly."""
    dirs = []
    model_path = cfg.get("llama", {}).get("model_path")
    if model_path:
        parent = Path(model_path).resolve().parent
        dirs.append(parent)
    models_dir_setting = cfg.get("llama", {}).get("models_dir")
    if models_dir_setting:
        extra = Path(models_dir_setting)
        if extra.is_dir() and extra not in dirs:
            dirs.append(extra)
    if MODELS_DIR not in dirs and MODELS_DIR.is_dir():
        dirs.append(MODELS_DIR)

    seen: set[str] = set()
    models = []
    current = str(Path(model_path).resolve()) if model_path else None
    for d in dirs:
        if not d.is_dir():
            continue
        for g in sorted(d.rglob("*.gguf")):
            if _is_draft_name(g.name):
                continue
            resolved = str(g.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            size_gb = round(g.stat().st_size / (1024 ** 3), 1)
            models.append({
                "path": resolved,
                "name": g.name,
                "size_gb": size_gb,
                "current": resolved == current,
            })
    return models


def _ensure_local_config() -> None:
    """Creates config.local.yaml (gitignored, this machine's own llama
    folders/model choice) from its template on first write, so set_model/
    set_folders always have a file to targeted-replace into rather than
    touching the shared config.yaml."""
    if LOCAL_CONFIG_PATH.is_file():
        return
    template = REPO_ROOT / "config.local.yaml.example"
    if template.is_file():
        LOCAL_CONFIG_PATH.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        LOCAL_CONFIG_PATH.write_text(
            'llama:\n  server_exe: ""\n  model_path: ""\n  draft_model_path: ""\n',
            encoding="utf-8",
        )


def set_model(model_path: str, draft_model_path: str | None = "") -> None:
    """Persists a new model_path (and, unless draft_model_path is left as the
    default "", a new/cleared draft_model_path) into config.local.yaml (this
    machine's personal, gitignored settings - see LOCAL_CONFIG_PATH) with a
    targeted line-level regex replace rather than a full YAML re-dump, so the
    file's hand-written comments survive. draft_model_path="" (the
    default) leaves that line untouched; pass None to clear it (commented
    out) or a path string to set it - callers should clear it when switching
    away from the model family the current draft/MTP file was built for,
    since a mismatched draft model will misbehave or crash the server."""
    _ensure_local_config()
    text = LOCAL_CONFIG_PATH.read_text(encoding="utf-8")

    escaped = model_path.replace("\\", "\\\\")
    text, n = re.subn(
        r'^(\s*model_path:\s*).*$',
        lambda m: f'{m.group(1)}"{escaped}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n == 0:
        raise ValueError("config.local.yaml has no model_path line to update")

    if draft_model_path != "":
        if draft_model_path:
            escaped_draft = draft_model_path.replace("\\", "\\\\")
            text, dn = re.subn(
                r'^(\s*)#?\s*(draft_model_path:\s*).*$',
                lambda m: f'{m.group(1)}{m.group(2)}"{escaped_draft}"',
                text,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            text, dn = re.subn(
                r'^(\s*)draft_model_path:(\s*.*)$',
                lambda m: f'{m.group(1)}# draft_model_path:{m.group(2)}',
                text,
                count=1,
                flags=re.MULTILINE,
            )

    LOCAL_CONFIG_PATH.write_text(text, encoding="utf-8")


def _set_or_clear_key(text: str, key: str, value: str | None) -> str:
    """Sets `key: "value"` on its own line under the llama: block, clears it
    (comments the line out) if value is falsy, or appends a new commented-out
    line right after `server_exe:` if the key doesn't exist yet - keeps
    set_folders' behavior consistent with set_model's targeted-replace
    approach so config.yaml's comments survive."""
    pattern = rf'^(\s*)#?\s*{key}:(\s*.*)$'
    if value:
        escaped = value.replace("\\", "\\\\")
        new_text, n = re.subn(
            pattern,
            lambda m: f'{m.group(1)}{key}: "{escaped}"',
            text,
            count=1,
            flags=re.MULTILINE,
        )
        if n > 0:
            return new_text
        # Key doesn't exist yet - insert right after the server_exe line.
        return re.sub(
            r'^(\s*)(server_exe:\s*.*)$',
            lambda m: f'{m.group(0)}\n{m.group(1)}{key}: "{escaped}"',
            text,
            count=1,
            flags=re.MULTILINE,
        )
    new_text, n = re.subn(
        pattern,
        lambda m: f'{m.group(1)}# {key}:{m.group(2)}',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    return new_text if n > 0 else text


def set_folders(llama_cpp_dir: str | None, models_dir: str | None) -> None:
    """Persists the Settings > Local LLM > Folders fields into config.local.
    yaml (this machine's personal, gitignored settings - see
    LOCAL_CONFIG_PATH): llama_cpp_dir (folder to search for llama-server.exe)
    and models_dir (an extra folder for available_models() to scan for .gguf
    files), each independently settable/clearable."""
    _ensure_local_config()
    text = LOCAL_CONFIG_PATH.read_text(encoding="utf-8")
    text = _set_or_clear_key(text, "llama_cpp_dir", llama_cpp_dir)
    text = _set_or_clear_key(text, "models_dir", models_dir)
    LOCAL_CONFIG_PATH.write_text(text, encoding="utf-8")
