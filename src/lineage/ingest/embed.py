"""Embedding backends. The index records `model_id` and refuses vectors from any other model."""

from __future__ import annotations

import hashlib
import json
import math
import urllib.request
from typing import Protocol

from lineage.text import tokens


class Embedder(Protocol):
    model_id: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Feature-hashed bag of identifier tokens. Lexical, not semantic, but deterministic,
    offline and dependency-free. ponytail: swap to `st:` or `ollama:` for real semantic recall."""

    def __init__(self, dim: int = 512):
        self.dim = dim
        self.model_id = f"hash-v1-{dim}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in tokens(text):
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "little")
            vec[h % self.dim] += 1.0 if (h >> 63) else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class SentenceTransformerEmbedder:
    def __init__(self, model: str):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError("embedder 'st:' needs `pip install lineage[st]`") from e
        self._model = SentenceTransformer(model)
        self.dim = self._model.get_sentence_embedding_dimension()
        self.model_id = f"st:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, batch_size=32, normalize_embeddings=True).tolist()


class OllamaEmbedder:
    def __init__(self, model: str, url: str):
        self._model, self._url = model, url.rstrip("/") + "/api/embed"
        self.model_id = f"ollama:{model}"
        self.dim = len(self.embed(["probe"])[0])

    def embed(self, texts: list[str]) -> list[list[float]]:
        req = urllib.request.Request(
            self._url, data=json.dumps({"model": self._model, "input": texts}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.load(resp)["embeddings"]


def make_embedder(spec: str, ollama_url: str = "http://localhost:11434") -> Embedder:
    kind, _, model = spec.partition(":")
    if kind == "hash":
        return HashEmbedder(int(model) if model else 512)
    if kind == "st" and model:
        return SentenceTransformerEmbedder(model)
    if kind == "ollama" and model:
        return OllamaEmbedder(model, ollama_url)
    raise ValueError(f"Unknown embedder {spec!r}; use 'hash', 'st:<model>' or 'ollama:<model>'")
