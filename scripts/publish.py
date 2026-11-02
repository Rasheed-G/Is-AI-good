"""The editorial gate. Promotes stories to Published, then build_data.py ships them.

Run as the first step of the deploy pipeline, before build_data.py:
    source .env.local && python scripts/publish.py && python scripts/build_data.py

MODEL — auto-publish with post-moderation, following the rulebook (sections 4, 6 and 7):
  * Approved (any sensitivity)                  -> Published  (owner's manual clears)
  * Candidate with a Hold reason                -> left as Candidate (held for the owner)
      - a sensitivity hold (risky picture or video, child could be identified) nobody has
        reviewed after "Held-story expiry" weeks -> Archived (kept in Airtable, never shown)
      - "Summary failed" is retried (and archived after 2 tries) by the harvester
  * Candidate with no summary                   -> left as Candidate (never a blank card)
  * Candidate + Safe                            -> Published  (auto, when AUTOPUBLISH is on)
  * Candidate + Sensitive, kept by the judge under the rulebook -> Published (shown hidden by
        default, with the "Sensitive topic" label: rulebook section 6, option A)
  * Candidate re-judged by regrade.py (has a "Judge theme") -> left as Candidate. A re-judge only
        records the judge's view; the owner's review decides, by setting Approved or Rejected.
  * Candidate + Sensitive, judged before the rulebook -> left as Candidate (re-judged in
        release 3 step 5 before any of them can go live)

The owner watches the site and pulls mistakes after the fact by setting a story's Status to
"Rejected" (it then drops from the build and, because dedup scans every row, is never
re-harvested). To release a held card, clear its Hold reason or set it to Approved.

AUTOPUBLISH env toggle (default "1" = on). Set AUTOPUBLISH=0 to fall back to manual review
(only Approved rows publish; Candidates all wait).
"""
import os, json, datetime, urllib.request, urllib.parse
import rulebook

TOKEN = os.environ["AIRTABLE_TOKEN"]
BASE = "appEiqYd3rnYwUNE7"
TABLE = "tblnfUTaXWRD3VYk6"
API = f"https://api.airtable.com/v0/{BASE}/{TABLE}"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

AUTOPUBLISH = os.environ.get("AUTOPUBLISH", "1").lower() not in ("0", "false", "no", "")
SENSITIVE_HOLDS = ("Risky picture or video", "Child could be identified")
FIELDS = ("Status", "Sensitivity", "Hold reason", "Held since", "Rulebook version", "Summary",
          "Judge verdict", "Judge theme")


def fetch_pending():
    """Every Candidate/Approved row, as {"id", "status", "sens", "hold", "held_since", "version", "summary"}."""
    rows, offset = [], None
    formula = urllib.parse.quote("OR({Status}='Approved',{Status}='Candidate')")
    fields = "".join(f"&fields%5B%5D={urllib.parse.quote(f)}" for f in FIELDS)
    while True:
        url = f"{API}?filterByFormula={formula}&pageSize=100{fields}"
        if offset:
            url += f"&offset={offset}"
        req = urllib.request.Request(url, headers=HEADERS)
        data = json.loads(urllib.request.urlopen(req).read())
        for r in data.get("records", []):
            f = r.get("fields", {})
            rows.append({"id": r["id"], "status": f.get("Status"), "sens": f.get("Sensitivity", "Safe"),
                         "hold": f.get("Hold reason"), "held_since": f.get("Held since", ""),
                         "version": f.get("Rulebook version"), "summary": (f.get("Summary") or "").strip(),
                         "verdict": f.get("Judge verdict"), "rejudged": bool(f.get("Judge theme"))})
        offset = data.get("offset")
        if not offset:
            break
    return rows


def decide(row, today=None):
    """"Published", "Archived" or None (leave as it is) for one pending row."""
    if row["status"] == "Approved":
        return "Published"                                # owner-cleared: always publish
    if row["hold"]:
        weeks = rulebook.setting_int("Held-story expiry")
        cutoff = ((today or datetime.date.today()) - datetime.timedelta(weeks=weeks)).isoformat()
        if row["hold"] in SENSITIVE_HOLDS and row["held_since"] and row["held_since"] < cutoff:
            return "Archived"                             # nobody reviewed it in time
        return None
    if row["rejudged"] or not AUTOPUBLISH or not row["summary"]:
        return None
    if row["sens"] != "Sensitive":
        return "Published"                                # auto-publish non-sensitive
    if row["version"] and row["verdict"] == "Keep":
        return "Published"                                # sensitive, passed the rulebook's bar
    return None                                           # sensitive, judged before the rulebook


def set_status(ids, status):
    """PATCH Status in batches of 10 (Airtable's per-request limit). typecast adds 'Archived' if new."""
    done = 0
    for i in range(0, len(ids), 10):
        body = json.dumps({"records": [{"id": rid, "fields": {"Status": status}} for rid in ids[i:i + 10]],
                           "typecast": True}).encode()
        req = urllib.request.Request(API, data=body, headers=HEADERS, method="PATCH")
        done += len(json.loads(urllib.request.urlopen(req).read()).get("records", []))
    return done


if __name__ == "__main__":
    rows = fetch_pending()
    plan = {"Published": [], "Archived": []}
    for row in rows:
        verdict = decide(row)
        if verdict:
            plan[verdict].append(row["id"])
    held = sum(1 for r in rows if r["status"] == "Candidate" and r["hold"])
    old_sensitive = sum(1 for r in rows if r["status"] == "Candidate" and not r["hold"]
                        and r["sens"] == "Sensitive" and not r["version"])
    print(f"AUTOPUBLISH={'on' if AUTOPUBLISH else 'off'} | to publish: {len(plan['Published'])} | "
          f"held for the owner: {held} | expired holds to archive: {len(plan['Archived'])} | "
          f"sensitive, judged before the rulebook (waiting for the re-judge): {old_sensitive}")
    for status, ids in plan.items():
        if ids:
            print(f"{set_status(ids, status)} stories -> {status}.")
    if not any(plan.values()):
        print("Nothing to publish.")
