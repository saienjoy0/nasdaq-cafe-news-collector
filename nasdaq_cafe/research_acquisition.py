from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from nasdaq_cafe.cache import write_json
from nasdaq_cafe.collectors.longbridge_cli_quotes import (
    fetch_longbridge_intraday,
    fetch_longbridge_quotes,
)
from nasdaq_cafe.config import RunConfig, build_config
from nasdaq_cafe.raw_archive import register_and_fetch_url


CONTRACT_VERSION = "1.0.0"
MAX_WAVES = 2
MAX_MARKET_REQUESTS = 8
MAX_EXACT_URL_REQUESTS = 8
MAX_TOTAL_REQUESTS = 12
REQUEST_TYPES = {"market_intraday", "market_quote", "exact_url_archive"}
REQUEST_STATUSES = {
    "success",
    "unavailable",
    "not-supported",
    "invalid-request",
    "provider-error",
}
SYMBOL_RE = re.compile(r"^(?:[A-Z][A-Z0-9.-]{0,20}|\.[A-Z0-9]{1,12})\.US$")
REQUEST_ID_RE = re.compile(r"^RA-[A-Z0-9-]{1,32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ResearchAcquisitionError(ValueError):
    pass


def load_request(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchAcquisitionError(f"research acquisition request invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise ResearchAcquisitionError("research acquisition request root must be an object")
    return value


def validate_request_document(value: dict[str, Any], *, episode_date: str | None = None) -> dict[str, Any]:
    errors: list[str] = []
    if value.get("contractVersion") != CONTRACT_VERSION:
        errors.append(f"contractVersion must be {CONTRACT_VERSION}")

    request_episode_date = value.get("episodeDate")
    try:
        normalized_episode_date = _parse_date(request_episode_date, "episodeDate")
    except ResearchAcquisitionError as exc:
        errors.append(str(exc))
        normalized_episode_date = str(request_episode_date or "")
    if episode_date and normalized_episode_date != episode_date:
        errors.append("episodeDate does not match collector target date")

    wave = value.get("wave")
    if not isinstance(wave, int) or isinstance(wave, bool) or not 1 <= wave <= MAX_WAVES:
        errors.append(f"wave must be an integer from 1 to {MAX_WAVES}")

    base_sha = str(value.get("baseResearchInputManifestSha256") or "")
    if not SHA256_RE.fullmatch(base_sha):
        errors.append("baseResearchInputManifestSha256 must be a lowercase SHA-256")

    purpose = value.get("researchPurpose")
    if not isinstance(purpose, str) or not purpose.strip():
        errors.append("researchPurpose must be a non-empty string")

    requests = value.get("requests")
    if not isinstance(requests, list) or not requests:
        errors.append("requests must be a non-empty array")
        requests = []
    if len(requests) > MAX_TOTAL_REQUESTS:
        errors.append(f"requests may contain at most {MAX_TOTAL_REQUESTS} entries")

    seen_ids: set[str] = set()
    market_keys: set[tuple[str, str, str, str]] = set()
    market_count = 0
    exact_url_count = 0
    normalized_requests: list[dict[str, Any]] = []

    for index, item in enumerate(requests):
        prefix = f"requests[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue

        request_id = str(item.get("requestId") or "")
        if not REQUEST_ID_RE.fullmatch(request_id):
            errors.append(f"{prefix}.requestId is invalid")
        elif request_id in seen_ids:
            errors.append(f"duplicate requestId: {request_id}")
        seen_ids.add(request_id)

        request_type = item.get("type")
        if request_type not in REQUEST_TYPES:
            errors.append(f"{prefix}.type is unsupported: {request_type!r}")
            continue

        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{prefix}.reason must be non-empty")
        requiredness = item.get("requiredness")
        if requiredness not in {"material", "supporting"}:
            errors.append(f"{prefix}.requiredness must be material or supporting")

        parameters = item.get("parameters")
        if not isinstance(parameters, dict):
            errors.append(f"{prefix}.parameters must be an object")
            continue

        normalized_parameters: dict[str, Any]
        if request_type in {"market_intraday", "market_quote"}:
            market_count += 1
            try:
                symbol = validate_us_symbol(parameters.get("symbol"))
            except ResearchAcquisitionError as exc:
                errors.append(f"{prefix}.parameters.symbol: {exc}")
                continue

            if request_type == "market_intraday":
                try:
                    market_date = _parse_date(parameters.get("date"), f"{prefix}.parameters.date")
                except ResearchAcquisitionError as exc:
                    errors.append(str(exc))
                    continue
                resolution = parameters.get("resolution")
                if resolution != "1m":
                    errors.append(f"{prefix}.parameters.resolution must be 1m")
                    continue
                session = parameters.get("session")
                if session not in {"regular", "all"}:
                    errors.append(f"{prefix}.parameters.session must be regular or all")
                    continue
                normalized_parameters = {
                    "symbol": symbol,
                    "date": market_date,
                    "resolution": "1m",
                    "session": session,
                }
                dedupe_key = (request_type, symbol, market_date, session)
            else:
                normalized_parameters = {"symbol": symbol}
                dedupe_key = (request_type, symbol, "", "")

            if dedupe_key in market_keys:
                errors.append(f"{prefix}: duplicate market request")
                continue
            market_keys.add(dedupe_key)
        else:
            exact_url_count += 1
            try:
                url = validate_exact_url(parameters.get("url"))
            except ResearchAcquisitionError as exc:
                errors.append(f"{prefix}.parameters.url: {exc}")
                continue
            title = parameters.get("title", "")
            if not isinstance(title, str):
                errors.append(f"{prefix}.parameters.title must be a string")
                continue
            normalized_parameters = {"url": url, "title": title.strip()}

        normalized_requests.append(
            {
                "requestId": request_id,
                "type": request_type,
                "reason": str(reason or "").strip(),
                "requiredness": requiredness,
                "parameters": normalized_parameters,
            }
        )

    if market_count > MAX_MARKET_REQUESTS:
        errors.append(f"market requests may contain at most {MAX_MARKET_REQUESTS} entries")
    if exact_url_count > MAX_EXACT_URL_REQUESTS:
        errors.append(f"exact URL requests may contain at most {MAX_EXACT_URL_REQUESTS} entries")
    if errors:
        raise ResearchAcquisitionError("\n".join(errors))

    return {
        "contractVersion": CONTRACT_VERSION,
        "episodeDate": normalized_episode_date,
        "wave": wave,
        "baseResearchInputManifestSha256": base_sha,
        "researchPurpose": purpose.strip(),
        "requests": normalized_requests,
    }


def run_followup(config: RunConfig, request_path: Path) -> dict[str, Any]:
    request = validate_request_document(load_request(request_path), episode_date=config.target_date)
    wave = request["wave"]
    followup_root = config.output_dir / "followup" / f"wave-{wave:02d}"
    raw_root = config.raw_dir / "followup" / f"wave-{wave:02d}"
    followup_root.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)

    canonical_request_path = followup_root / "research_acquisition_request.json"
    write_json(canonical_request_path, request)
    request_sha = sha256_file(canonical_request_path)

    results: list[dict[str, Any]] = []
    for item in request["requests"]:
        result = _execute_request(
            config=config,
            item=item,
            followup_root=followup_root,
            raw_root=raw_root,
        )
        results.append(result)

    success_count = sum(1 for item in results if item["status"] == "success")
    if success_count == len(results):
        overall = "success"
    elif success_count:
        overall = "partial"
    else:
        overall = "unavailable"

    result_document = {
        "contractVersion": CONTRACT_VERSION,
        "episodeDate": request["episodeDate"],
        "wave": wave,
        "requestSha256": request_sha,
        "status": overall,
        "results": results,
    }
    result_path = followup_root / "research_acquisition_result.json"
    write_json(result_path, result_document)

    manifest = {
        "contractVersion": CONTRACT_VERSION,
        "episodeDate": request["episodeDate"],
        "wave": wave,
        "requestPath": str(canonical_request_path.relative_to(config.output_dir)),
        "requestSha256": request_sha,
        "resultPath": str(result_path.relative_to(config.output_dir)),
        "resultSha256": sha256_file(result_path),
        "files": [
            {
                "path": item["outputPath"],
                "sha256": item["sha256"],
            }
            for item in results
            if item.get("outputPath") and item.get("sha256")
        ],
    }
    manifest_path = followup_root / "followup_manifest.json"
    write_json(manifest_path, manifest)

    return {
        "status": overall,
        "request_path": canonical_request_path,
        "result_path": result_path,
        "manifest_path": manifest_path,
        "result": result_document,
    }


def validate_us_symbol(value: Any) -> str:
    if not isinstance(value, str):
        raise ResearchAcquisitionError("symbol must be a string")
    symbol = value.strip().upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise ResearchAcquisitionError("symbol must be a read-only US market symbol such as AMD.US or .IXIC.US")
    return symbol


def validate_exact_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ResearchAcquisitionError("URL must be a string")
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ResearchAcquisitionError("URL must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ResearchAcquisitionError("URL credentials are forbidden")
    return url


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _execute_request(
    *,
    config: RunConfig,
    item: dict[str, Any],
    followup_root: Path,
    raw_root: Path,
) -> dict[str, Any]:
    request_id = item["requestId"]
    request_type = item["type"]
    parameters = item["parameters"]

    try:
        if request_type == "market_intraday":
            provider_result = fetch_longbridge_intraday(
                symbol=parameters["symbol"],
                target_date=parameters["date"],
                session=parameters["session"],
            )
            if provider_result["status"] != "fetched":
                return _unavailable_result(request_id, "Longbridge", provider_result)
            raw_path = raw_root / f"{request_id}_longbridge_intraday_raw.json"
            normalized_path = followup_root / f"{request_id}_intraday_series.json"
            write_json(raw_path, provider_result["rawRows"])
            normalized = provider_result["series"]
            normalized["rawSha256"] = sha256_file(raw_path)
            write_json(normalized_path, normalized)
            return _success_result(
                request_id=request_id,
                provider="Longbridge",
                output_path=normalized_path.relative_to(config.output_dir),
                sha256=sha256_file(normalized_path),
                record_count=len(normalized["points"]),
            )

        if request_type == "market_quote":
            provider_result = fetch_longbridge_quotes([parameters["symbol"]])
            if provider_result["status"] != "fetched":
                return _unavailable_result(request_id, "Longbridge", provider_result)
            output_path = followup_root / f"{request_id}_quote.json"
            payload = {
                "source": "Longbridge",
                "kind": "quote",
                "symbol": parameters["symbol"],
                "items": provider_result["items"],
            }
            write_json(output_path, payload)
            return _success_result(
                request_id=request_id,
                provider="Longbridge",
                output_path=output_path.relative_to(config.output_dir),
                sha256=sha256_file(output_path),
                record_count=len(provider_result["items"]),
            )

        if request_type == "exact_url_archive":
            archive_result = register_and_fetch_url(
                config,
                parameters["url"],
                parameters.get("title", ""),
            )
            summary = archive_result.get("summary", {})
            complete_count = int(summary.get("complete_count", 0) or 0)
            if complete_count <= 0:
                return {
                    "requestId": request_id,
                    "status": "unavailable",
                    "provider": "Raw Archive",
                    "outputPath": None,
                    "sha256": None,
                    "recordCount": 0,
                    "reason": "exact URL could not be materialized as readable full text",
                }
            output_path = followup_root / f"{request_id}_exact_url_archive.json"
            snapshot = {
                "requestedUrl": parameters["url"],
                "title": parameters.get("title", ""),
                "summary": summary,
                "payload": archive_result.get("payload", {}),
            }
            write_json(output_path, snapshot)
            return _success_result(
                request_id=request_id,
                provider="Raw Archive",
                output_path=output_path.relative_to(config.output_dir),
                sha256=sha256_file(output_path),
                record_count=complete_count,
            )

        return {
            "requestId": request_id,
            "status": "not-supported",
            "provider": "collector",
            "outputPath": None,
            "sha256": None,
            "recordCount": 0,
            "reason": f"unsupported request type: {request_type}",
        }
    except Exception as exc:
        return {
            "requestId": request_id,
            "status": "provider-error",
            "provider": "collector",
            "outputPath": None,
            "sha256": None,
            "recordCount": 0,
            "reason": f"{type(exc).__name__}: {exc}",
        }


def _success_result(
    *,
    request_id: str,
    provider: str,
    output_path: Path,
    sha256: str,
    record_count: int,
) -> dict[str, Any]:
    return {
        "requestId": request_id,
        "status": "success",
        "provider": provider,
        "outputPath": output_path.as_posix(),
        "sha256": sha256,
        "recordCount": record_count,
        "reason": "",
    }


def _unavailable_result(
    request_id: str,
    provider: str,
    provider_result: dict[str, Any],
) -> dict[str, Any]:
    missing = provider_result.get("missing_data") or []
    reason = ""
    if missing and isinstance(missing[0], dict):
        reason = str(missing[0].get("reason") or "")
    return {
        "requestId": request_id,
        "status": "unavailable",
        "provider": provider,
        "outputPath": None,
        "sha256": None,
        "recordCount": 0,
        "reason": reason or "provider data unavailable",
    }


def _parse_date(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ResearchAcquisitionError(f"{label} must be YYYY-MM-DD")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ResearchAcquisitionError(f"{label} must be YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one bounded NASDAQ Cafe research acquisition wave.")
    parser.add_argument("--date", required=True, help="Episode date YYYY-MM-DD")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        config = build_config(args.date, args.refresh)
        result = run_followup(config, args.request)
    except (ResearchAcquisitionError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "invalid-request", "errors": str(exc).splitlines()}, ensure_ascii=False, indent=2))
        return 2

    print(
        json.dumps(
            {
                "status": result["status"],
                "requestPath": str(result["request_path"]),
                "resultPath": str(result["result_path"]),
                "manifestPath": str(result["manifest_path"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
