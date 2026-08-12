from __future__ import annotations

from dataclasses import replace
from math import floor
from typing import Any

from nasdaq_cafe.cache import write_json
from nasdaq_cafe.config import RunConfig
from nasdaq_cafe.raw_archive import collect_manifest_fulltext

DEFAULT_RUNTIME_BUDGET_SECONDS = 0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_WORKERS = 6
CONNECT_AND_SCHEDULING_RESERVE_SECONDS = 7


def _nonnegative_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except ValueError:
        return default
    return max(0, parsed)


def _positive_int(value: Any, default: int) -> int:
    parsed = _nonnegative_int(value, default)
    return parsed if parsed > 0 else default


def _runtime_plan(config: RunConfig, manifest: dict[str, Any]) -> dict[str, Any]:
    explicit_count_cap = _nonnegative_int(
        config.env.get("NASDAQ_CAFE_MAX_FULLTEXT_ATTEMPTS"), 0
    )
    budget_seconds = _nonnegative_int(
        config.env.get("NASDAQ_CAFE_FULLTEXT_RUNTIME_BUDGET_SECONDS"),
        DEFAULT_RUNTIME_BUDGET_SECONDS,
    )
    workers = min(
        16,
        _positive_int(config.env.get("NASDAQ_CAFE_FULLTEXT_WORKERS"), DEFAULT_WORKERS),
    )
    timeout_seconds = _positive_int(
        config.env.get("NASDAQ_CAFE_REQUEST_TIMEOUT_SECONDS"),
        DEFAULT_REQUEST_TIMEOUT_SECONDS,
    )
    documents = [
        row for row in manifest.get("documents", []) if isinstance(row, dict)
    ]
    pending_candidates = [
        row for row in documents if str(row.get("fulltext_status") or "pending") == "pending"
    ]

    if explicit_count_cap > 0 or budget_seconds <= 0:
        return {
            "runtimeBudgetSeconds": budget_seconds or None,
            "effectiveAttemptLimit": explicit_count_cap or None,
            "runtimeBudgetApplied": False,
            "candidateCountBeforePreflight": len(pending_candidates),
            "workers": workers,
            "requestTimeoutSeconds": timeout_seconds,
        }

    worst_case_task_seconds = timeout_seconds + CONNECT_AND_SCHEDULING_RESERVE_SECONDS
    effective_limit = max(
        workers,
        floor((budget_seconds * workers) / max(1, worst_case_task_seconds)),
    )
    runtime_applied = len(pending_candidates) > effective_limit
    return {
        "runtimeBudgetSeconds": budget_seconds,
        "effectiveAttemptLimit": effective_limit if runtime_applied else None,
        "runtimeBudgetApplied": runtime_applied,
        "candidateCountBeforePreflight": len(pending_candidates),
        "workers": workers,
        "requestTimeoutSeconds": timeout_seconds,
    }


def _configured_for_plan(config: RunConfig, plan: dict[str, Any]) -> RunConfig:
    if not plan["runtimeBudgetApplied"]:
        return config
    env = dict(config.env)
    env["NASDAQ_CAFE_MAX_FULLTEXT_ATTEMPTS"] = str(plan["effectiveAttemptLimit"])
    return replace(config, env=env)


def _relabel_runtime_budget_deferred(
    manifest: dict[str, Any],
    result: dict[str, Any],
    plan: dict[str, Any],
) -> int:
    if not plan["runtimeBudgetApplied"]:
        return 0
    deferred_ids: set[str] = set()
    limit = plan["effectiveAttemptLimit"]
    for document in manifest.get("documents", []):
        if not isinstance(document, dict):
            continue
        if document.get("fulltext_status") != "not_attempted_limit":
            continue
        document_id = str(document.get("document_id") or "")
        if document_id:
            deferred_ids.add(document_id)
        document["fulltext_status"] = "not_attempted_runtime_budget"
        document["access_status"] = "not_attempted"
        document["failure_reason"] = (
            "technical full-text runtime budget reached; URL retained for retry "
            f"(derived direct-attempt ceiling {limit})"
        )
        for attempt in document.get("attempts", []):
            if not isinstance(attempt, dict):
                continue
            if attempt.get("status") == "not_attempted_limit":
                attempt["status"] = "not_attempted_runtime_budget"
                attempt["reason"] = "technical_fulltext_runtime_budget"

    payload = result.get("payload")
    if isinstance(payload, dict):
        for row in payload.get("unreadable", []):
            if not isinstance(row, dict):
                continue
            if str(row.get("document_id") or "") in deferred_ids or row.get("fulltext_status") == "not_attempted_limit":
                row["fulltext_status"] = "not_attempted_runtime_budget"
                row["reason"] = "technical full-text runtime budget reached; URL retained for retry"
                row["notes_for_chatgpt"] = row["reason"]
        summary = payload.get("summary")
        if isinstance(summary, dict):
            summary["not_attempted_limit_count"] = max(
                0, int(summary.get("not_attempted_limit_count", 0)) - len(deferred_ids)
            )
            summary["not_attempted_runtime_budget_count"] = len(deferred_ids)
            result["summary"] = summary
    return len(deferred_ids)


def _truthful_attempted_flag(manifest: dict[str, Any]) -> bool:
    deferred = {"pending", "not_attempted_limit", "not_attempted_runtime_budget"}
    return not any(
        isinstance(document, dict)
        and str(document.get("fulltext_status") or "pending") in deferred
        for document in manifest.get("documents", [])
    )


def acquisition_coverage(manifest: dict[str, Any]) -> dict[str, Any]:
    documents = [
        row for row in manifest.get("documents", []) if isinstance(row, dict)
    ]
    eligible = [row for row in documents if row.get("fulltext_status") != "excluded"]

    def request_attempted(row: dict[str, Any]) -> bool:
        return any(
            isinstance(attempt, dict)
            and attempt.get("route") == "requests"
            and attempt.get("status")
            not in {"not_attempted_limit", "not_attempted_runtime_budget"}
            for attempt in row.get("attempts", [])
        )

    def fallback_attempted(row: dict[str, Any]) -> bool:
        return any(
            isinstance(attempt, dict)
            and str(attempt.get("route") or "").startswith("tavily_extract_")
            and attempt.get("status")
            not in {"not_attempted_limit", "not_attempted_runtime_budget"}
            for attempt in row.get("attempts", [])
        )

    direct_attempted = [row for row in eligible if request_attempted(row)]
    fallback_attempted_rows = [row for row in eligible if fallback_attempted(row)]
    direct_readable = [row for row in eligible if row.get("access_status") == "readable"]
    fallback_readable = [
        row for row in eligible if row.get("access_status") == "alternate_readable"
    ]
    all_readable = [row for row in eligible if row.get("fulltext_status") == "complete"]

    def ratio(numerator: int, denominator: int) -> float:
        return 0.0 if denominator <= 0 else round(numerator / denominator, 6)

    return {
        "contractVersion": "1.0.0",
        "manifest_unique": len(documents),
        "eligible_unique": len(eligible),
        "excluded_unique": sum(row.get("fulltext_status") == "excluded" for row in documents),
        "direct_attempted_unique": len(direct_attempted),
        "direct_readable_unique": len(direct_readable),
        "fallback_attempted_unique": len(fallback_attempted_rows),
        "fallback_readable_unique": len(fallback_readable),
        "all_readable_unique": len(all_readable),
        "failed_unique": sum(row.get("fulltext_status") == "failed" for row in eligible),
        "not_attempted_limit_unique": sum(
            row.get("fulltext_status") == "not_attempted_limit" for row in eligible
        ),
        "not_attempted_runtime_budget_unique": sum(
            row.get("fulltext_status") == "not_attempted_runtime_budget" for row in eligible
        ),
        "pending_unique": sum(row.get("fulltext_status") == "pending" for row in eligible),
        "directAttemptCoverage": ratio(len(direct_attempted), len(eligible)),
        "readableCoverage": ratio(len(all_readable), len(eligible)),
        "fallbackRecoveryRate": ratio(
            len(fallback_readable), len(fallback_attempted_rows)
        ),
        "totalAcquisitionCoverage": ratio(len(all_readable), len(eligible)),
    }


def collect_manifest_fulltext_with_policy(
    config: RunConfig,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Broad Raw retrieval with technical safeguards only.

    This policy never inspects relevance, core-driver, handoff, Expected/Actual/Gap,
    temporal-hypothesis importance, or any other editorial field.
    """
    plan = _runtime_plan(config, manifest)
    effective_config = _configured_for_plan(config, plan)
    result = collect_manifest_fulltext(effective_config, manifest)
    deferred_count = _relabel_runtime_budget_deferred(manifest, result, plan)

    retrieval_policy = manifest.setdefault("retrieval_policy", {})
    retrieval_policy["all_manifest_article_urls_attempted"] = _truthful_attempted_flag(manifest)
    retrieval_policy["configured_fulltext_attempt_limit"] = (
        _nonnegative_int(config.env.get("NASDAQ_CAFE_MAX_FULLTEXT_ATTEMPTS"), 0) or None
    )
    retrieval_policy["technical_runtime_budget_seconds"] = plan["runtimeBudgetSeconds"]
    retrieval_policy["runtime_budget_effective_attempt_limit"] = (
        plan["effectiveAttemptLimit"] if plan["runtimeBudgetApplied"] else None
    )
    retrieval_policy["runtime_budget_deferred_count"] = deferred_count
    retrieval_policy["selection_fields_ignored"] = [
        "relevance_score",
        "review_priority",
        "core_driver",
        "low_value",
        "selected_for_handoff",
    ]

    coverage = acquisition_coverage(manifest)
    result["acquisition_coverage"] = coverage
    if deferred_count:
        result["status"] = "partial"

    write_json(config.raw_dir / "manifest.json", manifest)
    payload = result.get("payload")
    if isinstance(payload, dict):
        write_json(config.raw_dir / "article_fulltext.json", payload)
    return result
