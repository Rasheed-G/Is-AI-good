"""Re-write the title and summary of PUBLISHED cards with the rulebook's card copy rules.

Uses the same copy prompt as the live harvester (harvest_reddit.write_copy, built from rulebook.md
section 7), so rewritten cards read like new ones: our own title, 2 or 3 sentences of at most 50
words, facts only. Release 3 step 5 runs it on the stories that stay after the re-judge.

Process: re-fetch each story's real article body, ask for our own title + summary, PATCH Title and
Summary. The source's own headline is kept in "Source headline" (filled from the old Title the
first time).

Safety:
  * SKIPS a row (keeps its copy) when the article fetch is blocked or thin (< MIN_BODY chars).
  * SKIPS when the copy can't be written (Groq error or a refusal) — never writes a fallback.
  * FLAGS, and doesn't write, a row whose source headline and summary are about different events
    (rulebook section 7: flagged, not patched).
  * Only Published rows; never touches Rejected/Candidate/Approved.
  * Leaves alone the cards a theme page shows (the owner approved those pages as they are) and the
    "Even the experts are worried" stories (archived in step 5).
  * DRY=1 -> print before/after only, no writes. LIMIT=n -> a random sample of n rows.
  * Calls Groq, so it runs in GitHub Actions (Groq blocks the owner's computer).

Run:  python scripts/rewrite_summaries.py          (writes)
      DRY=1 LIMIT=6 python scripts/rewrite_summaries.py     (preview a sample, no writes)
"""
import os, sys, re, ssl, json, time, html, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

def _load_env():
    p = ROOT / ".env.local"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_load_env()

import harvest_reddit as H
try:
    ssl._create_default_https_context = ssl._create_unverified_context  # Win CA gap
except Exception:
    pass

DRY = os.environ.get("DRY", "").strip().lower() in ("1", "true", "yes", "on")
LIMIT = int(os.environ.get("LIMIT", "0") or 0)
MIN_BODY = 200          # below this the fetch is too thin to rewrite from — keep old copy
COPY_SLEEP = 5          # qwen TPM pacing (8K/min at ~650 tok/call)
HDRS = {"Authorization": f"Bearer {H.AIRTABLE_TOKEN}"}
API = f"https://api.airtable.com/v0/{H.BASE}/{H.TABLE}"
BACKUP = ROOT / "scripts" / "summary_backup.json"   # {id: {title, summary}} — restore point

# When the fetch returns the WRONG page (nav boilerplate, a different story, a paywall), the
# model rightly refuses and emits a META-comment ABOUT the source instead of a summary. Those
# are not card copy — skip them and keep the existing summary. The markers + detector now live
# in harvest_reddit (single source of truth), so the live path and this bulk rewrite agree.
looks_like_meta = H.looks_like_meta


def theme_page_ids():
    """Story ids the theme pages show (data-ids="rec…,rec…" in site/impact/*/index.html)."""
    ids = set()
    for page in (ROOT / "site").glob("impact/*/index.html"):
        for m in re.finditer(r'data-ids="([^"]*)"', page.read_text(encoding="utf-8")):
            ids.update(i.strip() for i in m.group(1).split(",") if i.strip())
    return ids


def published_rows():
    rows, offset = [], None
    while True:
        url = (API + "?pageSize=100&filterByFormula=" +
               "%7BStatus%7D%3D%27Published%27" + (f"&offset={offset}" if offset else ""))
        data = H.http_json(url, headers=HDRS)
        rows.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    return rows


def fetch_body(url):
    """Article text, the same way the judge and the live copy step read it (no site menus)."""
    body = H._article_text(url)
    return body, ("ok" if body else "empty")


def patch(batch):
    body = json.dumps({"records": batch, "typecast": True}).encode()
    hdrs = {**HDRS, "Content-Type": "application/json"}
    H.http_json(API, headers=hdrs, data=body, method="PATCH")


def main():
    keep_as_is = theme_page_ids()
    rows = [r for r in published_rows() if r["id"] not in keep_as_is
            and r.get("fields", {}).get("Theme") != "Even the experts are worried"]
    if LIMIT and LIMIT < len(rows):
        import random
        rows = random.Random(1).sample(rows, LIMIT)
    print(f"Loaded {len(rows)} Published rows{'  (DRY RUN — no writes)' if DRY else ''}\n")
    # Save every current title and summary first so any/all rewrites can be restored.
    backup = {r["id"]: {"title": r.get("fields", {}).get("Title") or "",
                        "summary": r.get("fields", {}).get("Summary") or ""} for r in rows}
    BACKUP.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
    updates = []
    stats = {"rewritten": 0, "skip_thin": 0, "skip_copy": 0, "flag_event": 0}

    for r in rows:
        f = r.get("fields", {})
        title = (f.get("Title") or "").strip()
        headline = (f.get("Source headline") or "").strip() or title
        old = (f.get("Summary") or "").strip()
        src = (f.get("Source URL") or "").strip()

        body, fstatus = fetch_body(src)
        short = f"[{r['id']}] {title[:48]!r}"
        if len(body) < MIN_BODY:
            stats["skip_thin"] += 1
            print(f"  · SKIP (thin fetch {fstatus}, {len(body)}c) {short}")
            continue

        post = {"title": headline, "subreddit": None, "external_url": src, "created": f.get("Date", ""),
                "provenance": f.get("Source type", "") or "", "source_label": f.get("Source name", "") or ""}
        new_title, new, same_event, _tok = H.write_copy(post, body)
        time.sleep(COPY_SLEEP)
        if not new:
            stats["skip_copy"] += 1
            print(f"  ! SKIP (copy not written: error or refusal) {short}")
            continue
        if not same_event:
            stats["flag_event"] += 1
            print(f"  ⚑ FLAG (headline and summary about different events, not written) {short}\n"
                  f"      HEADLINE: {headline}\n      SUMMARY:  {new}")
            continue

        stats["rewritten"] += 1
        print(f"  ✎ {short}")
        print(f"      OLD: {title}\n           {old} ({len(old.split())}w)")
        print(f"      NEW: {new_title}\n           {new} ({len(new.split())}w)\n")
        fx = {"Title": new_title[:250], "Summary": new}
        if not f.get("Source headline"):
            fx["Source headline"] = headline[:250]
        updates.append({"id": r["id"], "fields": fx})

    print(f"\n{stats['rewritten']} to rewrite · skipped: thin fetch {stats['skip_thin']}, "
          f"copy not written {stats['skip_copy']} · flagged (different events) {stats['flag_event']}"
          f"   (old copy backed up → {BACKUP.name})")

    if DRY:
        print("DRY RUN — nothing written.")
        return
    for i in range(0, len(updates), 10):
        patch(updates[i:i + 10])
    print(f"Wrote {len(updates)} title and summary updates to Airtable.")


if __name__ == "__main__":
    main()
