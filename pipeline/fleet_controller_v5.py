from __future__ import annotations

"""Broker-, yield-, historical-prior- and semantic-retry-aware Tender controller.

Historical Market Brain priors are strictly a bounded pre-DCE retrieval signal.
They cannot satisfy eligibility, DCE authority, or final verdict gates.

Critical state rule: ATTEMPTED is not PROCESSED. A candidate becomes durably
processed only after the aggregate proves a candidate-specific substantive DCE is
gate-ready. Retrieval misses remain retryable behind a bounded cooldown.
"""

import io
import json
import tarfile
from datetime import datetime, timedelta, timezone

import requests

import auto_select_dce as selector_mod
import dce_orphan_reconcile
import fleet_controller as fc
import historical_market_priors as historical_priors
from github_api_resilience import install as install_github_resilience

install_github_resilience(fc)

EVERGREEN_REPO = "walidgdg1-ai/evergreenleadminer"
BROKER_TAG = "global-fleet-broker"
BROKER_ASSET = "global-capacity.json"
PORTAL_PERFORMANCE_ASSET = "portal-performance.json"
DEFAULT_MINIMUMS = {"hospitality": 6, "gws": 3}
DISCOVERY_WORKFLOW = "supergreen-discovery-v2.yml"
ORPHAN_RECONCILE_INTERVAL_MINUTES = 30
RETRY_BASE_MINUTES = 5
RETRY_MAX_MINUTES = 360
MAX_RETRY_STATE = 100_000
TRUSTED_DCE_CONTENT_QUALITIES = {"SUBSTANTIVE_DCE_PRESENT", "MIXED_SUBSTANTIVE_AND_GUIDE"}

_HISTORICAL_PRIORS = historical_priors.load()
_ORIGINAL_RETRIEVAL_SCORE = selector_mod.retrieval_score


def _retrieval_score_with_historical_prior(rec: dict, portal_performance: dict | None = None):
    score, reasons = _ORIGINAL_RETRIEVAL_SCORE(rec, portal_performance=portal_performance)
    delta, hist_reasons = historical_priors.adjustment(rec, _HISTORICAL_PRIORS)
    if delta:
        score = max(-100, min(100, score + delta))
        reasons = list(reasons) + hist_reasons
    return score, reasons


selector_mod.retrieval_score = _retrieval_score_with_historical_prior


def _release_asset_json(repo: str, tag: str, name: str):
    try:
        rel = requests.get(
            f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "tender-fleet-broker/1.2"},
            timeout=20,
        )
        if rel.status_code != 200:
            return None
        asset = next((a for a in rel.json().get("assets") or [] if a.get("name") == name), None)
        if not asset:
            return None
        r = requests.get(
            asset["url"],
            headers={"Accept": "application/octet-stream", "User-Agent": "tender-fleet-broker/1.2"},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _policy():
    try:
        r = requests.get(
            f"https://raw.githubusercontent.com/{EVERGREEN_REPO}/main/config/global_fleet.json",
            headers={"User-Agent": "tender-fleet-broker/1.2"},
            timeout=20,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


def _sibling_missing_headroom(fleet_detail: list[dict]) -> tuple[int, dict]:
    policy = _policy()
    workloads = policy.get("workloads") or {}
    state = _release_asset_json(EVERGREEN_REPO, BROKER_TAG, BROKER_ASSET) or {}
    demand = ((state.get("last_decision") or {}).get("demand") or {})
    if not demand:
        demand = {"hospitality": 1, "gws": 0}
    active_evergreen = sum(
        int(x.get("active_jobs") or 0)
        for x in fleet_detail
        if str(x.get("repo") or "") == EVERGREEN_REPO
    )
    target = 0
    components = {}
    for name in ("hospitality", "gws"):
        cfg = workloads.get(name) or {}
        enabled = bool(cfg.get("enabled", True))
        backlog = int(demand.get(name, 0) or 0)
        floor = int(cfg.get("min_slots_when_demanding") or DEFAULT_MINIMUMS[name])
        need = min(backlog, floor) if enabled and backlog > 0 else 0
        components[name] = {"demand": backlog, "minimum": floor, "target": need}
        target += need
    missing = max(0, target - active_evergreen)
    return missing, {
        "at": datetime.now(timezone.utc).isoformat(),
        "active_evergreen_jobs": active_evergreen,
        "guaranteed_target": target,
        "missing_virtual_headroom": missing,
        "components": components,
        "broker_state_updated_at": state.get("updated_at"),
    }


_original_public_fleet_jobs = fc.public_fleet_jobs
_original_select = fc.select
_original_dispatch = fc.dispatch


def _broker_aware_public_fleet_jobs():
    active, queued, detail = _original_public_fleet_jobs()
    missing, decision = _sibling_missing_headroom(detail)
    if missing:
        detail = list(detail) + [{
            "repo": "virtual://global-capacity-broker",
            "active_jobs": missing,
            "queued_jobs": 0,
            "reason": "guaranteed sibling headroom not yet satisfied by live jobs",
            "decision": decision,
        }]
    print(json.dumps({"global_capacity_guard": decision}, separators=(",", ":")))
    return active + missing, queued, detail


def _yield_aware_select(records, minimum=34, limit=320, blocked_ids=None, **kwargs):
    performance = _release_asset_json(fc.REPO, fc.FLEET_TAG, PORTAL_PERFORMANCE_ASSET) or {}
    print(json.dumps({
        "portal_yield_scheduler": {
            "history_run_ids": performance.get("run_ids") or [],
            "observed_portals": len((performance.get("portals") or {})),
            "exploit_explore": "85/15",
            "historical_market_brain": "READY" if _HISTORICAL_PRIORS else "INACTIVE",
        }
    }, separators=(",", ":")))
    return _original_select(
        records, minimum=minimum, limit=limit, blocked_ids=blocked_ids,
        portal_performance=performance,
    )


def _delta_aware_dispatch(workflow: str, inputs: dict | None = None):
    if workflow == DISCOVERY_WORKFLOW:
        merged = dict(inputs or {})
        merged.setdefault("mode", "delta")
        print(json.dumps({"discovery_dispatch": {"workflow": workflow, "mode": merged["mode"]}}, separators=(",", ":")))
        return _original_dispatch(workflow, merged)
    return _original_dispatch(workflow, inputs)


fc.public_fleet_jobs = _broker_aware_public_fleet_jobs
fc.select = _yield_aware_select
fc.dispatch = _delta_aware_dispatch

import fleet_controller_v4 as v4  # noqa: E402,F401
import fleet_controller_v3 as v3  # noqa: E402

_original_reconcile_pending = v3._reconcile_pending


def _semantic_resolution(dce_run_id: str, executed_ids: list[str]) -> dict | None:
    """Read aggregate truth already persisted in the DCE Release.

    Permanent processing is fail-closed: gate_readiness must be true AND the
    evidence-quality classifier must identify substantive candidate-specific DCE
    content. A portal page, guide, notice or unknown retrieval remains retryable.
    """
    try:
        rel, assets = v4._release_assets(dce_run_id)
        if not rel:
            return None
        name = f"dce-deep-review-{dce_run_id}.tar.gz"
        if not any(str(a.get("name") or "") == name for a in assets):
            return None
        blob = fc.download_asset({**rel, "assets": assets}, name)
        if not blob:
            return None
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            member = next(
                (m for m in tf.getmembers() if m.isfile() and m.name.rstrip("/").endswith("deep_review_queue.jsonl")),
                None,
            )
            if member is None:
                return None
            fh = tf.extractfile(member)
            if fh is None:
                return None
            rows = []
            for raw in fh.read().decode("utf-8", errors="replace").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except Exception:
                    continue
                if isinstance(row, dict) and row.get("candidate_id"):
                    rows.append(row)
        by_id = {str(r.get("candidate_id")).casefold(): r for r in rows}
        resolved: list[str] = []
        retryable: list[str] = []
        retry_reasons: dict[str, str] = {}
        for cid in executed_ids:
            row = by_id.get(str(cid).casefold())
            summary = (row or {}).get("evidence_quality_summary") or {}
            quality = str((row or {}).get("content_quality") or summary.get("content_quality") or "").upper()
            trusted_quality = quality in TRUSTED_DCE_CONTENT_QUALITIES
            if row and bool(row.get("gate_readiness")) and trusted_quality:
                resolved.append(cid)
            else:
                retryable.append(cid)
                base_reason = str((row or {}).get("status") or (row or {}).get("raw_status") or "SEMANTIC_RESULT_MISSING")
                if row and bool(row.get("gate_readiness")) and not trusted_quality:
                    base_reason = f"UNTRUSTED_GATE_READY_CONTENT_QUALITY:{quality or 'MISSING'}"
                retry_reasons[cid] = base_reason
        return {
            "resolved_ids": resolved,
            "retryable_ids": retryable,
            "retry_reasons": retry_reasons,
            "semantic_rows": len(rows),
            "asset": name,
        }
    except Exception as exc:
        print(json.dumps({"semantic_resolution_error": {"run_id": dce_run_id, "error": repr(exc)[:500]}}, separators=(",", ":")))
        return None


def _expire_retry_cooldowns(state: dict, actions: list[dict]) -> None:
    retry_after = state.setdefault("dce_retry_after", {})
    attempts = state.setdefault("dce_retry_attempts", {})
    if not isinstance(retry_after, dict):
        retry_after = state["dce_retry_after"] = {}
    if not isinstance(attempts, dict):
        attempts = state["dce_retry_attempts"] = {}
    expired = []
    for cid, when in list(retry_after.items()):
        ts = fc.parse_ts(str(when or ""))
        if ts is None or fc.NOW >= ts:
            expired.append(str(cid))
            retry_after.pop(cid, None)
    if expired:
        expired_set = set(expired)
        state["processed_candidate_ids"] = [
            str(cid) for cid in state.get("processed_candidate_ids", []) if str(cid) not in expired_set
        ]
        actions.append({
            "type": "dce_retry_cooldown_expired",
            "candidates_released": len(expired),
            "sample": expired[:12],
        })
    if len(attempts) > MAX_RETRY_STATE:
        keep = set(list(attempts)[-MAX_RETRY_STATE:])
        state["dce_retry_attempts"] = {k: v for k, v in attempts.items() if k in keep}


def _semantic_commit_durable_batch(
    state: dict,
    actions: list[dict],
    pending: dict,
    leased_ids: list[str],
    run_id: str,
    executed_ids: list[str],
) -> bool:
    semantic = _semantic_resolution(run_id, executed_ids)
    if semantic is None:
        tries = int(pending.get("semantic_resolution_attempts") or 0) + 1
        pending["semantic_resolution_attempts"] = tries
        if tries < 4:
            state["pending_dce_batch"] = pending
            actions.append({
                "type": "dce_commit_waiting_for_semantic_resolution",
                "workflow_run_id": run_id,
                "executed_candidates": len(executed_ids),
                "verification_attempt": tries,
                "rule": "execution archive alone never makes a candidate processed",
            })
            return True
        actions.append({
            "type": "dce_batch_semantic_resolution_missing_requeued",
            "workflow_run_id": run_id,
            "executed_candidates": len(executed_ids),
        })
        state["pending_dce_batch"] = None
        if run_id and executed_ids:
            v4._dispatch_postprocessing(run_id, actions, state)
        return False

    resolved_ids = list(semantic["resolved_ids"])
    retryable_ids = list(semantic["retryable_ids"])
    retry_after = state.setdefault("dce_retry_after", {})
    attempts = state.setdefault("dce_retry_attempts", {})

    processed = list(state.get("processed_candidate_ids", []))
    processed.extend(resolved_ids)
    now = fc.NOW
    cooldowns = []
    for cid in retryable_ids:
        n = int(attempts.get(cid) or 0) + 1
        attempts[cid] = n
        minutes = min(RETRY_MAX_MINUTES, RETRY_BASE_MINUTES * (2 ** min(6, n - 1)))
        retry_after[cid] = (now + timedelta(minutes=minutes)).isoformat()
        processed.append(cid)
        cooldowns.append({"candidate_id": cid, "attempt": n, "minutes": minutes, "reason": semantic["retry_reasons"].get(cid)})
    for cid in resolved_ids:
        retry_after.pop(cid, None)
        attempts.pop(cid, None)

    state["processed_candidate_ids"] = v4._dedupe(processed)[-v3.MAX_REMEMBERED_CANDIDATES:]
    state["dce_retry_after"] = retry_after
    state["dce_retry_attempts"] = attempts

    executed_set = set(executed_ids)
    not_executed = [cid for cid in leased_ids if cid not in executed_set]
    actions.append({
        "type": "dce_batch_semantically_committed",
        "workflow_run_id": run_id,
        "source_run": pending.get("source_run"),
        "leased_candidates": len(leased_ids),
        "executed_candidates": len(executed_ids),
        "resolved_gate_ready_processed": len(resolved_ids),
        "retryable_executed_with_cooldown": len(retryable_ids),
        "not_executed_returned_immediately": len(not_executed),
        "execution_coverage": round(len(executed_ids) / max(1, len(leased_ids)), 6),
        "semantic_asset": semantic.get("asset"),
        "semantic_rows": semantic.get("semantic_rows"),
        "retry_sample": cooldowns[:12],
        "rule": "ATTEMPTED != PROCESSED; only trusted substantive candidate-specific gate-ready DCE becomes permanent",
    })
    state["pending_dce_batch"] = None
    if run_id and executed_ids:
        v4._dispatch_postprocessing(run_id, actions, state)
    return False


v4._commit_durable_batch = _semantic_commit_durable_batch


def _release_successful_zero_dce_lease(state: dict, actions: list[dict]) -> bool:
    pending = state.get("pending_dce_batch")
    if not isinstance(pending, dict) or not pending.get("candidate_ids"):
        return False
    run = v3._match_pending_run(pending)
    if not run:
        return False
    if str(run.get("status") or "") != "completed" or str(run.get("conclusion") or "") != "success":
        return False
    run_id = str(run.get("id") or "")
    if not run_id:
        return False

    release, _assets = v4._release_assets(run_id)
    if release:
        return False

    leased = [str(x) for x in pending.get("candidate_ids") or []]
    actions.append({
        "type": "dce_zero_execution_lease_released_immediately",
        "workflow_run_id": run_id,
        "source_run": pending.get("source_run"),
        "leased_candidates": len(leased),
        "reason": "successful DCE run has no canonical dce-harvest Release, proving zero materialized shard execution; all leased candidates returned to durable queue",
    })
    state["pending_dce_batch"] = None
    return True


def _reconcile_pending_with_orphans(state: dict, actions: list[dict]) -> bool:
    _expire_retry_cooldowns(state, actions)

    if _release_successful_zero_dce_lease(state, actions):
        return False

    last = fc.parse_ts(state.get("last_orphan_reconcile_at"))
    due = last is None or fc.NOW - last >= timedelta(minutes=ORPHAN_RECONCILE_INTERVAL_MINUTES)
    if due:
        dce_orphan_reconcile.reconcile(fc, state, actions, recent_limit=20)
        state["last_orphan_reconcile_at"] = fc.NOW.isoformat()
    else:
        actions.append({
            "type": "dce_orphan_reconcile_skipped_not_due",
            "last_at": state.get("last_orphan_reconcile_at"),
            "interval_minutes": ORPHAN_RECONCILE_INTERVAL_MINUTES,
        })
    return _original_reconcile_pending(state, actions)


v3._reconcile_pending = _reconcile_pending_with_orphans


if __name__ == "__main__":
    v3.main()
