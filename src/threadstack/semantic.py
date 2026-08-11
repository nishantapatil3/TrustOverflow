"""Small deterministic embeddings used for optional semantic search."""

from __future__ import annotations

import hashlib
import math
import re

EMBEDDING_DIMENSION = 128
_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")


def _features(text: str) -> list[str]:
    tokens = _TOKEN_PATTERN.findall(text.casefold())
    return tokens + [f"{left}:{right}" for left, right in zip(tokens, tokens[1:])]


def embed_text(text: str) -> list[float]:
    """Create a normalized hashing-vector embedding without external model calls."""
    vector = [0.0] * EMBEDDING_DIMENSION
    for feature in _features(text):
        digest = hashlib.sha256(feature.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSION
        vector[index] += 1.0
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        return vector
    return [value / magnitude for value in vector]


def thread_search_text(title: str, tags: list[str], bodies: list[str]) -> str:
    """Build the searchable document shared by both storage implementations."""
    return " ".join((title, title, *tags, *bodies))


def cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(first * second for first, second in zip(left, right))


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.8g}" for value in vector) + "]"
