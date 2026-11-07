"""Identifier-aware tokenization shared by keyword indexing, queries and the hash embedder."""

from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*|[0-9]+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


# English filler that matches everywhere in code and comments. Applied to natural-language queries
# only (never to the index), so identifiers are untouched.
STOPWORDS = frozenset("""
a an the and or but if then else when while of to in on at by for with without from into onto over after
before about as is are was were be been being have has had do does did can could should would will shall
may might must not no nor so than that this these those it its they them their there here we our ours you
your he she his her i me my what which who whom whose why how all any each every some such only own same
too very just also more most other again once up down out off get gets got make makes made want wants
need needs instead later still even ever really able allowed thing things way ways""".split())


def query_terms(text: str) -> list[str]:
    """Keyword terms for a natural-language query, filler dropped. (Stemming was measured and made
    ranking worse: stems like `notif*` match too broadly, so whole words are matched as prefixes.)"""
    return [t for t in tokens(text) if t not in STOPWORDS and not t.isdigit()]


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
