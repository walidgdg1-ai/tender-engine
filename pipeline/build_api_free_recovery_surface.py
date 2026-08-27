from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('control')
STRESS_DIR = ROOT / 'gpt_gate_stress_batches'
OUT_DIR = ROOT / 'api_free_recovery'

LEAN_RX = re.compile(
    r'\b(website|web site|web portal|web app|cms|wordpress|drupal|accessibility|graphic design|branding|brand |'
    r'video|animation|motion graphics|social media|digital marketing|content creation|copywriting|printing|print |'
    r'transcription|subtitl|caption|translation|e[- ]?learning|learning management|lms|digitisation|digitization|ocr|'
    r'data entry|workflow automation|low[- ]?code|no[- ]?code|application development|software development)\b', re.I
)
HARD_RX = re.compile(
    r'\b(sap|oracle|servicenow|salesforce|cisco|firewall|siem|soc\b|pki\b|data ?center|datacentre|'
    r'hardware|construction|civil works|hvac|plumbing|medical equipment|laboratory equipment|weapon|ammunition|'
    r'clearance|top secret|defen[cs]e|aircraft|vehicle maintenance)\b', re.I
)
REPAIR_SIGNAL_RX = re.compile(
    r'(unknown|unresolved|incomplete|review.required|not.yet|authentic|conflict|verify|verification|exact|pending)', re.I
)


def load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def cid(row: dict[str, Any]) -> str:
    return str(row.get('candidate_id') or '').strip()


def key(row: dict[str, Any]) -> str:
    return cid(row).casefold()


def deadline_open(row: dict[str, Any]) -> bool:
    raw = str(row.get('deadline') or '').strip()
    if not raw:
        auth = row.get('deadline_authority')
        if isinstance(auth, dict):
            raw = str(auth.get('authoritative_submission_date') or '').strip()
    if not raw:
        return True
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).date() >= datetime.now(timezone.utc).date()
    except Exception:
        try:
            return datetime.fromisoformat(raw[:10]).date() >= datetime.now(timezone.utc).date()
        except Exception:
            return True


def load_stress_evidence() -> dict[str, dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for p in sorted(STRESS_DIR.glob('batch-*.json')):
        obj = load(p, {})
        for row in obj.get('items') or []:
            if not isinstance(row, dict) or not key(row):
                continue
            k = key(row)
            quality = (
                int(bool(row.get('raw_dce_row_found'))),
                int(row.get('evidence_gate_coverage') or 0),
                sum(len(v or []) for v in (row.get('gate_evidence') or {}).values() if isinstance(v, list)),
                int(row.get('spm_post_dce_score') or 0),
            )
            old = best.get(k)
            if old is None or quality > old['_quality']:
                copy = dict(row)
                copy['_quality'] = quality
                copy['_stress_source_file'] = p.name
                best[k] = copy
    for row in best.values():
        row.pop('_quality', None)
    return best


def rank(row: dict[str, Any]) -> tuple[int, list[str]]:
    band = str(row.get('spm_fit_band') or '').upper()
    base = {'HOT': 92, 'GOOD': 82, 'MAYBE': 66, 'LOW': 34}.get(band, 50)
    base = max(base, min(89, int(row.get('spm_post_dce_score') or row.get('priority_score') or 0)))
    text = ' '.join(str(row.get(x) or '') for x in ('title', 'buyer'))
    reasons: list[str] = []
    if LEAN_RX.search(text):
        base += 16
        reasons.append('lean_scope_title')
    if HARD_RX.search(text):
        base -= 30
        reasons.append('enterprise_physical_or_security_friction')
    coverage = int(row.get('evidence_gate_coverage') or 0)
    if coverage:
        base += min(8, coverage)
        reasons.append(f'gate_coverage_{coverage}')
    evidence = row.get('gate_evidence') or row.get('evidence_by_gate') or {}
    if isinstance(evidence, dict) and any(evidence.values()):
        base += 5
        reasons.append('repo_local_gate_evidence')
    if row.get('recovery_lane') == 'YELLOW_REPAIR_PENDING_DCE':
        base += 10
        reasons.append('previously_adjudicated_repairable_yellow')
    return max(0, min(100, base)), reasons


def compact(row: dict[str, Any]) -> dict[str, Any]:
    evidence = row.get('gate_evidence') or row.get('evidence_by_gate') or {}
    packed: dict[str, list[Any]] = {}
    if isinstance(evidence, dict):
        for gate, vals in evidence.items():
            if not isinstance(vals, list) or not vals:
                continue
            out = []
            for item in vals[:3]:
                if isinstance(item, dict):
                    c = {k: item.get(k) for k in ('text','snippet','source','source_url','source_sha256','match') if item.get(k) not in (None,'')}
                    if 'text' in c:
                        c['text'] = ' '.join(str(c['text']).split())[:1400]
                    if 'snippet' in c:
                        c['snippet'] = ' '.join(str(c['snippet']).split())[:1400]
                    out.append(c)
                else:
                    out.append(' '.join(str(item).split())[:1400])
            if out:
                packed[str(gate)] = out
    return {
        'candidate_id': row.get('candidate_id'),
        'title': row.get('title'),
        'buyer': row.get('buyer'),
        'portal': row.get('portal'),
        'notice_url': row.get('notice_url'),
        'deadline': row.get('deadline'),
        'estimated_value': row.get('estimated_value'),
        'currency': row.get('currency'),
        'review_stage': row.get('review_stage'),
        'recovery_lane': row.get('recovery_lane'),
        'gate_readiness': row.get('gate_readiness'),
        'content_quality': row.get('content_quality'),
        'spm_fit_band': row.get('spm_fit_band'),
        'spm_post_dce_score': row.get('spm_post_dce_score'),
        'evidence_gate_coverage': row.get('evidence_gate_coverage'),
        'artifact_locator': row.get('artifact_locator'),
        'previous_classification': row.get('previous_classification'),
        'previous_unknowns': row.get('previous_unknowns'),
        'previous_blockers': row.get('previous_blockers'),
        'deadline_authority': row.get('deadline_authority'),
        'gate_evidence': packed,
        '_stress_source_file': row.get('_stress_source_file'),
        'api_free_priority': row.get('api_free_priority'),
        'api_free_rank_reasons': row.get('api_free_rank_reasons'),
        'finality': 'API_FREE_RECOVERY_INPUT_NOT_A_VERDICT',
    }


def main() -> None:
    index = load(ROOT / 'gpt_review_index.json', {})
    bank = load(ROOT / 'final_supergreen_bank.json', {})
    stress = load_stress_evidence()

    terminal = {
        key(x) for x in bank.get('items') or []
        if isinstance(x, dict) and str(x.get('classification') or '').upper() in {'GREEN','FINAL_SUPER_GREEN','RED'}
    }

    rows: dict[str, dict[str, Any]] = {}
    for raw in index.get('items') or []:
        if not isinstance(raw, dict) or not key(raw) or key(raw) in terminal or not deadline_open(raw):
            continue
        if str(raw.get('review_stage') or 'BUSINESS_GATES').upper() != 'BUSINESS_GATES':
            continue
        row = dict(raw)
        row['recovery_lane'] = 'BUSINESS_GATES_INDEX'
        ev = stress.get(key(raw))
        if ev:
            for field in ('gate_evidence','evidence_gate_coverage','deadline_authority','evidence_quality','content_quality','gate_readiness','artifact_locator','_stress_source_file'):
                if ev.get(field) not in (None, {}, [], ''):
                    row[field] = ev.get(field)
        rows[key(row)] = row

    repair_added = 0
    for raw in bank.get('items') or []:
        if not isinstance(raw, dict) or str(raw.get('classification') or '').upper() != 'YELLOW' or not key(raw) or not deadline_open(raw):
            continue
        text = ' '.join(map(str, [raw.get('evidence_quality'), raw.get('deadline_authority'), raw.get('why'), *(raw.get('unknowns') or [])]))
        if not (raw.get('unknowns') or REPAIR_SIGNAL_RX.search(text)):
            continue
        k = key(raw)
        row = dict(rows.get(k) or {})
        for field in ('candidate_id','title','buyer','deadline','notice_url','estimated_value','currency','source_dce_run_id'):
            if not row.get(field) and raw.get(field) not in (None,''):
                row[field] = raw.get(field)
        row['review_stage'] = 'YELLOW_REPAIR_PENDING_DCE'
        row['recovery_lane'] = 'YELLOW_REPAIR_PENDING_DCE'
        row['previous_classification'] = 'YELLOW'
        row['previous_unknowns'] = raw.get('unknowns') or []
        row['previous_blockers'] = raw.get('blockers') or []
        row['deadline_authority'] = raw.get('deadline_authority') or row.get('deadline_authority')
        row['content_quality'] = raw.get('evidence_quality') or row.get('content_quality')
        ev = stress.get(k)
        if ev:
            for field in ('gate_evidence','evidence_gate_coverage','artifact_locator','_stress_source_file'):
                if ev.get(field) not in (None, {}, [], ''):
                    row[field] = ev.get(field)
        rows[k] = row
        repair_added += 1

    ranked = []
    for row in rows.values():
        score, reasons = rank(row)
        row['api_free_priority'] = score
        row['api_free_rank_reasons'] = reasons
        ranked.append(row)
    ranked.sort(key=lambda r: (int(r.get('api_free_priority') or 0), int(r.get('spm_post_dce_score') or 0), int(r.get('evidence_gate_coverage') or 0)), reverse=True)

    business = [r for r in ranked if r.get('recovery_lane') == 'BUSINESS_GATES_INDEX']
    repairs = [r for r in ranked if r.get('recovery_lane') == 'YELLOW_REPAIR_PENDING_DCE']
    evidence_hydrated = [r for r in ranked if (r.get('gate_evidence') or {})]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / 'business_gates_all.jsonl').open('w', encoding='utf-8') as f:
        for row in business:
            f.write(json.dumps(compact(row), ensure_ascii=False, separators=(',', ':')) + '\n')

    payload = {
        'schema': 'API_FREE_RECOVERY_SURFACE_V1',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source_index_count': int(index.get('count') or 0),
        'terminal_ids_excluded': len(terminal),
        'business_gates_recovered': len(business),
        'repairable_yellows_reopened': len(repairs),
        'stress_rows_available_repo_local': len(stress),
        'rows_with_repo_local_gate_evidence': len(evidence_hydrated),
        'priority_90_plus': sum(1 for r in ranked if int(r.get('api_free_priority') or 0) >= 90),
        'priority_80_plus': sum(1 for r in ranked if int(r.get('api_free_priority') or 0) >= 80),
        'by_fit_band': dict(Counter(str(r.get('spm_fit_band') or 'UNKNOWN') for r in business)),
        'top_250': [compact(r) for r in ranked[:250]],
        'yellow_repairs': [compact(r) for r in repairs],
        'contract': 'API-free recovery only. No row here is a verdict. BUSINESS_GATES rows may be adjudicated only to the extent their persisted evidence resolves mandatory gates. YELLOW_REPAIR_PENDING_DCE rows remain non-final until a fresh authoritative DCE repair resolves their unknowns.',
    }
    (ROOT / 'gpt_api_free_recovery_surface.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    summary = {k: v for k, v in payload.items() if k not in {'top_250','yellow_repairs'}}
    (ROOT / 'gpt_api_free_recovery_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
