from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from post_dce_scope_gate import evaluate_post_dce_scope


# The inbox is a work queue for GPT Web, not a verdict store.
# Final/decided opportunities live in control/final_supergreen_bank.json.
VISIBLE_REVIEW_SIGNALS = {'FINAL_SUPER_GREEN', 'GREEN', 'GREEN_PARTNERABLE'}
MIN_PERSISTENT_INBOX_ITEMS = 160


def load_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        raw = path.read_text(encoding='utf-8', errors='replace').strip()
        obj = json.loads(raw) if raw else {}
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def load_live_current_inbox(path: Path) -> dict[str, Any]:
    """Prefer the current canonical main-branch inbox over a stale shard snapshot."""
    repo = str(os.getenv('GITHUB_REPOSITORY') or '').strip()
    token = str(os.getenv('GH_TOKEN') or os.getenv('GITHUB_TOKEN') or '').strip()
    if repo and token:
        ref = str(os.getenv('GPT_INBOX_LIVE_REF') or 'main').strip() or 'main'
        url = f'https://api.github.com/repos/{repo}/contents/control/gpt_supergreen_inbox.json?ref={ref}'
        req = urllib.request.Request(
            url,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github.raw+json',
                'X-GitHub-Api-Version': '2022-11-28',
                'User-Agent': 'tender-gpt-inbox/1.2',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode('utf-8', errors='replace').strip()
            obj = json.loads(raw) if raw else {}
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return load_json(path)


def load_live_review_ledger(path: Path) -> dict[str, Any]:
    """Prefer the current main-branch ledger when running inside Actions."""
    repo = str(os.getenv('GITHUB_REPOSITORY') or '').strip()
    token = str(os.getenv('GH_TOKEN') or os.getenv('GITHUB_TOKEN') or '').strip()
    if repo and token:
        url = f'https://api.github.com/repos/{repo}/contents/control/gpt_web_review_ledger.json?ref=main'
        req = urllib.request.Request(
            url,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github.raw+json',
                'X-GitHub-Api-Version': '2022-11-28',
                'User-Agent': 'tender-gpt-inbox/1.1',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode('utf-8', errors='replace').strip()
            obj = json.loads(raw) if raw else {}
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return load_json(path)


def key(row: dict[str, Any]) -> str:
    return str(row.get('candidate_id') or '').strip().casefold()


def open_deadline(row: dict[str, Any]) -> bool:
    raw = str(row.get('deadline') or '').strip()
    if not raw:
        return True
    try:
        return date.fromisoformat(raw[:10]) >= datetime.now(timezone.utc).date()
    except Exception:
        return True


def run_id(value: Any) -> int | None:
    s = str(value or '').strip()
    return int(s) if s.isdigit() else None


def source_run(row: dict[str, Any]) -> int | None:
    for name in ('source_dce_run_id', 'latest_dce_run_id', 'source_run', 'dce_run_id'):
        out = run_id(row.get(name))
        if out is not None:
            return out
    return None


def review_key(row: dict[str, Any]) -> str:
    stable = {
        'candidate_id': str(row.get('candidate_id') or ''),
        'source_dce_run_id': source_run(row),
        'title': str(row.get('title') or ''),
        'deadline': str(row.get('deadline') or ''),
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def ledger_ticks(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = ledger.get('ticks')
    if isinstance(raw, dict):
        return {str(k).casefold(): v for k, v in raw.items() if isinstance(v, dict)}
    if isinstance(raw, list):
        out: dict[str, dict[str, Any]] = {}
        for row in raw:
            if isinstance(row, dict) and key(row):
                out[key(row)] = row
        return out
    return {}


def is_reviewed(row: dict[str, Any], ticks: dict[str, dict[str, Any]]) -> bool:
    tick = ticks.get(key(row))
    if not tick or not bool(tick.get('reviewed', True)):
        return False
    exact = str(tick.get('review_key') or '').strip()
    if exact and exact == review_key(row):
        return True
    current_run = source_run(row)
    reviewed_run = run_id(tick.get('source_dce_run_id'))
    if current_run is not None and reviewed_run is not None:
        return current_run <= reviewed_run
    # Legacy ticks without a source DCE run cannot prove they cover a later DCE
    # version. Only an exact review_key may suppress in that case.
    return False


def pending_rank(row: dict[str, Any]):
    signal = str(row.get('upstream_signal') or row.get('classification') or '').upper()
    signal_rank = 3 if signal in VISIBLE_REVIEW_SIGNALS else 2 if str(row.get('review_stage') or '').upper() == 'BUSINESS_GATES' else 1
    return (
        signal_rank,
        int(row.get('gpt_priority_score') or row.get('spm_post_dce_score') or row.get('priority_score') or 0),
        int(bool(row.get('deadline_resolved'))),
        int(row.get('evidence_gate_coverage') or 0),
        source_run(row) or 0,
    )


def _stage(row: dict[str, Any]) -> str:
    return str(row.get('review_stage') or 'BUSINESS_GATES').upper()


def _band(row: dict[str, Any]) -> str:
    return str(row.get('spm_fit_band') or 'UNKNOWN').upper()


def _portal(row: dict[str, Any]) -> str:
    return str(row.get('portal') or row.get('source') or 'UNKNOWN').upper()


def _visible_priority(row: dict[str, Any]) -> bool:
    signal = str(row.get('upstream_signal') or row.get('classification') or '').upper()
    return signal in VISIBLE_REVIEW_SIGNALS


def _diverse_pick(rows: list[dict[str, Any]], n: int, chosen: set[str], lane: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        k = key(row)
        if k and k not in chosen:
            groups[_portal(row)].append(row)
    for portal in groups:
        groups[portal].sort(key=lambda r: (pending_rank(r), hashlib.sha256(key(r).encode()).hexdigest()), reverse=True)
    portals = sorted(groups, key=lambda p: (-len(groups[p]), p))
    out = []
    while portals and len(out) < n:
        next_portals = []
        for portal in portals:
            bucket = groups[portal]
            while bucket and key(bucket[0]) in chosen:
                bucket.pop(0)
            if not bucket:
                continue
            row = dict(bucket.pop(0))
            chosen.add(key(row))
            row['upstream_review_lane'] = row.get('review_lane')
            row['inbox_lane'] = lane
            out.append(row)
            if bucket:
                next_portals.append(portal)
            if len(out) >= n:
                break
        portals = next_portals
    return out


def select_inbox_window(rows: list[dict[str, Any]], max_items: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Non-destructive visible-window selection.

    75% priority + 12.5% authenticity repair + 12.5% portal-diverse LOW
    surveillance. The full uncapped backlog remains in gpt_review_index.json.
    """
    if max_items <= 0:
        return [], {}
    auth_n = max(1, int(round(max_items * 0.125)))
    low_n = max(1, int(round(max_items * 0.125)))
    priority_n = max(0, max_items - auth_n - low_n)
    ordered = sorted(rows, key=pending_rank, reverse=True)
    chosen: set[str] = set()
    selected: list[dict[str, Any]] = []

    priority_pool = [r for r in ordered if _visible_priority(r) or (_stage(r) != 'DCE_AUTHENTICITY' and _band(r) != 'LOW')]
    for row in priority_pool:
        if len(selected) >= priority_n:
            break
        k = key(row)
        if not k or k in chosen:
            continue
        chosen.add(k)
        out = dict(row)
        out['upstream_review_lane'] = out.get('review_lane')
        out['inbox_lane'] = 'GPT_PRIORITY'
        selected.append(out)

    auth_pool = [r for r in ordered if _stage(r) == 'DCE_AUTHENTICITY']
    selected.extend(_diverse_pick(auth_pool, auth_n, chosen, 'DCE_AUTHENTICITY_REPAIR'))

    low_pool = [r for r in ordered if _band(r) == 'LOW' and _stage(r) != 'DCE_AUTHENTICITY']
    selected.extend(_diverse_pick(low_pool, low_n, chosen, 'LOW_PORTAL_SURVEILLANCE'))

    if len(selected) < max_items:
        for row in ordered:
            if len(selected) >= max_items:
                break
            k = key(row)
            if not k or k in chosen:
                continue
            chosen.add(k)
            out = dict(row)
            out['upstream_review_lane'] = out.get('review_lane')
            out['inbox_lane'] = 'ELASTIC_FILL'
            selected.append(out)

    metrics = {
        'priority_target': priority_n,
        'authenticity_target': auth_n,
        'low_surveillance_target': low_n,
        'selected_by_lane': dict(Counter(str(r.get('inbox_lane') or 'UNKNOWN') for r in selected)),
        'selected_by_stage': dict(Counter(_stage(r) for r in selected)),
        'selected_by_band': dict(Counter(_band(r) for r in selected)),
        'selected_by_portal': dict(Counter(_portal(r) for r in selected)),
    }
    return selected, metrics


def normalize_hot_review(row: dict[str, Any], source_name: str = 'control/gpt_review_hot.json') -> dict[str, Any] | None:
    """Normalize anything deliberately placed on the persistent GPT review surface."""
    if not isinstance(row, dict) or not key(row) or not open_deadline(row):
        return None

    stage = str(row.get('review_stage') or '').upper()
    gate_ready = bool(row.get('gate_readiness'))
    coverage = int(row.get('evidence_gate_coverage') or 0)
    authenticity = stage == 'DCE_AUTHENTICITY' or bool(row.get('dce_authenticity_review_required'))
    if authenticity:
        if not row.get('evidence_quality_summary') and not row.get('content_quality'):
            return None
    elif not gate_ready or coverage <= 0:
        return None
    elif evaluate_post_dce_scope(row).get('auto_reject'):
        return None

    score = int(row.get('spm_post_dce_score') or row.get('priority_score') or 0)
    out = dict(row)
    out['review_state'] = 'PENDING_GPT_WEB'
    out['review_tick'] = False
    out['review_key'] = review_key(out)
    out['recommended_gpt_action'] = 'GPT_WEB_VERIFY_DCE_AUTHENTICITY' if authenticity else 'GPT_WEB_REVIEW_NOW'
    out['gpt_priority_score'] = max(1, min(100, score if score else int(out.get('priority_score') or 1)))
    out['upstream_signal'] = str(out.get('upstream_signal') or out.get('upstream_classification') or out.get('classification') or ('DCE_AUTHENTICITY_REVIEW' if authenticity else 'DCE_REVIEW_READY')).upper()
    out['finality'] = 'NOT_A_VERDICT_GPT_WEB_MUST_REVIEW'
    out['inbox_live_source'] = source_name
    locator = out.get('artifact_locator') if isinstance(out.get('artifact_locator'), dict) else {}
    out['evidence_locator'] = {
        'dce_run_id': source_run(out) or locator.get('dce_run_id'),
        'shard': out.get('source_shard') if out.get('source_shard') is not None else locator.get('shard'),
        'candidate_id': out.get('candidate_id'),
        'release_tag': locator.get('release_tag'),
    }
    return out


def normalize_hot_green(row: dict[str, Any]) -> dict[str, Any] | None:
    """Auto/guarded GREEN is only a high-priority GPT Web input."""
    if not isinstance(row, dict) or not key(row) or not open_deadline(row):
        return None
    out = dict(row)
    out['review_state'] = 'PENDING_GPT_WEB'
    out['review_tick'] = False
    out['review_key'] = review_key(out)
    out['upstream_signal'] = str(out.get('classification') or 'AUTO_GREEN').upper()
    out['recommended_gpt_action'] = 'GPT_WEB_REVIEW_NOW'
    out['gpt_priority_score'] = max(70, min(100, int(out.get('final_score') or 0)))
    out['finality'] = 'UPSTREAM_GUARDED_SIGNAL_NOT_A_GPT_WEB_VERDICT'
    out['inbox_live_source'] = 'control/supergreen_hot.json'
    out['review_lane'] = 'GUARDED_GREEN_PRIORITY'
    out['evidence_locator'] = {
        'dce_run_id': source_run(out),
        'shard': out.get('source_shard'),
        'candidate_id': out.get('candidate_id'),
    }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description='Build the persistent queue of DCE candidates GPT Web should review.')
    ap.add_argument('--existing', default='control/gpt_supergreen_inbox.json')
    ap.add_argument('--final-bank', default='control/final_supergreen_bank.json')
    ap.add_argument('--hot-green', default='control/supergreen_hot.json')
    ap.add_argument('--hot-review', default='control/gpt_review_hot.json')
    ap.add_argument('--review-ledger', default='control/gpt_web_review_ledger.json')
    ap.add_argument('--out', default='control/gpt_supergreen_inbox.json')
    ap.add_argument('--max-items', type=int, default=MIN_PERSISTENT_INBOX_ITEMS)
    args = ap.parse_args()

    existing = load_live_current_inbox(Path(args.existing))
    final_bank = load_json(Path(args.final_bank))
    hot_green = load_json(Path(args.hot_green))
    hot_review = load_json(Path(args.hot_review))
    ledger = load_live_review_ledger(Path(args.review_ledger))
    ticks = ledger_ticks(ledger)

    # YELLOW is a repair/review state, not a terminal resolution.
    final_items = [
        x for x in final_bank.get('items', [])
        if isinstance(x, dict) and key(x)
        and str(x.get('classification') or '').upper() in {'FINAL_SUPER_GREEN', 'GREEN', 'RED'}
    ]
    resolved = {key(x) for x in final_items}

    pending: dict[str, dict[str, Any]] = {}
    filtered_reviewed = 0
    filtered_resolved = 0

    def consider(row: dict[str, Any] | None) -> None:
        nonlocal filtered_reviewed, filtered_resolved
        if row is None:
            return
        k = key(row)
        if k in resolved:
            filtered_resolved += 1
            return
        if is_reviewed(row, ticks):
            filtered_reviewed += 1
            return
        cur = pending.get(k)
        if cur is None or pending_rank(row) >= pending_rank(cur):
            pending[k] = row

    existing_rows = existing.get('review_queue')
    if not isinstance(existing_rows, list):
        existing_rows = existing.get('pending_final_review') or []
    for raw in existing_rows:
        if isinstance(raw, dict):
            consider(normalize_hot_review(raw, 'existing_inbox'))

    for raw in hot_review.get('items', []) or []:
        if isinstance(raw, dict):
            consider(normalize_hot_review(raw))

    for bucket in ('final_supergreens', 'greens'):
        for raw in hot_green.get(bucket, []) or []:
            if isinstance(raw, dict):
                consider(normalize_hot_green(raw))

    max_items = max(MIN_PERSISTENT_INBOX_ITEMS, int(args.max_items or 0))
    rows, window_selection = select_inbox_window(list(pending.values()), max_items)

    runs: list[int] = []
    for value in (hot_review.get('latest_dce_run_id'), hot_green.get('latest_dce_run_id')):
        n = run_id(value)
        if n is not None:
            runs.append(n)
    for row in rows:
        n = source_run(row)
        if n is not None:
            runs.append(n)

    stage_counts = dict(Counter(_stage(row) for row in rows))
    payload = {
        'schema': 'GPT_WEB_REVIEW_INBOX_V3_FAIR_SHARE',
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'latest_source_dce_run_id': max(runs) if runs else existing.get('latest_source_dce_run_id'),
        'review_queue': rows,
        'pending_final_review': rows,
        'window_selection': window_selection,
        'counts': {
            'pending_gpt_web_review': len(rows),
            'reviewed_ticks_total': len(ticks),
            'excluded_already_reviewed': filtered_reviewed,
            'excluded_already_in_final_bank': filtered_resolved,
            'pending_before_visible_cap': len(pending),
            'pending_by_stage': stage_counts,
            'pending_by_inbox_lane': dict(Counter(str(row.get('inbox_lane') or 'UNKNOWN') for row in rows)),
            'pending_by_fit_band': dict(Counter(_band(row) for row in rows)),
            'pending_by_portal': dict(Counter(_portal(row) for row in rows)),
        },
        'review_contract': 'This file is only the visible GPT Web work window. It uses 75% priority, 12.5% DCE-authenticity repair, and 12.5% portal-diverse LOW surveillance with elastic fill. The durable uncapped backlog remains control/gpt_review_index.json; no candidate is deleted by this visible cap.',
        'tick_contract': 'After GPT Web reviews a pass, add one durable reviewed tick per candidate to control/gpt_web_review_ledger.json. The next rebuild removes ticked rows automatically. A later DCE run may re-enter review.',
        'final_bank_contract': 'Decided GREEN/YELLOW/RED/FINAL_SUPER_GREEN results belong in control/final_supergreen_bank.json, never mixed into this inbox.',
        'pipeline_contract': 'harvest -> rich-context Qwen triage -> DCE/evidence with document provenance -> deterministic post-DCE scope gate -> fair-share GPT Web inbox -> GPT Web review + tick -> final bank.',
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(payload['counts'], indent=2))


if __name__ == '__main__':
    main()
