from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load(path: str) -> dict[str, Any]:
    p = Path(path)
    try:
        obj = json.loads(p.read_text(encoding='utf-8'))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def norm(v: Any) -> str:
    return str(v or '').strip().upper()


def main() -> None:
    bank = load('control/final_supergreen_bank.json')
    ledger = load('control/gpt_web_review_ledger.json')
    index = load('control/gpt_review_index.json')
    metrics = load('control/gpt_inbox_metrics.json')
    inbox = load('control/gpt_supergreen_inbox.json')

    bank_items = [x for x in bank.get('items', []) if isinstance(x, dict)]
    ticks_raw = ledger.get('ticks') if isinstance(ledger.get('ticks'), dict) else {}
    ticks = [x for x in ticks_raw.values() if isinstance(x, dict)]

    cls_counts = Counter(norm(x.get('classification')) or 'UNKNOWN' for x in bank_items)
    tick_verdicts = Counter(norm(x.get('verdict')) or 'UNKNOWN' for x in ticks)

    suspicious_yellows = []
    terminal_yellow_reasons = Counter()
    for x in bank_items:
        if norm(x.get('classification')) != 'YELLOW':
            continue
        eq = norm(x.get('evidence_quality'))
        da = norm(x.get('deadline_authority'))
        unknowns = x.get('unknowns') if isinstance(x.get('unknowns'), list) else []
        why = str(x.get('why') or '')
        text = ' '.join([eq, da, why, ' '.join(map(str, unknowns))]).upper()
        reasons = []
        for token, label in [
            ('REVIEW_REQUIRED', 'review_required'),
            ('UNRESOLVED', 'unresolved'),
            ('INCOMPLETE', 'incomplete'),
            ('UNKNOWN', 'unknown'),
            ('NOT_YET', 'not_yet'),
            ('CONFLICT', 'conflict'),
            ('AUTHENTICITY', 'authenticity'),
            ('LEGACY_PROVENANCE', 'legacy_provenance'),
            ('PUBLIC_DCE', 'public_dce_not_canonical'),
        ]:
            if token in text:
                reasons.append(label)
                terminal_yellow_reasons[label] += 1
        if reasons or unknowns:
            suspicious_yellows.append({
                'candidate_id': x.get('candidate_id'),
                'title': x.get('title'),
                'source_dce_run_id': x.get('source_dce_run_id'),
                'evidence_quality': x.get('evidence_quality'),
                'deadline_authority': x.get('deadline_authority'),
                'unknowns': unknowns,
                'reasons': sorted(set(reasons)),
            })

    null_run_ticks = [x for x in ticks if x.get('source_dce_run_id') in (None, '', 0, '0')]
    yellow_ticks = [x for x in ticks if norm(x.get('verdict')) == 'YELLOW']
    yellow_null_run_ticks = [x for x in yellow_ticks if x.get('source_dce_run_id') in (None, '', 0, '0')]

    idx_counts = index.get('counts') if isinstance(index.get('counts'), dict) else {}
    idx_recovery = index.get('recovery') if isinstance(index.get('recovery'), dict) else {}
    health_counts = metrics.get('counts') if isinstance(metrics.get('counts'), dict) else {}

    latest_inbox_run = inbox.get('latest_source_dce_run_id')
    recovered_runs = [int(x) for x in idx_recovery.get('source_runs_seen_this_refresh', []) if str(x).isdigit()]
    latest_recovered_run = max(recovered_runs) if recovered_runs else None

    payload = {
        'schema': 'TENDER_PIPELINE_LOSS_AUDIT_V1',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'final_bank': {
            'total': len(bank_items),
            'classification_counts': dict(cls_counts),
            'yellow_total': cls_counts.get('YELLOW', 0),
            'suspicious_repairable_yellow_count': len(suspicious_yellows),
            'suspicious_repairable_yellow_reasons': dict(terminal_yellow_reasons),
            'suspicious_repairable_yellow_examples': suspicious_yellows[:100],
        },
        'review_ledger': {
            'ticks_total': len(ticks),
            'verdict_counts': dict(tick_verdicts),
            'ticks_missing_source_dce_run': len(null_run_ticks),
            'yellow_ticks': len(yellow_ticks),
            'yellow_ticks_missing_source_dce_run': len(yellow_null_run_ticks),
            'null_run_examples': null_run_ticks[:100],
        },
        'review_index': {
            'count': int(index.get('count') or 0),
            'counts': idx_counts,
            'recovery': idx_recovery,
        },
        'visible_inbox': {
            'pending': int(health_counts.get('pending_gpt_web_review') or 0),
            'pending_before_visible_cap': int(health_counts.get('pending_before_visible_cap') or 0),
            'latest_source_dce_run_id': latest_inbox_run,
            'latest_recovered_dce_run_id': latest_recovered_run,
            'source_run_lag_detected': bool(latest_recovered_run and str(latest_inbox_run or '').isdigit() and int(latest_inbox_run) < latest_recovered_run),
        },
        'known_structural_risks': [
            'GitHub Actions never performs final business adjudication; GPT_WEB_HANDOFF requires an active assistant pass.',
            'Normal inbox refresh recovers only the newest 10 DCE releases and uses cancel-in-progress=true, creating a burst-loss risk.',
            'All final-bank classes, including YELLOW, are excluded from the review index.',
            'Ledger ticks without source_dce_run_id can suppress all future versions because freshness cannot be compared.',
            'Historical immutable fast-adjudication rows are not re-evaluated with the current dce_evidence_quality rules during index rebuild.',
            'Visible review surface is capped at 160 while the uncapped backlog is thousands of rows.',
        ],
    }
    Path('control/pipeline_loss_audit.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
