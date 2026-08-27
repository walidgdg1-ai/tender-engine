from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from build_matrix import SUPPORTED, candidate_has_public_route, resolve_dce_portal


CANDIDATE_ID = "candidate_id"


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def walk_candidate_dicts(obj: Any) -> Iterable[dict[str, Any]]:
    """Yield candidate-shaped dicts from repo control JSON without assuming one schema."""
    if isinstance(obj, dict):
        if str(obj.get(CANDIDATE_ID) or "").strip():
            yield obj
        for value in obj.values():
            if isinstance(value, (dict, list)):
                yield from walk_candidate_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            if isinstance(value, (dict, list)):
                yield from walk_candidate_dicts(value)


def norm_id(value: object) -> str:
    return str(value or "").strip().casefold()


def nonempty(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def richness(rec: dict[str, Any]) -> int:
    score = 0
    for key in ("resolver_portal", "portal", "portal_key", "selection_portal", "source"):
        if nonempty(rec.get(key)):
            score += 12
    if str(rec.get("notice_url") or "").startswith(("http://", "https://")):
        score += 30
    route = rec.get("route") if isinstance(rec.get("route"), dict) else {}
    score += min(40, 5 * sum(1 for v in route.values() if nonempty(v)))
    docs = rec.get("documents") if isinstance(rec.get("documents"), list) else []
    if docs:
        score += min(30, 3 * len(docs))
    for key in (
        "resource_id", "notice_id", "notice_ref", "portal_ref", "consultation_id",
        "documents_url", "document_landing_url", "ocid", "reference", "procedure_id",
    ):
        if nonempty(rec.get(key)):
            score += 8
    for key in ("title", "buyer", "deadline", "estimated_value", "currency"):
        if nonempty(rec.get(key)):
            score += 2
    return score


def merge_missing(base: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in other.items():
        if key == "route" and isinstance(value, dict):
            route = dict(out.get("route") or {}) if isinstance(out.get("route"), dict) else {}
            for rk, rv in value.items():
                if not nonempty(route.get(rk)) and nonempty(rv):
                    route[rk] = rv
            if route:
                out["route"] = route
            continue
        if key == "documents" and isinstance(value, list):
            existing = out.get("documents") if isinstance(out.get("documents"), list) else []
            if not existing and value:
                out["documents"] = value
            continue
        if not nonempty(out.get(key)) and nonempty(value):
            out[key] = value
    return out


def normalize_route(rec: dict[str, Any], requested_id: str) -> dict[str, Any]:
    out = dict(rec)
    out[CANDIDATE_ID] = requested_id
    route = dict(out.get("route") or {}) if isinstance(out.get("route"), dict) else {}
    upper = requested_id.upper()

    if upper.startswith("TED:"):
        notice = requested_id.split(":", 1)[1]
        out.setdefault("portal", "TED")
        out.setdefault("notice_url", f"https://ted.europa.eu/en/notice/-/detail/{notice}")
        route.setdefault("notice_id", notice)
    elif upper.startswith("IE:"):
        resource = requested_id.split(":", 1)[1]
        out["portal"] = "IRELAND_ETENDERS"
        out.setdefault("resource_id", resource)
        out.setdefault("notice_url", f"https://www.etenders.gov.ie/epps/cft/prepareViewCfTWS.do?resourceId={resource}")
        route.setdefault("resource_id", resource)
    elif upper.startswith("UK_PCS_OCDS:"):
        out.setdefault("portal", "UK_PCS_OCDS")
    elif upper.startswith("ZA_ETENDERS_OCDS:"):
        out.setdefault("portal", "ZA_ETENDERS_OCDS")
    elif upper.startswith("PL-BZP:") or upper.startswith("PL_BZP:"):
        out.setdefault("portal", "PL_BZP")
    elif upper.startswith("US-SAM:") or upper.startswith("US_SAM:"):
        out.setdefault("portal", "US_SAM")

    if route:
        out["route"] = route
    out["status"] = "DCE_PENDING"
    out["force_retry"] = True
    try:
        out["preliminary_score"] = min(89, max(0, int(out.get("preliminary_score") or out.get("priority_score") or 84)))
    except Exception:
        out["preliminary_score"] = 84
    out["selection_reason"] = "Fresh authoritative DCE repair for a live unresolved YELLOW; durable-attempt dedupe intentionally bypassed for this explicit repair pass."
    return out


def candidate_sources(repo: Path) -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = []
    fixed = [
        ("review_index", repo / "control/gpt_review_index.json"),
        ("final_bank", repo / "control/final_supergreen_bank.json"),
        ("review_hot", repo / "control/gpt_review_hot.json"),
        ("supergreen_inbox", repo / "control/gpt_supergreen_inbox.json"),
        ("live_review_batch", repo / "control/gpt_live_review_batch.json"),
        ("seen_candidates", repo / "state/seen_candidates.jsonl"),
        ("legacy_dce_queue", repo / "queues/dce_candidates.jsonl"),
    ]
    paths.extend((name, p) for name, p in fixed if p.exists())
    patterns = [
        ("gate_stress", "control/gpt_gate_stress_batches/batch-*.json"),
        ("web_batch", "control/gpt_web_read_batches/batch-*.json"),
        ("adjudication", "control/adjudications/*.json"),
        ("auto_selection", "queues/auto_selection_*.jsonl"),
    ]
    for name, pattern in patterns:
        for p in sorted(repo.glob(pattern)):
            paths.append((name, p))
    return paths


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", default="control/yellow_repair_request.json")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--out", default="generated/dce_candidates.jsonl")
    ap.add_argument("--summary", default="generated/yellow_repair_queue_summary.json")
    ap.add_argument("--allow-partial", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo)
    request = load_json(Path(args.request))
    if not isinstance(request, dict) or not isinstance(request.get("candidate_ids"), list):
        raise SystemExit("Repair request must contain candidate_ids[]")
    requested = [str(x).strip() for x in request["candidate_ids"] if str(x).strip()]
    requested = list(dict.fromkeys(requested))
    wanted = {norm_id(x): x for x in requested}

    variants: dict[str, list[tuple[int, str, dict[str, Any]]]] = {k: [] for k in wanted}
    source_hits: Counter[str] = Counter()
    files_scanned = 0
    records_scanned = 0

    for source_name, path in candidate_sources(repo):
        files_scanned += 1
        if path.suffix.lower() == ".jsonl":
            rows = iter_jsonl(path)
        else:
            rows = walk_candidate_dicts(load_json(path))
        for rec in rows:
            records_scanned += 1
            key = norm_id(rec.get(CANDIDATE_ID))
            if key not in wanted:
                continue
            variants[key].append((richness(rec), source_name, dict(rec)))
            source_hits[source_name] += 1

    output: list[dict[str, Any]] = []
    missing: list[str] = []
    unroutable: list[dict[str, Any]] = []
    recovered_from: dict[str, list[str]] = {}

    for key, requested_id in wanted.items():
        found = variants.get(key) or []
        if not found:
            missing.append(requested_id)
            continue
        found.sort(key=lambda x: x[0], reverse=True)
        rec = dict(found[0][2])
        for _, _, other in found[1:]:
            rec = merge_missing(rec, other)
        rec = normalize_route(rec, requested_id)
        resolver, raw_portal = resolve_dce_portal(rec)
        rec["resolver_portal"] = resolver
        rec["repair_source_records"] = len(found)
        rec["repair_source_kinds"] = sorted({name for _, name, _ in found})
        recovered_from[requested_id] = rec["repair_source_kinds"]

        if resolver not in SUPPORTED:
            unroutable.append({
                "candidate_id": requested_id,
                "resolver_portal": resolver,
                "raw_portal": raw_portal,
                "notice_url": rec.get("notice_url"),
                "source_kinds": rec["repair_source_kinds"],
            })
            continue
        # A recognized portal may use an ID-derived route (TED/IE). Other lanes need
        # either a public URL or source-specific route metadata. Fail closed if neither exists.
        id_routable = requested_id.upper().startswith(("TED:", "IE:"))
        route = rec.get("route") if isinstance(rec.get("route"), dict) else {}
        route_metadata = any(nonempty(v) for v in route.values()) or any(
            nonempty(rec.get(k)) for k in ("resource_id", "notice_id", "notice_ref", "portal_ref", "ocid", "reference")
        )
        if not (id_routable or candidate_has_public_route(rec) or route_metadata):
            unroutable.append({
                "candidate_id": requested_id,
                "resolver_portal": resolver,
                "raw_portal": raw_portal,
                "notice_url": rec.get("notice_url"),
                "source_kinds": rec["repair_source_kinds"],
                "reason": "recognized_portal_but_no_public_or_id_route",
            })
            continue
        output.append(rec)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for rec in output:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    resolver_counts = Counter(str(x.get("resolver_portal") or "") for x in output)
    payload = {
        "schema": "YELLOW_REPAIR_QUEUE_RECOVERY_V1",
        "requested": len(requested),
        "recovered_routable": len(output),
        "missing": missing,
        "missing_count": len(missing),
        "unroutable": unroutable,
        "unroutable_count": len(unroutable),
        "files_scanned": files_scanned,
        "records_scanned": records_scanned,
        "source_hit_counts": dict(source_hits),
        "resolver_counts": dict(resolver_counts),
        "recovered_from": recovered_from,
    }
    summary_path = Path(args.summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if (missing or unroutable) and not args.allow_partial:
        raise SystemExit(f"Incomplete YELLOW repair queue: missing={len(missing)} unroutable={len(unroutable)}")
    if not output:
        raise SystemExit("YELLOW repair queue is empty")


if __name__ == "__main__":
    main()
