from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CORE = re.compile(
    r"\b(?:website|web\s*portal|web\s*app|web\s*site|web development|web design|cms\b|mobile app|smart phone application|"
    r"software development|application development|digital platform|workflow automation|rpa\b|automation|animation|motion graphics|"
    r"video production|videography|video editing|graphic design|publication design|layout|typesetting|content creation|copywriting|"
    r"translation|transcription|subtitl|caption|printing|brochure|annual report|social media|digital marketing|market research|"
    r"data processing|data entry|digitisation|digitization|ocr\b|scanning|e[- ]learning|accessibility audit)\b",
    re.I,
)

HARD_NOISE = re.compile(
    r"\b(?:construction|roof(?:ing| replacement)|civil works?|excavation|demolition|waste collection|janitorial|cleaning services?|"
    r"medical equipment|laboratory equipment|scientific equipment|ammunition|weapon|fuel delivery|vehicle maintenance|aircraft maintenance|"
    r"hvac|plumbing|electrical works?|uniforms?|furniture|patient lift|road works?|peatland restoration|refurbishment works?)\b",
    re.I,
)

VENDOR_HEAVY = re.compile(
    r"\b(?:citrix|netscaler|sap\b|cisco|oracle|microsoft dynamics|servicenow|salesforce|magnolia cms|adobe experience manager|"
    r"manufacturer authori[sz]ed|authori[sz]ed reseller|hardware appliance|firewall appliance|load balancer|network appliance)\b",
    re.I,
)

GATE_KEYS = (
    "entity_geography",
    "turnover_financial",
    "references_experience",
    "certifications_partner",
    "staffing_team",
    "insurance_bonds",
    "subcontracting_consortium",
    "deliverables_scope",
    "sla_onsite",
    "term_value",
    "award_criteria",
    "forms_signatures",
    "submission",
    "ip_data_security",
    "payment_tax",
)


def load(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def norm(value: Any) -> str:
    return str(value or "").strip().casefold()


def text_of(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or item.get("snippet") or item.get("evidence") or "")
    return str(item or "")


def compact_evidence(row: dict[str, Any], per_gate: int = 2, chars: int = 520) -> dict[str, list[str]]:
    src = row.get("evidence_by_gate") if isinstance(row.get("evidence_by_gate"), dict) else {}
    out: dict[str, list[str]] = {}
    for gate in GATE_KEYS:
        vals = src.get(gate)
        if not isinstance(vals, list):
            continue
        picked: list[str] = []
        for raw in vals:
            text = " ".join(text_of(raw).split())
            if not text:
                continue
            text = text[:chars]
            if text not in picked:
                picked.append(text)
            if len(picked) >= per_gate:
                break
        if picked:
            out[gate] = picked
    return out


def explicit_deadline(row: dict[str, Any]) -> Any:
    auth = row.get("deadline_authority")
    if isinstance(auth, dict):
        if auth.get("authoritative_submission_date"):
            return auth.get("authoritative_submission_date")
        for ev in auth.get("labelled_evidence") or []:
            if not isinstance(ev, dict):
                continue
            if str(ev.get("label") or "").upper() == "SUBMISSION" and ev.get("date"):
                return ev.get("date")
    return row.get("deadline")


def evidence_text(row: dict[str, Any]) -> str:
    bits = [str(row.get("title") or ""), str(row.get("buyer") or "")]
    for vals in compact_evidence(row, per_gate=2, chars=900).values():
        bits.extend(vals)
    return " ".join(bits)


def gate_burden(row: dict[str, Any]) -> tuple[int, list[str]]:
    text = evidence_text(row)
    signals: list[str] = []
    penalty = 0
    patterns = (
        (r"(?:turnover|annual turnover).{0,120}(?:€|eur|£|gbp|\$|usd)\s*\d", 14, "turnover_threshold"),
        (r"(?:at least|minimum of|minimum)\s+(?:two|three|2|3)\s+(?:similar|comparable|reference|contract)", 12, "reference_threshold"),
        (r"(?:iso\s*27001|security clearance|top secret|secret clearance)", 18, "special_cert_or_security"),
        (r"(?:employer.?s liability|public liability|professional indemnity|cyber liability).{0,120}(?:million|€|eur|£|gbp)", 7, "material_insurance"),
        (r"(?:onsite|on-site|site visit|mandatory visit)", 6, "onsite"),
        (r"(?:named personnel|key personnel|cv\b|curriculum vitae|minimum team)", 7, "staffing"),
    )
    for pat, cost, label in patterns:
        if re.search(pat, text, re.I | re.S):
            penalty += cost
            signals.append(label)
    return penalty, signals


def rank_row(row: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    base = int(row.get("spm_post_dce_score") or row.get("gpt_priority_score") or row.get("priority_score") or row.get("preliminary_score") or 0)
    text = evidence_text(row)
    reasons: list[str] = []
    score = base

    if CORE.search(text):
        score += 18
        reasons.append("native_spm_scope")
    if HARD_NOISE.search(text) and not CORE.search(str(row.get("title") or "")):
        score -= 45
        reasons.append("obvious_physical_noise")
    if VENDOR_HEAVY.search(text):
        score -= 28
        reasons.append("vendor_or_enterprise_specialist")

    burden, burden_signals = gate_burden(row)
    score -= burden
    reasons.extend(burden_signals)

    turnover_text = " ".join(compact_evidence(row).get("turnover_financial") or [])
    if re.search(r"\b(?:n/?a|not applicable|no minimum turnover|no turnover threshold)\b", turnover_text, re.I):
        score += 12
        reasons.append("turnover_no_minimum")

    subcontract = " ".join(compact_evidence(row).get("subcontracting_consortium") or [])
    if re.search(r"\b(?:consortium|subcontract|rely on|other entities|group of economic operators)\b", subcontract, re.I):
        score += 4
        reasons.append("partner_route_available")

    if row.get("content_quality") == "SUBSTANTIVE_DCE_PRESENT" and row.get("gate_readiness") is True:
        score += 8
        reasons.append("authoritative_gate_ready")
    if str(row.get("review_stage") or "").upper() == "BUSINESS_GATES":
        score += 5
    if row.get("dce_authenticity_review_required"):
        score -= 50
        reasons.append("authenticity_unresolved")

    return max(0, min(100, score)), {"rank_reasons": reasons, "gate_burden": burden_signals}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", default="control/gpt_supergreen_inbox.json")
    ap.add_argument("--final-bank", default="control/final_supergreen_bank.json")
    ap.add_argument("--ledger", default="control/gpt_web_review_ledger.json")
    ap.add_argument("--out", default="control/gpt_live_review_batch.json")
    ap.add_argument("--max-items", type=int, default=24)
    args = ap.parse_args()

    inbox = load(Path(args.inbox))
    bank = load(Path(args.final_bank))
    ledger = load(Path(args.ledger))
    final_ids = {
        norm(x.get("candidate_id")) for x in bank.get("items", [])
        if isinstance(x, dict)
        and str(x.get("classification") or "").upper() in {"FINAL_SUPER_GREEN", "GREEN", "RED"}
    }
    ticks = ledger.get("ticks") if isinstance(ledger.get("ticks"), dict) else {}
    reviewed_ids = {norm(v.get("candidate_id")) for v in ticks.values() if isinstance(v, dict) and v.get("reviewed")}

    candidates: list[dict[str, Any]] = []
    skipped = {"wrong_stage": 0, "not_gate_ready": 0, "already_final": 0, "already_reviewed": 0}
    for row in inbox.get("review_queue", []) or []:
        if not isinstance(row, dict):
            continue
        cid = norm(row.get("candidate_id"))
        if not cid:
            continue
        if str(row.get("review_stage") or "BUSINESS_GATES").upper() != "BUSINESS_GATES":
            skipped["wrong_stage"] += 1
            continue
        if row.get("gate_readiness") is not True or row.get("content_quality") != "SUBSTANTIVE_DCE_PRESENT":
            skipped["not_gate_ready"] += 1
            continue
        if cid in final_ids:
            skipped["already_final"] += 1
            continue
        if cid in reviewed_ids:
            skipped["already_reviewed"] += 1
            continue
        priority, meta = rank_row(row)
        candidates.append({
            "candidate_id": row.get("candidate_id"),
            "review_key": row.get("review_key"),
            "title": row.get("title"),
            "buyer": row.get("buyer"),
            "portal": row.get("portal"),
            "notice_url": row.get("notice_url"),
            "notice_deadline": row.get("deadline"),
            "best_submission_deadline": explicit_deadline(row),
            "estimated_value": row.get("estimated_value"),
            "currency": row.get("currency"),
            "source_dce_run_id": row.get("source_dce_run_id"),
            "source_shard": row.get("source_shard"),
            "spm_post_dce_score": row.get("spm_post_dce_score"),
            "pipeline_priority": priority,
            "rank_reasons": meta["rank_reasons"],
            "gate_burden_signals": meta["gate_burden"],
            "deadline_authority": row.get("deadline_authority"),
            "evidence": compact_evidence(row),
            "finality": "COMPACT_REVIEW_INPUT_NOT_A_VERDICT",
        })

    candidates.sort(key=lambda x: (int(x.get("pipeline_priority") or 0), int(x.get("spm_post_dce_score") or 0)), reverse=True)
    selected = candidates[: max(1, min(100, args.max_items))]
    payload = {
        "schema": "GPT_LIVE_BUSINESS_REVIEW_BATCH_V1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_inbox_updated_at": inbox.get("updated_at"),
        "latest_source_dce_run_id": inbox.get("latest_source_dce_run_id"),
        "unreviewed_gate_ready_total": len(candidates),
        "selected": len(selected),
        "skipped": skipped,
        "items": selected,
        "contract": "Compact dynamic input for final business adjudication. It never creates a verdict, never upgrades UNKNOWN to PASS, and never bypasses authoritative DCE or final-verdict guards.",
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"unreviewed_gate_ready_total": len(candidates), "selected": len(selected), "top": [x["candidate_id"] for x in selected[:5]], "skipped": skipped}, indent=2))


if __name__ == "__main__":
    main()
