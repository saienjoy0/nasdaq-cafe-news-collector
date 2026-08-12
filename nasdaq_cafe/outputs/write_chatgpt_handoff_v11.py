from __future__ import annotations

from pathlib import Path
from typing import Any

from nasdaq_cafe.outputs.submission_bundle import copy_submission_files
from nasdaq_cafe.outputs.write_chatgpt_handoff import (
    render_chatgpt_handoff as render_legacy_handoff,
    write_readme_for_human,
)
from nasdaq_cafe.processing.research_candidate_pool import PERSPECTIVES


LEGACY_INSERT_MARKER = "\n## Core Drivers\n"


def write_chatgpt_handoff(output_dir: Path, pack: dict[str, Any]) -> None:
    content = render_chatgpt_handoff(pack)
    dated_copy = output_dir / f"CHATGPT_HANDOFF_{output_dir.name}.md"
    dated_copy.write_text(content, encoding="utf-8")
    (output_dir / "chatgpt_handoff.md").write_text(content, encoding="utf-8")

    latest_dir = output_dir.parent / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    (latest_dir / "chatgpt_handoff.md").write_text(content, encoding="utf-8")

    write_readme_for_human(output_dir)
    copy_submission_files(output_dir)


def render_chatgpt_handoff(pack: dict[str, Any]) -> str:
    legacy = render_legacy_handoff(pack)
    broad = "\n".join(
        [
            "## Cross-Market Snapshot",
            "",
            *_render_cross_market(pack.get("cross_market_snapshot", {})),
            "",
            "## Discovery Coverage",
            "",
            *_render_coverage(pack.get("discovery_coverage", {})),
            "",
            "## Research Candidate Pool",
            "",
            *_render_candidate_pool(pack.get("research_candidate_pool", {})),
            "",
            "Broad Discovery above is observation-only. Candidate count is not confidence; formal NASDAQ causality is decided downstream.",
        ]
    )
    if LEGACY_INSERT_MARKER in legacy:
        return legacy.replace(LEGACY_INSERT_MARKER, "\n" + broad + LEGACY_INSERT_MARKER, 1)
    return legacy.rstrip() + "\n\n" + broad + "\n"


def _render_cross_market(snapshot: Any) -> list[str]:
    if not isinstance(snapshot, dict) or not snapshot:
        return ["Cross-Market Snapshot unavailable."]
    lines = [
        "definition: " + _clean(snapshot.get("definition")),
        "status: " + _clean(snapshot.get("status")),
        "us_anchor_market_date_local: " + _clean(snapshot.get("us_anchor_market_date_local")),
    ]
    markets = snapshot.get("markets", {})
    if not isinstance(markets, dict):
        return lines + ["markets: unavailable"]
    for market in ("us", "japan", "china", "hong_kong", "cross_asset"):
        lines.extend(["", f"### {market}"])
        entries = markets.get(market, [])
        if not isinstance(entries, list) or not entries:
            lines.append("- none")
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            status = _clean(item.get("status"))
            instrument = _clean(item.get("instrument"))
            if status == "unavailable":
                lines.extend(
                    [
                        f"- {instrument}: unavailable",
                        f"  - reason: {_clean(item.get('reason'))}",
                        f"  - timezone: {_clean(item.get('timezone'))}",
                        f"  - relation_to_us_session: {_clean(item.get('relation_to_us_session'))}",
                    ]
                )
                continue
            lines.extend(
                [
                    f"- {instrument}",
                    f"  - market_date_local: {_clean(item.get('market_date_local'))}",
                    f"  - timezone: {_clean(item.get('timezone'))}",
                    f"  - session: {_clean(item.get('session'))}",
                    f"  - session_status: {_clean(item.get('session_status'))}",
                    f"  - relation_to_us_session: {_clean(item.get('relation_to_us_session'))}",
                    f"  - price: {_clean(item.get('price'))}",
                    f"  - change_percent: {_clean(item.get('change_percent'))}",
                    f"  - source: {_clean(item.get('source'))}",
                    f"  - source_symbol: {_clean(item.get('source_symbol'))}",
                ]
            )
    return lines


def _render_coverage(coverage: Any) -> list[str]:
    if not isinstance(coverage, dict) or not coverage:
        return ["Discovery Coverage unavailable."]
    lines = [
        "all_perspectives_attempted: " + _clean(coverage.get("all_perspectives_attempted")),
        "attempted_perspective_count: " + _clean(coverage.get("attempted_perspective_count")),
        "note: " + _clean(coverage.get("note")),
        "",
    ]
    rows = coverage.get("perspectives", {})
    if not isinstance(rows, dict):
        return lines + ["- no perspective rows"]
    for perspective in PERSPECTIVES:
        row = rows.get(perspective["key"], {})
        if not isinstance(row, dict):
            row = {}
        lines.extend(
            [
                f"### {perspective['id']} {perspective['label']}",
                "attempted: " + _clean(row.get("attempted")),
                "provider_attempts: " + _compact_map(row.get("provider_attempts")),
                "provider_status: " + _compact_map(row.get("provider_status")),
                "raw_count: " + _clean(row.get("raw_count")),
                "candidate_count: " + _clean(row.get("candidate_count")),
                "",
            ]
        )
    return lines[:-1]


def _render_candidate_pool(pool: Any) -> list[str]:
    if not isinstance(pool, dict) or not pool:
        return ["Research Candidate Pool unavailable."]
    lines = [
        "pool_count: " + _clean(pool.get("pool_count")),
        "handoff_count: " + _clean(pool.get("handoff_count")),
        "note: " + _clean(pool.get("note")),
        "",
    ]
    by_perspective = pool.get("by_perspective", {})
    if not isinstance(by_perspective, dict):
        by_perspective = {}
    for perspective in PERSPECTIVES:
        lines.append(f"### {perspective['id']} {perspective['label']}")
        items = by_perspective.get(perspective["id"], [])
        if not isinstance(items, list) or not items:
            lines.extend(["- candidateなし", ""])
            continue
        for item in items[:3]:
            if not isinstance(item, dict):
                continue
            provenance = item.get("provenance", [])
            lines.extend(
                [
                    "title: " + _clean(item.get("title")),
                    "provider: " + _clean(item.get("provider")),
                    "source: " + _clean(item.get("source")),
                    "published_at: " + _clean(item.get("published_at")),
                    "url: " + _clean(item.get("url")),
                    "region_tags: " + _clean_list(item.get("region_tags")),
                    "topic_tags: " + _clean_list(item.get("topic_tags")),
                    "perspective_ids: " + _clean_list(item.get("perspective_ids")),
                    "quality_status: " + _clean(item.get("quality_status")),
                    "body_verified: " + _clean(item.get("body_verified")),
                    "needs_fulltext_before_script: " + _clean(item.get("needs_fulltext_before_script")),
                    "provenance: " + _provenance(provenance),
                    "",
                ]
            )
    return lines[:-1]


def _provenance(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts = []
    for item in value:
        if not isinstance(item, dict):
            continue
        parts.append(
            f"{_clean(item.get('provider'))}/{_clean(item.get('source'))}: {_clean(item.get('url'))}"
        )
    return " | ".join(parts)


def _compact_map(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return ", ".join(f"{key}={_clean(item)}" for key, item in sorted(value.items()))


def _clean_list(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    return ", ".join(_clean(item) for item in value if _clean(item))


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", " ").strip()
