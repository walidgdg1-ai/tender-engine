#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / 'control/qwen_live/classification_summary.json'
DESIRED = ROOT / 'control/desired_state.json'
QWEN_STATE_MAX_AGE_HOURS = 6


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _parse_ts(value: object) -> datetime | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        if raw.endswith('Z'):
            raw = raw[:-1] + '+00:00'
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def qwen_health(summary: dict) -> dict:
    now = datetime.now(timezone.utc)
    generated = _parse_ts(summary.get('generated_at'))
    age_hours = None if generated is None else max(0.0, (now - generated).total_seconds() / 3600.0)
    seen = max(0, int(summary.get('result_rows_seen') or 0))
    rejected = summary.get('rejected_result_reasons') or {}
    parse_rejected = max(0, int(rejected.get('parse_or_fallback_error') or 0))
    stale = generated is None or (age_hours is not None and age_hours > QWEN_STATE_MAX_AGE_HOURS)
    all_recent_results_invalid = seen > 0 and parse_rejected >= seen
    healthy_for_throttling = not stale and not all_recent_results_invalid
    reasons=[]
    if stale:
        reasons.append('classification_summary_stale')
    if all_recent_results_invalid:
        reasons.append('last_merge_100pct_parse_or_fallback_error')
    return {
        'generated_at': summary.get('generated_at'),
        'age_hours': None if age_hours is None else round(age_hours, 3),
        'result_rows_seen': seen,
        'parse_or_fallback_rejected': parse_rejected,
        'stale': stale,
        'all_recent_results_invalid': all_recent_results_invalid,
        'healthy_for_throttling': healthy_for_throttling,
        'reasons': reasons,
    }


def policy(remaining: int) -> tuple[int, int, str]:
    if remaining >= 50_000:
        return 4, 2, 'qwen_backfill_guarded_thin_dce'
    if remaining >= 20_000:
        return 20, 4, 'qwen_backfill_dce_low'
    if remaining >= 5_000:
        return 80, 8, 'qwen_backfill_dce_medium'
    return 320, 20, 'deep_queue_streaming_hotpath'


def main() -> None:
    summary = load(SUMMARY)
    desired = load(DESIRED)
    reported_remaining = max(0, int(summary.get('remaining_classification_queue') or 0))
    health = qwen_health(summary)

    # Qwen classification is a prioritisation aid, never a mandatory eligibility
    # gate. A stale or demonstrably invalid classifier checkpoint must therefore
    # fail OPEN for DCE throughput instead of pinning the procurement engine to a
    # tiny DCE lane indefinitely.
    effective_remaining = reported_remaining if health['healthy_for_throttling'] else 0
    max_candidates, expected_slots, mode = policy(effective_remaining)

    dce = desired.setdefault('dce', {})
    latency = dce.setdefault('latency_policy', {})
    benchmark = dce.setdefault('benchmark_basis', {})
    changed = False

    updates = {
        'max_candidates_per_cycle': max_candidates,
    }
    for key, value in updates.items():
        if dce.get(key) != value:
            dce[key] = value
            changed = True

    latency_updates = {
        'target_candidates_per_batch': max_candidates,
        'expected_github_parallel_slots': expected_slots,
        'mode': mode,
        'goal': (
            'prioritize Qwen semantic backfill while preserving a bounded continuous DCE lane; automatically restore deeper DCE throughput as classification backlog falls'
            if effective_remaining >= 5_000
            else 'maximize guarded or ChatGPT-ready supergreen candidates per runner-minute; stale/broken Qwen state is never allowed to throttle authoritative DCE retrieval'
        ),
    }
    for key, value in latency_updates.items():
        if latency.get(key) != value:
            latency[key] = value
            changed = True

    decision = (
        f'auto_qwen_backlog_{reported_remaining}_effective_{effective_remaining}_dce_candidates_{max_candidates}'
    )
    if benchmark.get('decision') != decision:
        benchmark['decision'] = decision
        changed = True
    qwen_guard = {
        'reported_remaining': reported_remaining,
        'effective_remaining_for_dce_throttle': effective_remaining,
        'state_max_age_hours': QWEN_STATE_MAX_AGE_HOURS,
        **health,
        'rule': 'Qwen is a prioritisation aid. Stale or 100%-invalid Qwen checkpoints fail open for DCE throughput.',
    }
    if benchmark.get('qwen_throttle_health') != qwen_guard:
        benchmark['qwen_throttle_health'] = qwen_guard
        changed = True

    if changed:
        DESIRED.write_text(json.dumps(desired, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    print(json.dumps({
        'reported_remaining_classification_queue': reported_remaining,
        'effective_remaining_for_dce_throttle': effective_remaining,
        'qwen_health': health,
        'max_candidates_per_cycle': max_candidates,
        'expected_github_parallel_slots': expected_slots,
        'mode': mode,
        'changed': changed,
    }, indent=2))


if __name__ == '__main__':
    main()
