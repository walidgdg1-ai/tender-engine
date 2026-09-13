from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import requests

SERVICE_ROOT = "https://service.eop.bg/NX1Service.svc"
SERVICE_JS = f"{SERVICE_ROOT}/js"
TENDER_ID = 597088
TIME_ZONE = "Europe/Kiev"
HEADERS = {
    "User-Agent": "Tender-Engine/EOP-Public-Probe/2.0",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json; charset=UTF-8",
    "Origin": "https://app.eop.bg",
    "Referer": "https://app.eop.bg/",
    "X-Requested-With": "XMLHttpRequest",
}
METHODS = [
    "GetSignedUrlByDocumentIdWithTenderId",
    "GetSignedUrlByDocumentId",
    "GetSignedUrlByDocumentIdWithDocumentAzureBlobStorageId",
    "GetSignedUrlsByDocumentIds",
]


def post(session: requests.Session, method: str, payload: dict) -> dict:
    url = f"{SERVICE_ROOT}/{method}"
    try:
        r = session.post(url, json=payload, headers=HEADERS, timeout=35, allow_redirects=True)
        try:
            data = r.json()
        except Exception:
            data = {"_text": r.text[:100_000]}
        return {
            "method": method,
            "payload": payload,
            "status": r.status_code,
            "content_type": r.headers.get("content-type"),
            "data": data,
        }
    except Exception as exc:
        return {"method": method, "payload": payload, "error": repr(exc)}


def unwrap(value):
    if isinstance(value, dict) and set(value) == {"d"}:
        return value["d"]
    return value


def walk_urls(value):
    out = []
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(walk_urls(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(walk_urls(v))
    return list(dict.fromkeys(out))


def download_probe(session: requests.Session, url: str) -> dict:
    try:
        r = session.get(url, headers={"User-Agent": HEADERS["User-Agent"], "Accept": "*/*"}, timeout=35, allow_redirects=True, stream=True)
        first = next(r.iter_content(chunk_size=4096), b"") if r.status_code < 500 else b""
        row = {
            "status": r.status_code,
            "resolved_url": r.url,
            "host": urlparse(r.url).hostname,
            "content_type": r.headers.get("content-type"),
            "content_disposition": r.headers.get("content-disposition"),
            "content_length": r.headers.get("content-length"),
            "first_bytes_hex": first[:32].hex(),
        }
        r.close()
        return row
    except Exception as exc:
        return {"error": repr(exc)}


def service_signatures(session: requests.Session) -> dict:
    r = session.get(SERVICE_JS, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=35)
    text = r.text if r.ok else ""
    found = {}
    for method in METHODS:
        # WCF JavaScript proxy shape:
        # Method:function(arg1,arg2,succeededCallback,...){ return this._invoke(...,{arg1:arg1,...},...); }
        rx = re.compile(rf"{re.escape(method)}:function\(([^)]*)\)\s*\{{(.{{0,1800}}?)\n?\}}", re.S)
        m = rx.search(text)
        if not m:
            idx = text.find(method + ":function")
            found[method] = {"found": False, "near": text[idx:idx+2500] if idx >= 0 else ""}
        else:
            found[method] = {"found": True, "args": m.group(1), "body": m.group(2)[:1800]}
    return {"status": r.status_code, "chars": len(text), "methods": found}


def main():
    s = requests.Session()
    signatures = service_signatures(s)

    exports = post(s, "GetPublishedTenderExportsByTenderId", {"tenderId": TENDER_ID, "ianaTimeZone": TIME_ZONE})
    details = post(s, "GetPublishedTenderDetails", {"tenderId": TENDER_ID, "ianaTimeZone": TIME_ZONE})
    export_data = unwrap(exports.get("data"))
    detail_data = unwrap(details.get("data"))

    targets = []
    if isinstance(export_data, list):
        full = [x for x in export_data if isinstance(x, dict) and x.get("IsFullExport") and x.get("DocumentId")]
        full.sort(key=lambda x: (str(x.get("ModifiedDate") or ""), int(x.get("Id") or 0)), reverse=True)
        if full:
            x = full[0]
            targets.append({"kind": "full_export", "document_id": int(x["DocumentId"]), "name": x.get("Name")})
    if isinstance(detail_data, dict):
        for x in (detail_data.get("TenderDescriptionDocuments") or [])[:2]:
            if isinstance(x, dict) and (x.get("Id") or x.get("DocumentId")):
                targets.append({
                    "kind": "description",
                    "document_id": int(x.get("Id") or x.get("DocumentId")),
                    "name": x.get("Name"),
                    "blob_storage_id": x.get("BlobStorageId"),
                })

    results = []
    for target in targets:
        did = int(target["document_id"])
        attempts = []
        payloads = [
            ("GetSignedUrlByDocumentIdWithTenderId", {"documentId": did, "tenderId": TENDER_ID}),
            ("GetSignedUrlByDocumentId", {"documentId": did}),
            ("GetSignedUrlsByDocumentIds", {"documentIds": [did]}),
        ]
        if target.get("blob_storage_id") is not None:
            payloads.insert(1, (
                "GetSignedUrlByDocumentIdWithDocumentAzureBlobStorageId",
                {"documentId": did, "documentAzureBlobStorageId": target["blob_storage_id"]},
            ))
        signed_urls = []
        for method, payload in payloads:
            a = post(s, method, payload)
            urls = walk_urls(a.get("data"))
            a["urls"] = urls
            attempts.append(a)
            signed_urls.extend(urls)
            if urls:
                break
        signed_urls = list(dict.fromkeys(signed_urls))
        results.append({
            "target": target,
            "attempts": attempts,
            "download_probes": [{"url": u, **download_probe(s, u)} for u in signed_urls[:2]],
        })

    output = {
        "signatures": signatures,
        "exports": exports,
        "details": details,
        "targets": targets,
        "results": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))

    if exports.get("status") != 200 or details.get("status") != 200:
        raise SystemExit("public EOP metadata calls did not reproduce browser success")
    if not targets:
        raise SystemExit("no public EOP document targets discovered")
    successful = [p for r in results for p in r["download_probes"] if p.get("status") in (200, 206) and p.get("first_bytes_hex")]
    if not successful:
        raise SystemExit("no anonymous signed EOP download was proven")


if __name__ == "__main__":
    main()
