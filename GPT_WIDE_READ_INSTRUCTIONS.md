# GPT Wide Read — Live World Snapshot

Source discovery run: `32591521073`  
Snapshot rows: **118,862**  
Generated: `2026-08-22T19:40:33.172426+00:00`

## Objective
Read the snapshot semantically. Do **not** treat keyword matches or legacy heuristic scores as business truth. Select only opportunities plausibly executable by SPM Business. For SOLO-only mode, reject anything requiring borrowed references, mandatory partner/reseller status, regulated certification, local licensed trade, forced consortium, specialist onsite team, or another entity's capacity.

## Required first-pass output
For each tender worth DCE retrieval, return: `candidate_id`, `preliminary_score` (<=89), `reason`, `solo_fit`, and the uncertainty that requires DCE verification. Do not declare FINAL_SUPER_GREEN from notice text.

## DCE request contract
Write `control/gpt_dce_request.json` on `main` using:

```json
{
  "schema": "GPT_DCE_REQUEST_V1",
  "source_discovery_run": 32591521073,
  "wide_read_run_id": 32591521073,
  "default_preliminary_score": 84,
  "status": "DCE_PENDING",
  "mode": "SOLO_LEAN",
  "selection_reason": "GPT semantic wide-read of the complete live snapshot",
  "candidate_ids": ["..."]
}
```

A push of that file triggers the DCE fanout. DCE retrieval does not imply eligibility. Final 90+/FINAL_SUPER_GREEN still requires authoritative DCE evidence for every mandatory gate.

## Discovery coverage
Coverage status: **PARTIAL_WORLD_COVERAGE**.

Read `discovery_coverage.json` before making worldwide or source-completeness claims. A failed/unknown source lane is missing coverage, never zero opportunities. Rows marked `carried_forward_from_previous_snapshot=true` are continuity rows, not proof that the source succeeded in this generation.
