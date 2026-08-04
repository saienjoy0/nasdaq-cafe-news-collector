from __future__ import annotations

import shutil
from pathlib import Path


SUBMISSION_DIR_NAME = "chatgpt_submission"
REGULAR_CONTEXT_MARKER = "\n## Regular Handoff Context\n"


def copy_submission_files(output_dir: Path) -> list[Path]:
    """Keep the five legacy files and add one deduplicated daily package."""
    target_date = output_dir.name
    submission_dir = output_dir / SUBMISSION_DIR_NAME
    submission_dir.mkdir(parents=True, exist_ok=True)

    sources = _legacy_source_paths(output_dir)
    copied: list[Path] = []
    for source in sources:
        if not source.is_file():
            continue
        destination = submission_dir / source.name
        shutil.copy2(source, destination)
        copied.append(destination)

    package = write_daily_source_package(output_dir)
    if package is not None:
        destination = submission_dir / package.name
        shutil.copy2(package, destination)
        copied.append(destination)
    return copied


def write_daily_source_package(output_dir: Path) -> Path | None:
    """Merge the five submission files while removing exact handoff duplication."""
    target_date = output_dir.name
    dated_handoff = output_dir / f"CHATGPT_HANDOFF_{target_date}.md"
    fulltext_handoff = output_dir / f"CHATGPT_FULLTEXT_HANDOFF_{target_date}.md"
    alias_handoff = output_dir / "chatgpt_handoff.md"
    source_pack = output_dir / "source_pack.md"
    prompt_input = output_dir / "prompt_input.md"
    required = [dated_handoff, fulltext_handoff, alias_handoff, source_pack, prompt_input]
    if not all(path.is_file() for path in required):
        return None

    normal_text = _read(dated_handoff)
    alias_text = _read(alias_handoff)
    fulltext_text = _read(fulltext_handoff)
    unique_fulltext, embedded_handoff_removed = _dedupe_fulltext_handoff(fulltext_text, normal_text)

    sections = [
        _section("Part 1: Internal Generation Input", prompt_input.name, _read(prompt_input)),
        _section("Part 2: Canonical ChatGPT Handoff", dated_handoff.name, normal_text),
        _section("Part 3: Collection Record", source_pack.name, _read(source_pack)),
        _section("Part 4: Article Full Text and Retrieval Failures", fulltext_handoff.name, unique_fulltext),
    ]
    alias_deduplicated = alias_text == normal_text
    if not alias_deduplicated:
        sections.append(_section("Part 5: Legacy Handoff Alias", alias_handoff.name, alias_text))

    header = [
        "# Daily Source Package",
        "",
        f"target_date: {target_date}",
        "intended_use: Upload this file alone to ChatGPT.",
        "contains: market data, source-pack details, generation input, article full text, and retrieval failures.",
        "",
        "## Integration Manifest",
        "",
        f"- {prompt_input.name}: included once",
        f"- {dated_handoff.name}: included once as canonical regular handoff",
        f"- {source_pack.name}: included once",
        f"- {fulltext_handoff.name}: included once",
        (
            f"- {alias_handoff.name}: exact duplicate of {dated_handoff.name}; represented by the canonical copy"
            if alias_deduplicated
            else f"- {alias_handoff.name}: content differs; included separately"
        ),
        (
            "- embedded Regular Handoff Context in the fulltext file: exact duplicate removed and represented by the canonical handoff"
            if embedded_handoff_removed
            else "- embedded Regular Handoff Context in the fulltext file: retained because it was not an exact duplicate"
        ),
        "",
    ]
    package_path = output_dir / f"daily_source_package_{target_date}.md"
    package_path.write_text("\n".join(header + sections).rstrip() + "\n", encoding="utf-8")
    return package_path


def _legacy_source_paths(output_dir: Path) -> list[Path]:
    target_date = output_dir.name
    return [
        output_dir / f"CHATGPT_HANDOFF_{target_date}.md",
        output_dir / f"CHATGPT_FULLTEXT_HANDOFF_{target_date}.md",
        output_dir / "chatgpt_handoff.md",
        output_dir / "source_pack.md",
        output_dir / "prompt_input.md",
    ]


def _dedupe_fulltext_handoff(fulltext: str, normal_handoff: str) -> tuple[str, bool]:
    if REGULAR_CONTEXT_MARKER not in fulltext:
        return fulltext, False
    unique_part, embedded = fulltext.split(REGULAR_CONTEXT_MARKER, 1)
    embedded = embedded.lstrip("\n")
    if embedded == normal_handoff:
        return unique_part.rstrip() + "\n", True
    return fulltext, False


def _section(title: str, source_name: str, content: str) -> str:
    return "\n".join(
        [
            f"## {title}",
            "",
            f"source_file: {source_name}",
            "",
            f"<!-- BEGIN {source_name} -->",
            content.rstrip(),
            f"<!-- END {source_name} -->",
            "",
        ]
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")
