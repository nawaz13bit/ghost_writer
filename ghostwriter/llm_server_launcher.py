"""Starts llama-server.exe with the ghost_writer config, in the foreground.

Usage:
    python -m ghostwriter.llm_server_launcher [--verbose]

Leave this running in its own terminal; agents talk to it over HTTP.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

from ghostwriter.config import load_config


def build_command(cfg: dict) -> list[str]:
    llama = cfg["llama"]
    cmd = [
        llama["server_exe"],
        "--model", llama["model_path"],
        "--host", llama["host"],
        "--port", str(llama["port"]),
        "--ctx-size", str(llama["ctx_size"]),
        "--gpu-layers", str(llama["gpu_layers"]),
        "--n-cpu-moe", str(llama["n_cpu_moe"]),
        "--flash-attn", str(llama.get("flash_attn", "auto")),
        "--reasoning", str(llama.get("reasoning", "off")),
    ]

    draft_model = llama.get("draft_model_path")
    if draft_model:
        cmd += [
            "--model-draft", draft_model,
            "--spec-type", llama.get("spec_type", "draft-mtp"),
            "--gpu-layers-draft", str(llama.get("gpu_layers_draft", "all")),
        ]

    cmd += llama.get("extra_args", []) or []
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    cfg = load_config()
    cmd = build_command(cfg)
    print("Launching:", " ".join(cmd), file=sys.stderr)
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
