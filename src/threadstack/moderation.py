"""Small automatic safety screen used before moderation workflows exist."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .models import EvidenceRef

QUARANTINE_RULES = (
    (re.compile(r"-----BEGIN [^-\n]*PRIVATE KEY-----", re.IGNORECASE), "private key material"),
    (re.compile(r"<\s*script\b|javascript\s*:", re.IGNORECASE), "executable HTML"),
    (re.compile(r"(?:curl|wget)\b[^\n|]*\|\s*(?:sh|bash)\b", re.IGNORECASE), "shell pipeline"),
    (re.compile(r"\bdata\s*:\s*text/(?:html|javascript)", re.IGNORECASE), "executable data URL"),
)


def quarantine_reason(
    *,
    text: Iterable[str] = (),
    evidence: Iterable[EvidenceRef] = (),
) -> str | None:
    values = [*text, *(item.uri for item in evidence), *(item.note or "" for item in evidence)]
    combined = "\n".join(values)
    for pattern, reason in QUARANTINE_RULES:
        if pattern.search(combined):
            return reason
    return None
