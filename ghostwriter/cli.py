"""CLI entrypoint for ghost_writer.

Usage:
    python -m ghostwriter.cli serve
"""
from __future__ import annotations

import argparse
import sys


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("ghostwriter.webui.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ghostwriter")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="Start the local web UI.")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8000)
    serve_p.add_argument("--reload", action="store_true", help="Auto-reload on code changes (development only)")
    serve_p.set_defaults(func=cmd_serve)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
