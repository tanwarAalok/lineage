"""Identifier-aware tokenization shared by keyword indexing, queries and the hash embedder."""

from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*|[0-9]+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def tokens(text: str) -> list[str]:
    """`getUserByID_v2` -> ['getuserbyid', 'get', 'user', 'by', 'id', 'v', '2'] (lowercased, deduped in order)."""
    out: dict[str, None] = {}
    for word in _WORD.findall(text):
        out[word.lower()] = None
        parts = _CAMEL.findall(word)
        if len(parts) > 1:
            for p in parts:
                out[p.lower()] = None
    return list(out)
