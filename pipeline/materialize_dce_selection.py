from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from build_live_world_snapshot import make_row as build_snapshot_row
from build_notice_intelligence_ledger import digest, material_payload
from dce_attempt_ledger import load_attempt_index, was_attempted

QWEN_STATUS = "AUTO_DCE_PREFETCH_QWEN"
ALIAS_ID_FIELDS = (
    "candidate_id",
    "canonical_notice_id",
    "canonical_key",
    "ocid",
    "notice_id",
    "resource_id",
    "procedure_id",
    "reference",
    "id",
)


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{line_no}: expected JSON object")
            rows.append(obj)
    return rows


def norm(value: object) -> str:
    s = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def canonical_candidate_id(value: object) -> str:
    return str(value or "").strip().casefold()


def identity(rec: dict) -> tuple[str, tuple[str, str] | None]:
    cid = canonical_candidate_id(rec.get("candidate_id"))
    title = norm(rec.get("title"))
    buyer = norm(rec.get("buyer"))
    return cid, ((title, buyer) if title and buyer else None)


def _alias_values(rec: dict) -> set[str]:
    """Return all stable identities by which an upstream normalized notice may be known.

    The live snapshot/ledger can carry a process-level OCDS identity while the
    canonical discovery pack still stores a release-level candidate_id. DCE must
    be able to map both representations back to the same source record.
    """
    aliases: set[str] = set()
    source = str(rec.get("source") or rec.get("portal") or "").strip()
    source_norm = canonical_candidate_id(source)

    for field in ALIAS_ID_FIELDS:
        raw = str(rec.get(field) or "").strip()
        if not raw:
            continue
        aliases.add(canonical_candidate_id(raw))
        if source_norm and ":" not in raw:
            aliases.add(canonical_candidate_id(f"{source}:{raw}"))

    # OCDS process ids are especially important because some portal feeds use a
    # release id for candidate_id while semantic state is keyed by the ocid.
    ocid = str(rec.get("ocid") or "").strip()
    if source_norm and ocid:
        aliases.add(canonical_candidate_id(f"{source}:{ocid}"))
    return {x for x in aliases if x}


def build_candidate_index(candidates: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for rec in candidates:
        for alias in _alias_values(rec):
            bucket = out.setdefault(alias, [])
            if rec not in bucket:
                bucket.append(rec)
    return out


def candidate_material_hash(rec: dict) -> str:
    try:
        snapshot = build_snapshot_row(rec, datetime.now(timezone.utc))
        if not snapshot:
            return ""
        return digest(material_payload(snapshot))
    except Exception:
        return ""


def selection_material_hash(sel: dict) -> str:
    q = sel.get("qwen") if isinstance(sel.get("qwen"), dict) else {}
    return str(q.get("material_fields_hash") or sel.get("material_fields_hash") or "").strip().casefold()


def resolve_candidate(sel: dict, index: dict[str, list[dict]]) -> tuple[dict | None, str]:
    cid_key = canonical_candidate_id(sel.get("candidate_id"))
    matches = list(index.get(cid_key) or [])
    if not matches:
        return None, "missing"
    if len(matches) == 1:
        exact = canonical_candidate_id(matches[0].get("candidate_id")) == cid_key
        return matches[0], "exact_candidate_id" if exact else "source_alias"

    expected_hash = selection_material_hash(sel)
    if expected_hash:
        hashed = [rec for rec in matches if candidate_material_hash(rec).casefold() == expected_hash]
        if len(hashed) == 1:
            return hashed[0], "source_alias_material_hash"
        if hashed:
            matches = hashed

    # Deterministic last-resort tie-breaker is safe only when all alias matches are
    # semantically the same title/buyer/deadline. Otherwise fail closed as missing
    # instead of arbitrarily retrieving the wrong release revision.
    signatures = {
        (
            norm(rec.get("title")),
            norm(rec.get("buyer")),
            str(rec.get("deadline") or rec.get("deadline_utc") or "").strip(),
        )
        for rec in matches
    }
    if len(signatures) == 1:
        matches.sort(key=lambda r: canonical_candidate_id(r.get("candidate_id")))
        return matches[0], "source_alias_equivalent_revision"
    return None, "ambiguous_alias"


def load_selection(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("candidate_ids"), list):
            default_score = min(89, int(obj.get("default_preliminary_score", 84)))
            default_status = str(obj.get("status") or "DCE_PENDING")
            default_run = obj.get("wide_read_run_id")
            default_reason = obj.get("selection_reason")
            default_force_retry = bool(obj.get("force_retry"))
            out = []
            for cid in obj["candidate_ids"]:
                rec = {
                    "candidate_id": str(cid),
                    "preliminary_score": default_score,
                    "status": default_status,
                }
                if default_run is not None:
                    rec["wide_read_run_id"] = default_run
                if default_reason:
                    rec["selection_reason"] = default_reason
                if default_force_retry:
                    rec["force_retry"] = True
                out.append(rec)
            return out
    if path.suffix.lower() == ".json":
        obj = json.loads(text)
        if isinstance(obj, list):
            return [x if isinstance(x, dict) else {"candidate_id": str(x)} for x in obj]
    return load_jsonl(path)


def qwen_pre_admission(base: dict, sel: dict) -> tuple[bool, str]:
    if str(sel.get("status") or "").upper() != QWEN_STATUS:
        return True, "not_qwen_lane"
    return True, "qwen_selected_high_recall"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--selection", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--exclude-queue", default="")
    ap.add_argument("--attempt-ledger", default="control/dce_attempt_ledger.jsonl")
    args = ap.parse_args()

    candidates = load_jsonl(Path(args.candidates))
    selection = load_selection(Path(args.selection))
    by_id = build_candidate_index(candidates)

    exclude_rows = load_jsonl(Path(args.exclude_queue)) if args.exclude_queue else []
    excluded_ids: set[str] = set()
    excluded_tb: set[tuple[str, str]] = set()
    for rec in exclude_rows:
        cid, tb = identity(rec)
        if cid:
            excluded_ids.add(cid)
        if tb:
            excluded_tb.add(tb)

    attempt_index = load_attempt_index(Path(args.attempt_ledger)) if args.attempt_ledger else {}

    selected: list[dict] = []
    missing: list[str] = []
    ambiguous: list[str] = []
    duplicate_selection: list[str] = []
    excluded_existing: list[str] = []
    excluded_attempted: list[str] = []
    excluded_qwen_pre_admission: list[dict] = []
    qwen_admission_reasons: dict[str, int] = {}
    resolution_reasons: dict[str, int] = {}
    seen: set[str] = set()
    seen_tb: set[tuple[str, str]] = set()

    for sel in selection:
        cid = str(sel.get("candidate_id") or "").strip()
        cid_key = canonical_candidate_id(cid)
        if not cid_key:
            continue
        if cid_key in seen:
            duplicate_selection.append(cid)
            continue
        seen.add(cid_key)

        base, resolution_reason = resolve_candidate(sel, by_id)
        resolution_reasons[resolution_reason] = resolution_reasons.get(resolution_reason, 0) + 1
        if not base:
            if resolution_reason == "ambiguous_alias":
                ambiguous.append(cid)
            else:
                missing.append(cid)
            continue

        raw_candidate_id = str(base.get("candidate_id") or "").strip()
        candidate_for_attempt = dict(base)
        # The semantic/ledger id is now the cross-stage canonical id. Preserve the
        # raw discovery id separately for provenance and portal debugging.
        candidate_for_attempt["candidate_id"] = cid
        if raw_candidate_id and canonical_candidate_id(raw_candidate_id) != cid_key:
            candidate_for_attempt["source_candidate_id"] = raw_candidate_id
        candidate_for_attempt["selection_manifest_candidate_id"] = cid
        candidate_for_attempt["identity_resolution"] = resolution_reason

        for key in (
            "preliminary_score",
            "business_fit_score",
            "wide_read_run_id",
            "status",
            "selection_portal",
            "selection_reason",
            "selection_bucket",
            "selection_fit_class",
            "selection_freshness",
            "qwen",
            "force_retry",
        ):
            if key in sel:
                candidate_for_attempt[key] = sel[key]

        # Normal discovery remains deduplicated by the durable attempt ledger.
        # Explicit repair manifests may bypass it once to fetch a newer DCE version.
        force_retry = bool(sel.get("force_retry"))
        if not force_retry and (was_attempted(base, attempt_index) or was_attempted(candidate_for_attempt, attempt_index)):
            excluded_attempted.append(cid)
            continue
        if force_retry:
            candidate_for_attempt["force_retry"] = True

        base_cid_key, tb_key = identity(base)
        if base_cid_key in excluded_ids or cid_key in excluded_ids or (tb_key and tb_key in excluded_tb):
            excluded_existing.append(cid)
            continue
        if tb_key and tb_key in seen_tb:
            duplicate_selection.append(cid)
            continue

        allowed, admission_reason = qwen_pre_admission(base, sel)
        qwen_admission_reasons[admission_reason] = qwen_admission_reasons.get(admission_reason, 0) + 1
        if not allowed:
            excluded_qwen_pre_admission.append({"candidate_id": cid, "reason": admission_reason})
            continue

        if tb_key:
            seen_tb.add(tb_key)

        rec = candidate_for_attempt
        rec["qwen_pre_admission_reason"] = admission_reason
        if int(rec.get("preliminary_score") or 0) > 89:
            raise SystemExit(f"Pre-DCE score exceeds 89 for {cid}")
        selected.append(rec)

    accounted = (
        len(selected)
        + len(missing)
        + len(ambiguous)
        + len(excluded_existing)
        + len(excluded_attempted)
        + len(duplicate_selection)
        + len(excluded_qwen_pre_admission)
    )
    if accounted != len(selection):
        raise SystemExit(
            f"Coverage mismatch: selected={len(selected)} missing_snapshot={len(missing)} ambiguous_alias={len(ambiguous)} "
            f"existing={len(excluded_existing)} attempted={len(excluded_attempted)} "
            f"duplicates={len(duplicate_selection)} qwen_deferred={len(excluded_qwen_pre_admission)} "
            f"selection={len(selection)}"
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for rec in selected:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")

    summary = {
        "canonical_candidate_pool": len(candidates),
        "candidate_identity_aliases": len(by_id),
        "selection_manifest_records": len(selection),
        "missing_from_source_snapshot_count": len(missing),
        "missing_from_source_snapshot_candidate_ids": missing,
        "ambiguous_source_alias_count": len(ambiguous),
        "ambiguous_source_alias_candidate_ids": ambiguous,
        "identity_resolution_reasons": resolution_reasons,
        "excluded_as_existing_exact_identity": len(excluded_existing),
        "excluded_existing_candidate_ids": excluded_existing,
        "excluded_as_durable_attempt": len(excluded_attempted),
        "excluded_attempted_candidate_ids": excluded_attempted,
        "deduplicated_exact_identity_count": len(duplicate_selection),
        "deduplicated_candidate_ids": duplicate_selection,
        "qwen_pre_admission_deferred_count": len(excluded_qwen_pre_admission),
        "qwen_pre_admission_deferred": excluded_qwen_pre_admission,
        "qwen_pre_admission_reasons": qwen_admission_reasons,
        "materialized_queue_records": len(selected),
        "queue_exhausted": bool(selection) and not selected,
        "coverage_ok": accounted == len(selection),
        "max_preliminary_score": max((int(r.get("preliminary_score") or 0) for r in selected), default=0),
        "source_run_ids": sorted(
            {str(r.get("wide_read_run_id")) for r in selected if r.get("wide_read_run_id") is not None}
        ),
    }
    summary_path = out.with_suffix(out.suffix + ".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
