from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright


INTEREST_RE = re.compile(r"api|download|file|document|export|tender|procedure|public|today|attachment|requirement|version|signed", re.I)
SERVICE_ROOT = "https://service.eop.bg/NX1Service.svc"
SERVICE_JS = f"{SERVICE_ROOT}/js"
SERVICE_METHOD_RE = re.compile(
    r"(?:GetPublishedTenderExportsByTenderId|GetPublishedTenderDetails|GetDocument|DownloadDocument|"
    r"GetSigned|SignedUrl|Download|DocumentById|GetPublicTenderRequirements|RequirementBox)",
    re.I,
)
PUBLIC_HEADERS = {
    "User-Agent": "Tender-Engine/EOP-Public-Probe",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://app.eop.bg",
    "Referer": "https://app.eop.bg/",
}


def _safe_headers(headers: dict) -> dict:
    keep = {"content-type", "accept", "origin", "referer", "x-requested-with"}
    return {str(k): str(v)[:1000] for k, v in (headers or {}).items() if str(k).casefold() in keep}


def _safe_response_headers(headers: dict) -> dict:
    keep = {"content-type", "content-disposition", "content-length", "etag", "last-modified", "cache-control"}
    return {str(k): str(v)[:1200] for k, v in (headers or {}).items() if str(k).casefold() in keep}


def _jsonable_response(resp: requests.Response):
    try:
        return resp.json()
    except Exception:
        text = resp.text if resp.content else ""
        return {"_raw_text": text[:200_000]}


def _public_post(session: requests.Session, method: str, payload: dict) -> dict:
    url = f"{SERVICE_ROOT}/{method}"
    try:
        r = session.post(url, json=payload, headers=PUBLIC_HEADERS, timeout=30, allow_redirects=True)
        return {
            "method": method,
            "url": url,
            "payload": payload,
            "status": r.status_code,
            "resolved_url": r.url,
            "headers": _safe_response_headers(r.headers),
            "data": _jsonable_response(r),
        }
    except Exception as exc:
        return {"method": method, "url": url, "payload": payload, "error": repr(exc)[:1000]}


def _walk_urls(value):
    out = []
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_walk_urls(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_walk_urls(v))
    return list(dict.fromkeys(out))


def _unwrap_data(value):
    # WCF endpoints in this portal sometimes return bare JSON and sometimes a
    # wrapper such as {"d": ...}. Keep this tolerant but deterministic.
    if isinstance(value, dict) and set(value.keys()) == {"d"}:
        return value.get("d")
    return value


def _collect_document_targets(exports_data, details_data):
    targets = []
    exports_data = _unwrap_data(exports_data)
    details_data = _unwrap_data(details_data)
    if isinstance(exports_data, list):
        full = [x for x in exports_data if isinstance(x, dict) and x.get("DocumentId") and x.get("IsFullExport")]
        full.sort(key=lambda x: (str(x.get("ModifiedDate") or ""), int(x.get("Id") or 0)), reverse=True)
        if full:
            x = full[0]
            targets.append({"kind": "latest_full_export", "document_id": int(x["DocumentId"]), "name": str(x.get("Name") or "")})
    if isinstance(details_data, dict):
        docs = details_data.get("TenderDescriptionDocuments") or []
        for x in docs[:8] if isinstance(docs, list) else []:
            if not isinstance(x, dict):
                continue
            doc_id = x.get("Id") or x.get("DocumentId")
            if doc_id:
                targets.append({
                    "kind": "tender_description_document",
                    "document_id": int(doc_id),
                    "name": str(x.get("Name") or ""),
                    "document_cloud_name": str(x.get("DocumentCloudName") or ""),
                    "blob_storage_id": x.get("BlobStorageId"),
                })
    deduped = []
    seen = set()
    for x in targets:
        key = x.get("document_id")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(x)
    return deduped


def _probe_download_url(session: requests.Session, url: str) -> dict:
    try:
        r = session.get(url, headers={"User-Agent": PUBLIC_HEADERS["User-Agent"], "Accept": "*/*"}, timeout=30, allow_redirects=True, stream=True)
        first = b""
        if r.status_code < 500:
            try:
                first = next(r.iter_content(chunk_size=4096), b"")
            except Exception:
                first = b""
        row = {
            "url": url,
            "status": r.status_code,
            "resolved_url": r.url,
            "host": urlparse(r.url).hostname,
            "headers": _safe_response_headers(r.headers),
            "first_bytes_hex": first[:32].hex(),
            "first_bytes_ascii": first[:80].decode("latin-1", errors="replace"),
        }
        r.close()
        return row
    except Exception as exc:
        return {"url": url, "error": repr(exc)[:1000]}


def _probe_public_signed_urls(tender_id: int) -> dict:
    session = requests.Session()
    exports = _public_post(session, "GetPublishedTenderExportsByTenderId", {"tenderId": tender_id})
    details = _public_post(session, "GetPublishedTenderDetails", {"tenderId": tender_id, "cultureAbbreviation": "bg-BG"})
    targets = _collect_document_targets(exports.get("data"), details.get("data"))
    signed_attempts = []
    for target in targets[:6]:
        doc_id = int(target["document_id"])
        methods = [
            ("GetSignedUrlByDocumentIdWithTenderId", {"documentId": doc_id, "tenderId": tender_id}),
            ("GetSignedUrlByDocumentId", {"documentId": doc_id}),
        ]
        if target.get("blob_storage_id") is not None:
            methods.append((
                "GetSignedUrlByDocumentIdWithDocumentAzureBlobStorageId",
                {"documentId": doc_id, "documentAzureBlobStorageId": target.get("blob_storage_id")},
            ))
        result = {"target": target, "methods": []}
        found_urls = []
        for method, payload in methods:
            attempt = _public_post(session, method, payload)
            urls = _walk_urls(attempt.get("data"))
            attempt["discovered_urls"] = urls
            result["methods"].append(attempt)
            found_urls.extend(urls)
            if urls:
                break
        result["download_probes"] = [_probe_download_url(session, u) for u in list(dict.fromkeys(found_urls))[:3]]
        signed_attempts.append(result)
    return {
        "tender_id": tender_id,
        "exports": exports,
        "details": details,
        "targets": targets,
        "signed_attempts": signed_attempts,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://app.eop.bg/today/597088")
    ap.add_argument("--out", default="bg-eop-probe.json")
    args = ap.parse_args()

    m = re.search(r"/today/(\d+)", args.url)
    tender_id = int(m.group(1)) if m else 0
    if not tender_id:
        raise SystemExit("EOP public URL is missing /today/{tender_id}")

    chrome = os.getenv("CHROME_BIN") or None
    responses = []
    json_bodies = []
    console = []

    service_js = ""
    try:
        rr = requests.get(SERVICE_JS, timeout=30, headers={"User-Agent": PUBLIC_HEADERS["User-Agent"]})
        if rr.ok:
            service_js = rr.text[:8_000_000]
    except Exception:
        service_js = ""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=chrome, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(ignore_https_errors=False, locale="bg-BG")
        page = context.new_page()

        def on_console(msg):
            try:
                console.append({"type": msg.type, "text": msg.text[:1500]})
            except Exception:
                pass

        def on_response(resp):
            try:
                req = resp.request
                headers = resp.headers or {}
                ct = str(headers.get("content-type") or "")
                try:
                    post_data = req.post_data
                except Exception:
                    post_data = None
                row = {
                    "url": str(resp.url),
                    "status": int(resp.status),
                    "resource_type": str(req.resource_type),
                    "method": str(req.method),
                    "request_post_data": (post_data[:100_000] if isinstance(post_data, str) else None),
                    "request_headers": _safe_headers(req.headers or {}),
                    "content_type": ct[:250],
                    "content_disposition": str(headers.get("content-disposition") or "")[:500],
                }
                responses.append(row)
                if ("json" in ct.casefold() or req.resource_type in {"xhr", "fetch"}) and resp.status < 500:
                    try:
                        body = resp.text()
                        if body and len(body) <= 1_500_000:
                            json_bodies.append({**row, "body": body[:1_500_000]})
                    except Exception as exc:
                        json_bodies.append({**row, "body_error": repr(exc)[:500]})
            except Exception:
                pass

        page.on("console", on_console)
        page.on("response", on_response)
        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        page.wait_for_timeout(1500)

        try:
            body = page.locator("body").inner_text(timeout=15000)
        except Exception:
            body = ""
        try:
            anchors = page.locator("a[href]").evaluate_all(
                "els => els.map(a => ({href:a.href||'', text:(a.innerText||a.textContent||'').trim(), download:a.getAttribute('download')||''}))"
            )
        except Exception:
            anchors = []
        try:
            controls = page.locator("button, [role='button'], a, [tabindex], [class*='download'], [class*='document']").evaluate_all(
                """els => els.map((e,index) => ({index,tag:e.tagName,text:(e.innerText||e.textContent||'').trim(),aria:e.getAttribute('aria-label')||'',title:e.getAttribute('title')||'',href:e.href||'',cls:e.className||'',data:Object.fromEntries(Array.from(e.attributes||[]).filter(a=>a.name.startsWith('data-')).map(a=>[a.name,a.value]))})).filter(x => x.text||x.aria||x.title||x.href)"""
            )
        except Exception:
            controls = []
        try:
            performance_urls = page.evaluate("performance.getEntriesByType('resource').map(x => x.name)") or []
        except Exception:
            performance_urls = []
        try:
            scripts = page.locator("script[src]").evaluate_all("els => els.map(e => e.src).filter(Boolean)")
        except Exception:
            scripts = []

        browser.close()

    service_method_snippets = []
    if service_js:
        for match in SERVICE_METHOD_RE.finditer(service_js):
            a = max(0, match.start() - 700)
            b = min(len(service_js), match.end() + 1800)
            snippet = service_js[a:b]
            if snippet not in service_method_snippets:
                service_method_snippets.append(snippet)
            if len(service_method_snippets) >= 100:
                break

    signed_url_probe = _probe_public_signed_urls(tender_id)
    result = {
        "url": args.url,
        "tender_id": tender_id,
        "resolved_url": args.url,
        "body_text": body[:500_000],
        "anchors": anchors[:1500],
        "controls": controls[:2000],
        "responses": responses[:6000],
        "json_bodies": json_bodies[:1200],
        "performance_urls": performance_urls[:6000],
        "scripts": scripts[:500],
        "service_js_url": SERVICE_JS,
        "service_js_chars": len(service_js),
        "service_method_snippets": service_method_snippets,
        "signed_url_probe": signed_url_probe,
        "console": console[:500],
    }
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "resolved_url": result["resolved_url"],
        "body_chars": len(body),
        "responses": len(responses),
        "json_bodies": len(json_bodies),
        "anchors": len(anchors),
        "controls": len(controls),
        "service_js_chars": len(service_js),
        "signed_url_probe": signed_url_probe,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
