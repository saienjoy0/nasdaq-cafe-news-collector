from __future__ import annotations


VALID_CONFIDENCE = {"high", "medium", "low", "unknown"}


def normalize_confidence(value: str | None) -> str:
    normalized = (value or "unknown").strip().lower()
    return normalized if normalized in VALID_CONFIDENCE else "unknown"


def confidence_from_source_count(count: int, has_reason: bool) -> str:
    if count >= 2 and has_reason:
        return "high"
    if count >= 1 and has_reason:
        return "medium"
    if count >= 1:
        return "low"
    return "unknown"

