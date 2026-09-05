"""Word-level diff for highlighting what an instruction/revision changed.

Stdlib-only (difflib), consistent with the rest of the project's
dependency-free approach (see memory/retriever.py's BM25 index).
"""
from __future__ import annotations

import difflib
import re

_TOKEN_RE = re.compile(r"\s+|[^\s]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def word_diff(old_text: str, new_text: str) -> list[dict[str, str]]:
    """Returns a list of {"op": "equal"|"delete"|"insert", "text": ...} segments
    comparing old_text -> new_text, tokenized so whitespace/wrapping is preserved."""
    old_tokens = _tokenize(old_text)
    new_tokens = _tokenize(new_text)
    matcher = difflib.SequenceMatcher(a=old_tokens, b=new_tokens, autojunk=False)

    segments: list[dict[str, str]] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            segments.append({"op": "equal", "text": "".join(old_tokens[i1:i2])})
        elif op == "delete":
            segments.append({"op": "delete", "text": "".join(old_tokens[i1:i2])})
        elif op == "insert":
            segments.append({"op": "insert", "text": "".join(new_tokens[j1:j2])})
        elif op == "replace":
            segments.append({"op": "delete", "text": "".join(old_tokens[i1:i2])})
            segments.append({"op": "insert", "text": "".join(new_tokens[j1:j2])})
    return segments
