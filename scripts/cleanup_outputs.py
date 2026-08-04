from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT_DIR / "output"
INVALID_KEY_VALUES = {
    "",
    "your_serpapi_api_key",
    "your_tavily_api_key",
    "todo",
    "dummy",
    "test",
    "none",
    "null",
}


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


@dataclass
class PlannedMove:
    source: Path
    destination: Path
    reason: str


@dataclass
class PlannedDelete:
    path: Path
    reason: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean Phase 1 output files without deleting important artifacts.")
    parser.add_argument("--date", default="today", help="'today' or YYYY-MM-DD")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true", help="Show planned moves/deletes without applying them.")
    action.add_argument("--apply", action="store_true", help="Apply cleanup moves/deletes.")
    args = parser.parse_args()

    target_date = _parse_target_date(args.date)
    report = cleanup_outputs(target_date, apply_changes=args.apply)
    print(report)
    return 0


def cleanup_outputs(target_date: str, apply_changes: bool) -> str:
    target_dir = OUTPUT_DIR / target_date
    if not target_dir.exists():
        raise SystemExit(f"Output directory does not exist: {_display(target_dir)}")

    archive_dir = target_dir / "_archive"
    formal_file = target_dir / f"CHATGPT_HANDOFF_{target_date}.md"
    latest_file = OUTPUT_DIR / "latest" / "chatgpt_handoff.md"
    report_file = target_dir / "cleanup_report.md"

    planned_moves = _planned_archive_moves(target_dir, archive_dir, target_date)
    planned_deletes = _planned_deletes(target_dir)

    moved: list[PlannedMove] = []
    deleted: list[PlannedDelete] = []
    not_deleted = _not_deleted_reasons(target_dir, target_date)
    fulltext_metadata = _fulltext_cleanup_metadata(target_dir, target_date, planned_moves)
    gdelt_metadata = _gdelt_cleanup_metadata(target_dir, planned_moves)

    if apply_changes:
        if planned_moves:
            archive_dir.mkdir(parents=True, exist_ok=True)
        for move in planned_moves:
            if not move.source.exists():
                not_deleted.append(f"{_display(move.source)}: already missing.")
                continue
            if _is_protected_daily_path(move.source, target_dir, target_date):
                not_deleted.append(f"{_display(move.source)}: protected file, not archived.")
                continue
            destination = _unique_destination(move.destination)
            _assert_within_workspace(move.source)
            _assert_within_workspace(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(move.source), str(destination))
            moved.append(PlannedMove(move.source, destination, move.reason))

        for delete in planned_deletes:
            if not delete.path.exists():
                continue
            if _is_protected_daily_path(delete.path, target_dir, target_date):
                not_deleted.append(f"{_display(delete.path)}: protected file, not deleted.")
                continue
            _assert_within_workspace(delete.path)
            _delete_path(delete.path)
            deleted.append(delete)

    report = _render_report(
        target_date=target_date,
        mode="apply" if apply_changes else "dry-run",
        formal_file=formal_file,
        latest_file=latest_file,
        kept_files=_kept_files(target_dir, target_date),
        planned_moves=planned_moves,
        moved=moved,
        planned_deletes=planned_deletes,
        deleted=deleted,
        not_deleted=not_deleted,
        fulltext_metadata=fulltext_metadata,
        gdelt_metadata=gdelt_metadata,
    )
    report_file.write_text(report, encoding="utf-8")
    return report


def _planned_archive_moves(target_dir: Path, archive_dir: Path, target_date: str) -> list[PlannedMove]:
    candidates = [
        (
            target_dir / "chatgpt_handoff.md",
            archive_dir / "chatgpt_handoff.md",
            "正式版 CHATGPT_HANDOFF_YYYY-MM-DD.md と重複するため。",
        ),
        (
            target_dir / "prompt_input.md",
            archive_dir / "prompt_input.md",
            "ChatGPT送信用は handoff に一本化したため。中間生成物として保管。",
        ),
        (
            target_dir / "raw" / "article_fulltext.json",
            archive_dir / "raw" / "article_fulltext.json",
            "Article Full Text Package の一時本文キャッシュ。手動投入後のarchive/delete対象。",
        ),
        (
            target_dir / f"CHATGPT_FULLTEXT_HANDOFF_{target_date}.md",
            archive_dir / f"CHATGPT_FULLTEXT_HANDOFF_{target_date}.md",
            "Article Full Text Package の本文入り一時handoff。手動投入後のarchive/delete対象。",
        ),
        (
            OUTPUT_DIR / "latest" / "chatgpt_fulltext_handoff.md",
            archive_dir / "chatgpt_fulltext_handoff_latest.md",
            "Article Full Text Package のlatest確認用ショートカット。",
        ),
    ]
    if _is_older_than_days(target_date, 30):
        candidates.append(
            (
                target_dir / "raw" / "gdelt_radar.json",
                archive_dir / "raw" / "gdelt_radar.json",
                "GDELT Radar metadata raw cache older than 30 days. long_term_archive_candidate.",
            )
        )

    old_review_patterns = ("phase*_check_*.md", "*verification*.md", "*review*.md")
    for pattern in old_review_patterns:
        for path in target_dir.glob(pattern):
            if path.name in {"phase1_check.md", "cleanup_report.md"}:
                continue
            if path.is_file() and not _is_protected_daily_path(path, target_dir, target_date):
                candidates.append((path, archive_dir / path.name, "古い検収用ファイルのため。"))

    return [
        PlannedMove(source=source, destination=destination, reason=reason)
        for source, destination, reason in candidates
        if source.exists()
    ]


def _planned_deletes(target_dir: Path) -> list[PlannedDelete]:
    deletes: list[PlannedDelete] = []

    for path in ROOT_DIR.rglob("__pycache__"):
        if path.is_dir():
            deletes.append(PlannedDelete(path, "Python bytecode cache."))
    for path in ROOT_DIR.rglob(".pytest_cache"):
        if path.is_dir():
            deletes.append(PlannedDelete(path, "pytest cache."))
    for pattern in ("*.pyc", "*.tmp", "*.temp"):
        for path in ROOT_DIR.rglob(pattern):
            if path.is_file():
                deletes.append(PlannedDelete(path, "temporary file."))
    for path in ROOT_DIR.rglob("youtube_materials.md"):
        if path.is_file():
            deletes.append(PlannedDelete(path, "Codex側でYouTube素材を生成しない方針に反するため。"))

    return _dedupe_deletes(deletes)


def _kept_files(target_dir: Path, target_date: str) -> list[str]:
    names = [
        f"CHATGPT_HANDOFF_{target_date}.md",
        "source_pack.md",
        "source_pack.json",
        "README_FOR_HUMAN.md",
        "phase1_check.md",
        "raw/",
        "cleanup_report.md",
    ]
    return [_display(target_dir / name.rstrip("/")) + ("/" if name.endswith("/") else "") for name in names if (target_dir / name.rstrip("/")).exists()]


def _not_deleted_reasons(target_dir: Path, target_date: str) -> list[str]:
    protected = [
        ROOT_DIR / ".env",
        ROOT_DIR / ".env.example",
        ROOT_DIR / "README.md",
        ROOT_DIR / ".gitignore",
        ROOT_DIR / ".agents",
        ROOT_DIR / "nasdaq_cafe",
        ROOT_DIR / "scripts",
        target_dir / "source_pack.md",
        target_dir / "source_pack.json",
        target_dir / "raw",
        target_dir / f"CHATGPT_HANDOFF_{target_date}.md",
    ]
    reasons = []
    for path in protected:
        if path.exists():
            reasons.append(f"{_display(path)}: protected by cleanup rules.")
    reasons.append(".env values were not displayed or shared.")
    reasons.append("raw/rss_news.json, raw/longbridge_quotes.json, raw/fred_dgs10.json, raw/market_movers.json are protected.")
    return reasons


def _fulltext_cleanup_metadata(target_dir: Path, target_date: str, planned_moves: list[PlannedMove]) -> list[str]:
    raw_path = target_dir / "raw" / "article_fulltext.json"
    dated_handoff = target_dir / f"CHATGPT_FULLTEXT_HANDOFF_{target_date}.md"
    latest_handoff = OUTPUT_DIR / "latest" / "chatgpt_fulltext_handoff.md"
    planned_sources = {move.source.resolve() for move in planned_moves if move.source.exists()}

    summary = _read_article_fulltext_summary(raw_path)
    article_action = "archived" if raw_path.exists() and raw_path.resolve() in planned_sources else "retained"
    handoff_planned = any(path.exists() and path.resolve() in planned_sources for path in [dated_handoff, latest_handoff])
    handoff_action = "archived" if handoff_planned else "retained"

    return [
        f"article_fulltext_processed: {str(raw_path.exists()).lower()}",
        f"article_fulltext_action: {article_action}",
        f"article_fulltext_items_count: {summary.get('items_count', 0)}",
        f"article_fulltext_total_chars: {summary.get('total_full_text_chars', 0)}",
        f"fulltext_handoff_processed: {str(dated_handoff.exists() or latest_handoff.exists()).lower()}",
        f"fulltext_handoff_action: {handoff_action}",
    ]


def _gdelt_cleanup_metadata(target_dir: Path, planned_moves: list[PlannedMove]) -> list[str]:
    raw_path = target_dir / "raw" / "gdelt_radar.json"
    planned_sources = {move.source.resolve() for move in planned_moves if move.source.exists()}
    if not raw_path.exists():
        action = "missing"
    elif raw_path.resolve() in planned_sources:
        action = "long_term_archive_candidate"
    else:
        action = "retained"
    return [
        f"gdelt_radar_path: {_display(raw_path)}",
        f"gdelt_radar_action: {action}",
        "gdelt_radar_cleanup_rule: retain current metadata raw; only GDELT Radar raw older than 30 days becomes long_term_archive_candidate.",
    ]


def _read_article_fulltext_summary(path: Path) -> dict[str, int]:
    if not path.exists():
        return {"items_count": 0, "total_full_text_chars": 0}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items_count": 0, "total_full_text_chars": 0}
    items = payload.get("items", []) if isinstance(payload, dict) else []
    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    if isinstance(summary, dict) and "total_full_text_chars" in summary:
        total_chars = int(summary.get("total_full_text_chars") or 0)
    else:
        total_chars = sum(int(item.get("full_text_char_count") or 0) for item in items if isinstance(item, dict))
    return {"items_count": len(items) if isinstance(items, list) else 0, "total_full_text_chars": total_chars}


def _render_report(
    *,
    target_date: str,
    mode: str,
    formal_file: Path,
    latest_file: Path,
    kept_files: list[str],
    planned_moves: list[PlannedMove],
    moved: list[PlannedMove],
    planned_deletes: list[PlannedDelete],
    deleted: list[PlannedDelete],
    not_deleted: list[str],
    fulltext_metadata: list[str],
    gdelt_metadata: list[str],
) -> str:
    move_rows = moved if mode == "apply" else planned_moves
    delete_rows = deleted if mode == "apply" else planned_deletes
    move_heading = "archiveに移動したファイル" if mode == "apply" else "archiveに移動予定のファイル"
    delete_heading = "削除したファイル" if mode == "apply" else "削除予定のファイル"

    lines = [
        "# Cleanup Report",
        "",
        f"- target_date: {target_date}",
        f"- mode: {mode}",
        f"- generated_at_utc: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## 正式送信ファイル",
        f"- {_display(formal_file)}",
        "",
        "## 確認用latest",
        f"- {_display(latest_file)}",
        "",
        "## 残したファイル",
        *(_prefix(kept_files) or ["- なし"]),
        "",
        f"## {move_heading}",
        *(_format_moves(move_rows) or ["- なし"]),
        "",
        f"## {delete_heading}",
        *(_format_deletes(delete_rows) or ["- なし"]),
        "",
        "## 削除しなかった理由",
        *(_prefix(not_deleted) or ["- なし"]),
        "",
        "## Article Full Text Cleanup Metadata",
        *(_prefix(fulltext_metadata) or ["- none"]),
        "",
        "## GDELT Radar Cleanup Metadata",
        *(_prefix(gdelt_metadata) or ["- none"]),
        "",
        "## 注意事項",
        "- ChatGPTに送る正式ファイルは日付付き大文字版です。",
        "- output/latest/chatgpt_handoff.md は確認用であり、メール添付や正式送信には使いません。",
        "- dry-run は移動・削除を行わず、予定だけを表示します。",
        "- .env は削除・表示・共有しません。",
        "",
    ]
    return "\n".join(lines)


def _format_moves(moves: list[PlannedMove]) -> list[str]:
    return [f"- {_display(move.source)} -> {_display(move.destination)}: {move.reason}" for move in moves]


def _format_deletes(deletes: list[PlannedDelete]) -> list[str]:
    return [f"- {_display(delete.path)}: {delete.reason}" for delete in deletes]


def _prefix(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items]


def _delete_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _is_protected_daily_path(path: Path, target_dir: Path, target_date: str) -> bool:
    protected = {
        target_dir / f"CHATGPT_HANDOFF_{target_date}.md",
        target_dir / "source_pack.md",
        target_dir / "source_pack.json",
        target_dir / "README_FOR_HUMAN.md",
        target_dir / "phase1_check.md",
        target_dir / "cleanup_report.md",
        target_dir / "raw",
    }
    try:
        resolved = path.resolve()
    except FileNotFoundError:
        resolved = path.absolute()
    return any(resolved == item.resolve() for item in protected if item.exists() or item.parent.exists())


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    counter = 1
    while True:
        candidate = path.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _dedupe_deletes(deletes: list[PlannedDelete]) -> list[PlannedDelete]:
    seen: set[Path] = set()
    unique: list[PlannedDelete] = []
    for delete in deletes:
        resolved = delete.path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(delete)
    return unique


def _read_env_keys() -> dict[str, str]:
    path = ROOT_DIR / ".env"
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _valid_search_key(value: str, provider: str) -> bool:
    key = value.strip()
    lowered = key.lower()
    return bool(key and lowered not in INVALID_KEY_VALUES and lowered != f"your_{provider}_api_key")


def _is_older_than_days(target_date: str, days: int) -> bool:
    try:
        parsed = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        return False
    return (date.today() - parsed).days >= days


def _parse_target_date(value: str) -> str:
    if value.lower() == "today":
        return date.today().isoformat()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise SystemExit("--date must be 'today' or YYYY-MM-DD") from exc


def _assert_within_workspace(path: Path) -> None:
    resolved = path.resolve()
    root = ROOT_DIR.resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeError(f"Refusing to modify path outside workspace: {resolved}")


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT_DIR.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
