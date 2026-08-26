from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

ACCESS_GUIDE_FILENAME_PATTERNS = [
    r"instructions?.*(?:access|tender|portal)", r"how.*access.*tender", r"user.?guide", r"supplier.*guide", r"portal.*guide", r"terms.*use",
    r"guide.*utilisateur", r"notice.*utilisation", r"mode.*emploi", r"nutzungsbedingungen", r"bedienungsanleitung", r"benutzerhandbuch", r"datenschutz",
    r"gebruikershandleiding", r"instrukcja", r"podręcznik użytkownika", r"manual.*usuario", r"gu[ií]a.*usuario", r"manuale.*utente", r"guida.*utente",
    r"manual.*utilizador", r"guia.*utilizador", r"cgu.*march", r"depot[-_ ]?pli",
    # Generic portal/support files can contain the candidate title, deadline and
    # procurement vocabulary. They are transport/access evidence, not the DCE.
    r"portal[_ .-]?page", r"(?:^|[/_.-])cgu(?:[/_.-]|$)", r"bieterunterst.*tzung",
]

ACCESS_GUIDE_TEXT_PATTERNS = [
    r"instructions? on how to access", r"how to access .*tenders?", r"express interest", r"view documents",
    r"register(?:ed|ing|ation)? (?:on|at|with) (?:the )?(?:ungm|portal|e[- ]?tender)", r"supplier registration", r"complete your registration",
    r"login to (?:the )?(?:portal|system)", r"click (?:on )?[\"']?(?:express interest|view documents)", r"redirected to .*tender",
    r"guide (?:d['’])?utilisation", r"comment accéder", r"se connecter au portail", r"créer (?:un|votre) compte",
    r"anleitung.*zugang", r"benutzerhandbuch", r"anmelden.*portal", r"registrier(?:en|ung).*portal",
    r"gebruikershandleiding", r"inloggen.*portaal", r"registreren.*portaal",
    r"instrukcja.*dostęp", r"zaloguj.*portal", r"rejestracja.*portal",
    r"manual de usuario", r"gu[ií]a de usuario", r"acceder al portal", r"registrarse.*portal",
    r"manuale utente", r"guida utente", r"accedere al portale", r"registrazione.*portale",
    r"manual do utilizador", r"guia do utilizador", r"aceder ao portal", r"registo.*portal",
]

INTEREST_REQUIRED_PATTERNS = [
    r"express interest", r"record(?:ing)? (?:your )?interest", r"register interest", r"manifest(?:er|ation).*intérêt", r"interesse bekunden",
    r"belangstelling registreren", r"wyrazić zainteresowanie", r"manifestar inter[eé]s", r"manifestare interesse", r"manifestar interesse",
]

SUBSTANTIVE_PATTERNS = [
    r"request for tender", r"request for proposal", r"invitation to tender", r"invitation to submit", r"terms of reference", r"scope of work",
    r"statement of work", r"requirements and specifications", r"technical specifications?", r"award criteria", r"selection criteria", r"evaluation criteria",
    r"pricing schedule", r"form of tender", r"conditions of contract", r"contract duration", r"submission deadline", r"deadline for (?:receipt|submission)",
    r"minimum turnover", r"professional indemnity", r"public liability",
    r"r[eè]glement de (?:la )?consultation", r"cahier des clauses", r"cahier des charges", r"sp[eé]cifications techniques?", r"crit[eè]res? d['’]attribution",
    r"crit[eè]res? de s[eé]lection", r"date limite de remise", r"date limite de r[eé]ception", r"acte d['’]engagement", r"bordereau des prix", r"m[eé]moire technique",
    r"leistungsbeschreibung", r"vergabeunterlagen", r"zuschlagskriterien", r"eignungskriterien", r"angebotsfrist", r"preisblatt", r"vertragsbedingungen",
    r"aanbestedingsleidraad", r"programma van eisen", r"gunningscriteria", r"geschiktheidseisen", r"inschrijvings(?:termijn|deadline)", r"prijzenblad",
    r"specyfikacja warunk[oó]w zam[oó]wienia", r"\bSWZ\b", r"opis przedmiotu zam[oó]wienia", r"kryteria oceny ofert", r"termin sk[łl]adania ofert",
    r"pliego de prescripciones t[eé]cnicas", r"pliego de cl[aá]usulas administrativas", r"criterios de adjudicaci[oó]n", r"plazo de presentaci[oó]n", r"presupuesto base",
    r"capitolato tecnico", r"disciplinare di gara", r"criteri di aggiudicazione", r"termine (?:di )?presentazione", r"offerta economica",
    r"caderno de encargos", r"programa do procedimento", r"crit[eé]rios de adjudica[cç][aã]o", r"prazo para apresenta[cç][aã]o", r"proposta financeira",
    r"iarratas ar thairiscint", r"cuireadh chun tairisceana", r"crit[eé]ir d[aá]mhachtana", r"riachtanais agus sonra[ií]ochta[ií]",
]

GENERIC_FILENAME_RX = [re.compile(p, re.I) for p in ACCESS_GUIDE_FILENAME_PATTERNS]
ACCESS_RX = [re.compile(p, re.I) for p in ACCESS_GUIDE_TEXT_PATTERNS]
INTEREST_RX = [re.compile(p, re.I) for p in INTEREST_REQUIRED_PATTERNS]
SUBSTANTIVE_RX = [re.compile(p, re.I) for p in SUBSTANTIVE_PATTERNS]

STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "into", "website", "services", "service",
    "tender", "contract", "public", "procurement", "provision", "creation", "development", "support",
}

ZA_TRUSTED_TENDER_DOCUMENT_TYPES = {
    "biddingdocuments",
    "technicalspecifications",
    "evaluationcriteria",
    "eligibilitycriteria",
    "contractdraft",
    "billofquantity",
}


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def _matches(regexes: list[re.Pattern], text: str) -> list[str]:
    out = []
    for rx in regexes:
        m = rx.search(text or "")
        if m:
            out.append(m.group(0)[:160])
    return out


def _title_tokens(title: str) -> list[str]:
    toks = re.findall(r"[A-Za-zÀ-ÿ0-9]{4,}", (title or "").lower())
    return sorted({t for t in toks if t not in STOPWORDS})


def _title_overlap(title: str, text: str) -> dict:
    toks = _title_tokens(title)
    if not toks:
        return {"tokens": [], "hits": [], "ratio": 0.0}
    low = (text or "").lower()
    hits = [t for t in toks if t in low]
    return {"tokens": toks[:30], "hits": hits[:30], "ratio": round(len(hits) / len(toks), 4)}


def _is_notice_only_source_url(url: str) -> bool:
    """Deterministic publication-PDF guard.

    BOAMP's /telechargements/...PDF files are rendered publication notices, not
    the buyer's consultation package. They may contain procurement markers and a
    high title overlap, so semantic heuristics alone cannot distinguish them.
    """
    try:
        p = urlparse(str(url or ""))
    except Exception:
        return False
    host = (p.hostname or "").casefold()
    path = (p.path or "").casefold()
    return host.endswith("boamp.fr") and "/telechargements/" in path and path.endswith(".pdf")


def _candidate_notice_id(candidate: dict, manifest: dict) -> str:
    raw = candidate.get("notice_id") or manifest.get("notice_id") or candidate.get("candidate_id") or manifest.get("candidate_id") or ""
    value = str(raw).strip()
    if ":" in value:
        value = value.rsplit(":", 1)[-1]
    return value


def _usable_text_docs(docs: list[dict]) -> int:
    return sum(1 for x in docs if isinstance(x, dict) and int(x.get("text_chars") or 0) >= 200)


def _official_khmdhs_attachment_provenance(candidate: dict, manifest: dict, source_urls: list[str], docs: list[dict]) -> dict:
    """Prove an exact Greek KHMDHS official attachment without guessing from text."""
    notice_id = _candidate_notice_id(candidate, manifest)
    if not notice_id:
        return {"matched": False, "reason": "missing_notice_id"}
    candidate_portal = str(candidate.get("portal") or candidate.get("source") or "").upper()
    if candidate_portal not in {"GR_KHMDHS", "GR-KHMDHS"} and not str(manifest.get("candidate_id") or "").upper().startswith("GR-KHMDHS:"):
        return {"matched": False, "reason": "not_gr_khmdhs"}
    hits = []
    expected_suffix = "/khmdhs-opendata/notice/attachment/" + notice_id.casefold()
    for raw in source_urls:
        try:
            p = urlparse(str(raw))
        except Exception:
            continue
        host = (p.hostname or "").casefold()
        path = unquote(p.path or "").rstrip("/").casefold()
        if host == "cerpp.eprocurement.gov.gr" and path.endswith(expected_suffix):
            hits.append(str(raw))
    docs_with_text = _usable_text_docs(docs)
    return {
        "matched": bool(hits) and docs_with_text > 0,
        "notice_id": notice_id,
        "matching_source_urls": hits,
        "documents_with_usable_text": docs_with_text,
        "authority": "Greek KHMDHS official government notice-attachment endpoint",
    }


def _official_placsp_attachment_provenance(candidate: dict, manifest: dict, source_urls: list[str], docs: list[dict]) -> dict:
    """Prove that a persisted file came from the exact matched official PLACSP Atom entry.

    The v17 resolver only records document_urls after _entry_match succeeds on an
    official contrataciondelsectorpublico.gob.es Atom entry. We still require the
    persisted source URL to be one of those exact document URLs and usable text.
    """
    portal = str(candidate.get("portal") or candidate.get("source") or "").upper()
    cid = str(candidate.get("candidate_id") or manifest.get("candidate_id") or "").upper()
    if portal != "ES_PLACSP" and not cid.startswith("ES-PLACSP:"):
        return {"matched": False, "reason": "not_es_placsp"}
    resolution = manifest.get("placsp_atom_resolution")
    if not isinstance(resolution, dict):
        return {"matched": False, "reason": "missing_atom_resolution"}
    allowed = {str(x).strip() for x in (resolution.get("document_urls") or []) if str(x).strip()}
    if not allowed:
        return {"matched": False, "reason": "missing_official_document_urls"}
    entry_found = any(
        isinstance(a, dict)
        and str(a.get("method") or "") == "ES_PLACSP_OFFICIAL_ATOM_V17"
        and str(a.get("outcome") or "") == "ENTRY_FOUND"
        for a in (manifest.get("dce_method_attempts") or [])
    )
    hits = [u for u in source_urls if u in allowed]
    docs_with_text = _usable_text_docs(docs)
    return {
        "matched": entry_found and bool(hits) and docs_with_text > 0,
        "matching_source_urls": hits,
        "documents_with_usable_text": docs_with_text,
        "match_method": resolution.get("match_method"),
        "contract_folder_id": resolution.get("contract_folder_id"),
        "authority": "Spain PLACSP official Atom matched-entry procurement document reference",
    }


def _za_candidate_ocid(candidate: dict, manifest: dict) -> str:
    for value in (candidate.get("ocid"), candidate.get("procedure_id"), manifest.get("ocid")):
        s = str(value or "").strip()
        if s.startswith("ocds-"):
            return s
    cid = str(candidate.get("candidate_id") or manifest.get("candidate_id") or "")
    m = re.search(r"(?:^|:)(ocds-[A-Za-z0-9-]+?)(?:-\d{4}-\d{2}-\d{2})?$", cid)
    return m.group(1) if m else ""


def _official_za_ocds_provenance(candidate: dict, manifest: dict, source_urls: list[str], docs: list[dict]) -> dict:
    """Prove an exact South African OCDS tender-document chain, fail-closed.

    A release lookup must resolve the candidate's exact OCID on the official API;
    a persisted URL must have a successful ZA_OCDS_DOCUMENT_V16 attempt; and that
    attempt must carry a tender-document type rather than a publication notice.
    """
    portal = str(candidate.get("portal") or candidate.get("source") or "").upper()
    cid = str(candidate.get("candidate_id") or manifest.get("candidate_id") or "").upper()
    if portal not in {"ZA_ETENDERS", "ZA_ETENDERS_OCDS"} and not cid.startswith("ZA_ETENDERS"):
        return {"matched": False, "reason": "not_za_etenders"}
    ocid = _za_candidate_ocid(candidate, manifest)
    if not ocid:
        return {"matched": False, "reason": "missing_ocid"}
    official_release = False
    trusted_downloads = []
    source_set = set(source_urls)
    expected_path = "/api/ocdsreleases/release/" + ocid.casefold()
    for attempt in manifest.get("dce_method_attempts") or []:
        if not isinstance(attempt, dict):
            continue
        method = str(attempt.get("method") or "")
        outcome = str(attempt.get("outcome") or "")
        if method == "ZA_OCDS_RELEASE_API_V16" and outcome == "RELEASE_FOUND":
            try:
                p = urlparse(str(attempt.get("resolved_url") or attempt.get("url") or ""))
                if (p.hostname or "").casefold() == "ocds-api.etenders.gov.za" and (p.path or "").rstrip("/").casefold().endswith(expected_path):
                    official_release = True
            except Exception:
                pass
        if method == "ZA_OCDS_DOCUMENT_V16" and outcome == "DOWNLOADED":
            dtype = str(attempt.get("document_type") or "").replace("_", "").replace("-", "").casefold()
            url = str(attempt.get("url") or "")
            if dtype in ZA_TRUSTED_TENDER_DOCUMENT_TYPES and url in source_set:
                trusted_downloads.append({"url": url, "document_type": attempt.get("document_type"), "title": attempt.get("title")})
    docs_with_text = _usable_text_docs(docs)
    return {
        "matched": official_release and bool(trusted_downloads) and docs_with_text > 0,
        "ocid": ocid,
        "official_release_match": official_release,
        "trusted_downloads": trusted_downloads[:20],
        "documents_with_usable_text": docs_with_text,
        "authority": "South Africa eTenders official OCDS release and typed tender-document chain",
    }


def classify_candidate(root: Path) -> dict:
    manifest = _load(root / "manifest.json", {})
    candidate = manifest.get("candidate") or _load(root / "candidate.json", {})
    docs = _load(root / "document_index.json", [])
    corpus_path = root / "corpus.txt"
    corpus = corpus_path.read_text(encoding="utf-8", errors="replace") if corpus_path.exists() else ""
    raw_status = str(manifest.get("status") or "UNKNOWN")
    filenames = [str(x.get("name") or "") for x in docs if isinstance(x, dict)]
    generic_names = [n for n in filenames if n and any(rx.search(n) for rx in GENERIC_FILENAME_RX)]
    access_hits = _matches(ACCESS_RX, corpus)
    interest_hits = _matches(INTEREST_RX, corpus)
    substantive_hits = _matches(SUBSTANTIVE_RX, corpus)
    title_match = _title_overlap(str(candidate.get("title") or ""), corpus)
    text_chars = len(corpus)
    extracted_text_docs = sum(1 for x in docs if isinstance(x, dict) and int(x.get("text_chars") or 0) > 0)

    manifest_files = [x for x in (manifest.get("files") or []) if isinstance(x, dict)]
    source_urls = [str(x.get("source_url") or "") for x in manifest_files if str(x.get("source_url") or "")]
    all_source_files_notice_only = bool(source_urls) and all(_is_notice_only_source_url(u) for u in source_urls)
    khmdhs_provenance = _official_khmdhs_attachment_provenance(candidate, manifest, source_urls, docs)
    placsp_provenance = _official_placsp_attachment_provenance(candidate, manifest, source_urls, docs)
    za_ocds_provenance = _official_za_ocds_provenance(candidate, manifest, source_urls, docs)

    quality = "NOT_APPLICABLE"
    derived_status = raw_status
    gate_readiness = False
    reasons: list[str] = []

    if raw_status == "DOWNLOADED_PUBLIC":
        if all_source_files_notice_only:
            quality = "NOTICE_ONLY"
            derived_status = "NOTICE_ONLY_NOT_DCE"
            reasons.append("all_downloaded_files_are_boamp_publication_notice_pdfs_not_consultation_documents")
        elif extracted_text_docs == 0 or text_chars < 50:
            quality = "EXTRACTION_EMPTY"
            derived_status = "DOWNLOADED_PUBLIC_EMPTY"
            reasons.append("downloaded_files_but_no_extractable_authoritative_text")
        elif khmdhs_provenance.get("matched"):
            quality = "SUBSTANTIVE_DCE_PRESENT"
            derived_status = "DOWNLOADED_PUBLIC"
            gate_readiness = True
            reasons.append("exact_official_khmdhs_notice_attachment_provenance_matches_candidate_id")
        elif placsp_provenance.get("matched"):
            quality = "SUBSTANTIVE_DCE_PRESENT"
            derived_status = "DOWNLOADED_PUBLIC"
            gate_readiness = True
            reasons.append("exact_official_placsp_matched_entry_document_provenance")
        elif za_ocds_provenance.get("matched"):
            quality = "SUBSTANTIVE_DCE_PRESENT"
            derived_status = "DOWNLOADED_PUBLIC"
            gate_readiness = True
            reasons.append("exact_official_za_ocds_typed_tender_document_provenance")
        else:
            named = [n for n in filenames if n]
            all_named_generic = bool(named) and len(generic_names) == len(named)
            strong_access_guide = len(access_hits) >= 2 and len(substantive_hits) <= 1
            weak_specificity = title_match["ratio"] < 0.25
            if all_named_generic or (strong_access_guide and (weak_specificity or text_chars < 120_000)):
                quality = "ACCESS_GUIDE_ONLY" if access_hits else "PORTAL_GENERIC_ONLY"
                if interest_hits:
                    derived_status = "INTEREST_RECORDING_REQUIRED"
                    reasons.append("retrieved_material_instructs_supplier_to_express_or_record_interest_before_real_documents")
                else:
                    derived_status = "PORTAL_GENERIC_ONLY"
                    reasons.append("retrieved_material_is_portal_or_access_boilerplate_not_procurement_specification")
            elif len(substantive_hits) >= 2:
                quality = "MIXED_SUBSTANTIVE_AND_GUIDE" if len(access_hits) >= 2 else "SUBSTANTIVE_DCE_PRESENT"
                derived_status = "DOWNLOADED_PUBLIC"
                gate_readiness = True
                reasons.append("multiple_authoritative_procurement_markers_detected")
            elif title_match["ratio"] >= 0.4 and text_chars >= 2_000:
                quality = "SUBSTANTIVE_DCE_PRESENT"
                derived_status = "DOWNLOADED_PUBLIC"
                gate_readiness = True
                reasons.append("strong_candidate_specificity_in_retrieved_text")
            else:
                quality = "UNKNOWN_RETRIEVED_DOCUMENT"
                derived_status = "DCE_CONTENT_UNVERIFIED"
                reasons.append("download_succeeded_but_authoritative_dce_content_not_proven")
    else:
        reasons.append("retrieval_status_not_downloaded_public")

    return {
        "contract": "DCE_EVIDENCE_QUALITY_V2",
        "candidate_id": manifest.get("candidate_id") or candidate.get("candidate_id") or root.name,
        "raw_status": raw_status,
        "derived_status": derived_status,
        "content_quality": quality,
        "gate_readiness": gate_readiness,
        "text_chars": text_chars,
        "documents_indexed": len(docs) if isinstance(docs, list) else 0,
        "documents_with_text": extracted_text_docs,
        "source_urls": source_urls[:30],
        "all_source_files_notice_only": all_source_files_notice_only,
        "official_khmdhs_provenance": khmdhs_provenance,
        "official_placsp_provenance": placsp_provenance,
        "official_za_ocds_provenance": za_ocds_provenance,
        "generic_filename_hits": generic_names[:30],
        "access_guide_hits": access_hits,
        "interest_required_hits": interest_hits,
        "substantive_marker_hits": substantive_hits,
        "candidate_title_overlap": title_match,
        "reasons": reasons,
        "rule": "DOWNLOADED_PUBLIC is transport success only. Gate review requires semantic candidate specificity or exact trusted official attachment provenance; publication notice PDFs are never promoted. Provenance proves authority, not eligibility, and mandatory-gate UNKNOWN remains UNKNOWN.",
    }


def process(root: Path) -> dict:
    result = classify_candidate(root)
    (root / "evidence_quality.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="out")
    args = ap.parse_args()
    base = Path(args.root)
    roots = sorted(set(p.parent for p in base.rglob("manifest.json")))
    rows = [process(root) for root in roots]
    from collections import Counter
    summary = {
        "candidates": len(rows),
        "content_quality_counts": dict(Counter(r["content_quality"] for r in rows)),
        "derived_status_counts": dict(Counter(r["derived_status"] for r in rows)),
        "gate_ready": sum(1 for r in rows if r["gate_readiness"]),
        "gate_blocked": sum(1 for r in rows if not r["gate_readiness"]),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
