"""The editorial gate. Promotes stories to Published, then build_data.py ships them.

Run as the first step of the deploy pipeline, before build_data.py:
    source .env.local && python scripts/publish.py && python scripts/build_data.py

MODEL — auto-publish with post-moderation (PLAN section 6, "AUTO-PUBLISH" switch):
  * Approved (any sensitivity)   -> Published   (owner's manual clears)
  * Candidate + Safe             -> Published   (auto, when AUTOPUBLISH is on)
  * Candidate + Sensitive        -> left as Candidate (waits for human review)

So non-sensitive content flows to the live site on its own; the owner watches the
site and pulls mistakes after the fact by setting a story's Status to "Rejected"
(it then drops from the build and, because dedup scans every row, is never
re-harvested). Sensitive items never auto-publish — the owner must Approve them.

AUTOPUBLISH env toggle (default "1" = on). Set AUTOPUBLISH=0 to fall back to
manual review (only Approved rows publish; Candidates all wait). Nothing here can
publish a Candidate the owner flagged Sensitive, or a Rejected row.
"""
import os, json, urllib.request, urllib.parse

TOKEN = os.environ["AIRTABLE_TOKEN"]
BASE = "appEiqYd3rnYwUNE7"
TABLE = "tblnfUTaXWRD3VYk6"
API = f"https://api.airtable.com/v0/{BASE}/{TABLE}"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

AUTOPUBLISH = os.environ.get("AUTOPUBLISH", "1").lower() not in ("0", "false", "no", "")


def fetch_pending():
    """Return (id, status, sensitivity) for every Candidate/Approved row."""
    rows, offset = [], None
    formula = urllib.parse.quote("OR({Status}='Approved',{Status}='Candidate')")
    while True:
        url = (f"{API}?filterByFormula={formula}&pageSize=100"
               "&fields%5B%5D=Status&fields%5B%5D=Sensitivity")
        if offset:
            url += f"&offset={offset}"
        req = urllib.request.Request(url, headers=HEADERS)
        data = json.loads(urllib.request.urlopen(req).read())
        for r in data.get("records", []):
            f = r.get("fields", {})
            rows.append((r["id"], f.get("Status"), f.get("Sensitivity", "Safe")))
        offset = data.get("offset")
        if not offset:
            break
    return rows


def to_publish(rows):
    ids = []
    for rid, status, sens in rows:
        if status == "Approved":
            ids.append(rid)                                   # owner-cleared: always publish
        elif status == "Candidate" and AUTOPUBLISH and sens != "Sensitive":
            ids.append(rid)                                   # auto-publish non-sensitive
        # Candidate + Sensitive, or AUTOPUBLISH off: left for human review
    return ids


def promote(ids):
    """PATCH Status -> Published in batches of 10 (Airtable's per-request limit)."""
    done = 0
    for i in range(0, len(ids), 10):
        batch = ids[i:i + 10]
        body = json.dumps({
            "records": [{"id": rid, "fields": {"Status": "Published"}} for rid in batch]
        }).encode()
        req = urllib.request.Request(API, data=body, headers=HEADERS, method="PATCH")
        resp = json.loads(urllib.request.urlopen(req).read())
        done += len(resp.get("records", []))
    return done


if __name__ == "__main__":
    rows = fetch_pending()
    approved = sum(1 for _, s, _ in rows if s == "Approved")
    auto = sum(1 for _, s, sens in rows if s == "Candidate" and AUTOPUBLISH and sens != "Sensitive")
    held = sum(1 for _, s, sens in rows if s == "Candidate" and sens == "Sensitive")
    print(f"AUTOPUBLISH={'on' if AUTOPUBLISH else 'off'} | "
          f"Approved to publish: {approved} | auto-publish (Safe Candidates): {auto} | "
          f"Sensitive held for review: {held}")
    ids = to_publish(rows)
    if not ids:
        print("Nothing to publish.")
    else:
        n = promote(ids)
        print(f"Published {n} stories -> Published.")
