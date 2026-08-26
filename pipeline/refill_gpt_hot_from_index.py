from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from publish_supergreen_hot import compact_review, deadline_open, ensure_review_rank, item_key, review_sort
from rebuild_gpt_review_bank_from_release import load_json, load_jsonl, run_and_shard
from refresh_gpt_inbox_live import is_reviewed, key, ledger_ticks


def stage(row: dict[str, Any]) -> str:
    return str(row.get("review_stage") or "BUSINESS_GATES").upper()


def band(row: dict[str, Any]) -> str:
    return str(row.get("spm_fit_band") or "UNKNOWN").upper()


def portal(row: dict[str, Any]) -> str:
    return str(row.get("portal") or row.get("source") or "UNKNOWN").upper()


def diverse_pick(rows: list[dict[str, Any]], n: int, chosen: set[str]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        ck = key(row)
        if ck and ck not in chosen:
            groups[portal(row)].append(row)
    portals = sorted(groups, key=lambda p: (-len(groups[p]), p))
    out: list[dict[str, Any]] = []
    while portals and len(out) < n:
        nxt: list[str] = []
        for p in portals:
            bucket = groups[p]
            while bucket and key(bucket[0]) in chosen:
                bucket.pop(0)
            if not bucket:
                continue
            row = bucket.pop(0)
            chosen.add(key(row))
            out.append(row)
            if bucket:
                nxt.append(p)
            if len(out) >= n:
                break
        portals = nxt
    return out


def select(rows: list[dict[str, Any]], max_items: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    auth_n = max(1, int(round(max_items * 0.125)))
    low_n = max(1, int(round(max_items * 0.125)))
    priority_n = max(0, max_items - auth_n - low_n)
    chosen: set[str] = set()
    selected: list[dict[str, Any]] = []

    priority = [r for r in rows if stage(r) != "DCE_AUTHENTICITY" and band(r) != "LOW"]
    for row in priority[:priority_n]:
        ck = key(row)
        if ck and ck not in chosen:
            chosen.add(ck)
            selected.append(dict(row, review_lane="SPM_PRIORITY"))

    auth = diverse_pick([r for r in rows if stage(r) == "DCE_AUTHENTICITY"], auth_n, chosen)
    selected.extend(dict(r, review_lane="DCE_AUTHENTICITY_REPAIR") for r in auth)

    low = diverse_pick([r for r in rows if band(r) == "LOW" and stage(r) != "DCE_AUTHENTICITY"], low_n, chosen)
    selected.extend(dict(r, review_lane="LOW_PORTAL_SURVEILLANCE") for r in low)

    if len(selected) < max_items:
        for row in rows:
            if len(selected) >= max_items:
                break
            ck = key(row)
            if not ck or ck in chosen:
                continue
            chosen.add(ck)
            selected.append(dict(row, review_lane="ELASTIC_FILL"))

    metrics = {
        "priority_target": priority_n,
        "authenticity_target": auth_n,
        "low_surveillance_target": low_n,
        "selected_by_lane": dict(Counter(str(r.get("review_lane") or "UNKNOWN") for r in selected)),
        "selected_by_stage": dict(Counter(stage(r) for r in selected)),
        "selected_by_band": dict(Counter(band(r) for r in selected)),
        "selected_by_portal": dict(Counter(portal(r) for r in selected)),
    }
    return selected, metrics


def run_id(row: dict[str, Any]) -> int:
    value = row.get("source_dce_run_id")
    if not str(value or "").isdigit():
        locator = row.get("artifact_locator") if isinstance(row.get("artifact_locator"), dict) else {}
        value = locator.get("dce_run_id")
    return int(value) if str(value or "").isdigit() else 0


def download_runs(rows: list[dict[str, Any]], root: Path, limit: int) -> list[int]:
    wanted: list[int] = []
    seen: set[int] = set()
    for row in rows:
        rid = run_id(row)
        if rid and rid not in seen:
            seen.add(rid)
            wanted.append(rid)
    downloaded: list[int] = []
    for rid in wanted[: max(0, limit)]:
        out = root / str(rid)
        out.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["gh", "release", "download", f"dce-harvest-{rid}", "--pattern", "fast-adjudication-shard-*.jsonl", "--dir", str(out)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            env=os.environ.copy(),
        )
        if proc.returncode == 0 and any(out.glob("fast-adjudication-shard-*.jsonl")):
            downloaded.append(rid)
    return downloaded


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="control/gpt_review_index.json")
    ap.add_argument("--final-bank", default="control/final_supergreen_bank.json")
    ap.add_argument("--review-ledger", default="control/gpt_web_review_ledger.json")
    ap.add_argument("--out", default="control/gpt_review_hot.json")
    ap.add_argument("--recovery-root", default="/tmp/gpt-index-refill")
    ap.add_argument("--max-items", type=int, default=160)
    ap.add_argument("--max-release-downloads", type=int, default=80)
    args = ap.parse_args()

    index = load_json(Path(args.index), {})
    final_bank = load_json(Path(args.final_bank), {})
    ledger = load_json(Path(args.review_ledger), {})
    ticks = ledger_ticks(ledger)
    # Repairable YELLOW rows must remain eligible for a newer DCE/review pass.
    final_ids = {
        key(x) for x in (final_bank.get("items") or [])
        if isinstance(x, dict) and key(x)
        and str(x.get("classification") or "").upper() in {"FINAL_SUPER_GREEN", "GREEN", "RED"}
    }

    rows = []
    for row in index.get("items") or []:
        if not isinstance(row, dict) or not key(row) or not deadline_open(row):
            continue
        if key(row) in final_ids or is_reviewed(row, ticks):
            continue
        rows.append(row)
    rows.sort(key=lambda r: (review_sort(r), run_id(r)), reverse=True)

    max_items = max(1, int(args.max_items))
    selected, selection_metrics = select(rows, max_items)
    selected_by_id = {key(r): r for r in selected}

    recovery_root = Path(args.recovery_root)
    shutil.rmtree(recovery_root, ignore_errors=True)
    recovery_root.mkdir(parents=True, exist_ok=True)
    downloaded = download_runs(selected, recovery_root, int(args.max_release_downloads))

    hydrated: dict[str, dict[str, Any]] = {}
    for path in sorted(recovery_root.rglob("fast-adjudication-shard-*.jsonl")):
        rid, shard = run_and_shard(path)
        for raw in load_jsonl(path):
            ck = key(raw)
            meta = selected_by_id.get(ck)
            if not meta or rid != run_id(meta):
                continue
            compact = compact_review(raw, str(rid), str(shard), datetime.now(timezone.utc).isoformat())
            compact["review_stage"] = meta.get("review_stage") or "BUSINESS_GATES"
            compact["dce_authenticity_review_required"] = stage(meta) == "DCE_AUTHENTICITY"
            compact["finalization_allowed"] = False
            compact["artifact_locator"] = {
                "dce_run_id": rid,
                "shard": shard,
                "candidate_id": raw.get("candidate_id"),
                "release_tag": f"dce-harvest-{rid}",
            }
            compact["evidence_quality_summary"] = raw.get("evidence_quality") or {}
            compact["evidence_provenance_summary"] = raw.get("evidence_provenance_summary") or {}
            compact["review_lane"] = meta.get("review_lane")
            compact = ensure_review_rank(compact)
            hydrated[ck] = compact

    items: list[dict[str, Any]] = []
    for meta in selected:
        ck = key(meta)
        if ck in hydrated:
            items.append(hydrated[ck])
            continue
        fallback = dict(meta)
        fallback["review_lane"] = meta.get("review_lane")
        fallback["finalization_allowed"] = False
        items.append(fallback)

    payload = {
        "schema": "GPT_REVIEW_HOT_V8_INDEX_REFILL",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "latest_dce_run_id": max((run_id(r) for r in items), default=0) or None,
        "generation_reset": False,
        "count": len(items),
        "items": items,
        "window_selection": selection_metrics,
        "refill_metrics": {
            "uncapped_index_pending_seen": len(rows),
            "selected": len(selected),
            "hydrated_from_exact_release": len(hydrated),
            "fallback_compact_only": len(items) - len(hydrated),
            "release_runs_requested": len({run_id(r) for r in selected if run_id(r)}),
            "release_runs_downloaded": len(downloaded),
        },
        "ranking_contract": "Refill from the durable uncapped GPT review index. Final-bank and reviewed-ledger rows are excluded before the visible cap. Fair-share remains 75% non-LOW business priority, 12.5% DCE authenticity repair, 12.5% LOW surveillance, then elastic fill.",
        "instruction": "GPT Web is the assistant. Adjudicate these rows, follow artifact_locator when compact evidence is insufficient, persist verdicts, then allow the next index refill. Unknown is never PASS.",
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"count": len(items), "refill_metrics": payload["refill_metrics"], "window_selection": selection_metrics}, indent=2))


if __name__ == "__main__":
    main()
