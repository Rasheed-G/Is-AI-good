"""One-off: re-write every PUBLISHED card summary in the new Stage-C voice.

Why: early summaries were rich; a later batch came out thin (6-13 words). This re-runs
Stage C on existing Published rows with the LOCKED copy prompt (harvest_reddit.copy_prompt
— the same prompt the live harvester now uses), so back-filled cards read identically to
newly-harvested ones: plain-language, harm/risk-forward, with the concrete 'so what'.

Process (option ii — same as the live pipeline): re-fetch each story's real article body,
feed title+body to qwen via the shared prompt, PATCH the Airtable Summary.

Safety:
  * SKIPS a row (keeps its existing summary) when the article fetch is blocked or thin
    (< MIN_BODY chars) — protects the ~8-10 hard bot-blocked sources from a hollow rewrite.
  * SKIPS on any Groq non-'ok' status — never writes a fail-safe/echo over good copy.
  * Only Published rows; never touches Rejected/Candidate/Approved.
  * DRY=1 -> print before/after only, no writes.
  * Idempotent enough to re-run (temp 0.2 → near-stable output).

Run:  python scripts/rewrite_summaries.py          (writes)
      DRY=1 python scripts/rewrite_summaries.py     (preview, no writes)
"""
import os, sys, re, ssl, json, time, html, pathlib, urllib.error

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
MIN_BODY = 200          # below this the fetch is too thin to rewrite from — keep old copy
COPY_SLEEP = 5          # qwen TPM pacing (8K/min at ~650 tok/call)
HDRS = {"Authorization": f"Bearer {H.AIRTABLE_TOKEN}"}
API = f"https://api.airtable.com/v0/{H.BASE}/{H.TABLE}"
BACKUP = ROOT / "scripts" / "summary_backup.json"   # {id: old summary} — restore point

# When the fetch returns the WRONG page (nav boilerplate, a different story, a paywall),
# the model rightly refuses and emits a META-comment ABOUT the source instead of a summary.
# Those are not card copy — skip them and keep the existing summary. Match tell-tale phrases
# that never appear in a real card summary (which describes the event directly).
META_MARKERS = (
    "provided source", "source text", "the source text", "no factual summary",
    "cannot be generated", "can be generated", "no summary can", "does not illustrate",
    "not an ai risk", "not an ai incident", "not an ai story", "website navigation",
    "boilerplate", "this item describes", "this does not", "lacks a specific",
    "no specific incident", "insufficient information", "unable to summarize",
)


def looks_like_meta(text):
    t = text.lower()
    return any(m in t for m in META_MARKERS)


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
    """Article body text (og:description + <p> text), mirroring the copy test harness."""
    try:
        raw = H.http_text(url, headers={"User-Agent": H.BROWSER_UA})
    except Exception as e:
        return "", f"fetch-fail:{getattr(e, 'code', e)}"
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    m = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\']'
                  r'[^>]+content=["\']([^"\']+)', raw, re.I)
    desc = html.unescape(m.group(1)) if m else ""
    paras = re.findall(r"(?is)<p[^>]*>(.*?)</p>", raw)
    text = " ".join(html.unescape(re.sub(r"(?s)<[^>]+>", " ", p)) for p in paras)
    body = re.sub(r"\s+", " ", (desc + " " + text).strip())
    return body, ("ok" if body else "empty")


def patch(batch):
    body = json.dumps({"records": batch, "typecast": True}).encode()
    hdrs = {**HDRS, "Content-Type": "application/json"}
    H.http_json(API, headers=hdrs, data=body, method="PATCH")


def main():
    rows = published_rows()
    print(f"Loaded {len(rows)} Published rows{'  (DRY RUN — no writes)' if DRY else ''}\n")
    # Save every current summary first so any/all rewrites can be restored.
    backup = {r["id"]: (r.get("fields", {}).get("Summary") or "") for r in rows}
    BACKUP.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
    updates = []
    stats = {"rewritten": 0, "skip_thin": 0, "skip_groq": 0, "skip_same": 0, "skip_meta": 0}

    for r in rows:
        f = r.get("fields", {})
        title = (f.get("Title") or "").strip()
        old = (f.get("Summary") or "").strip()
        src = (f.get("Source URL") or "").strip()
        prov = f.get("Source type", "") or ""
        sname = f.get("Source name", "") or ""

        body, fstatus = fetch_body(src)
        short = f"[{r['id']}] {title[:48]!r}"
        if len(body) < MIN_BODY:
            stats["skip_thin"] += 1
            print(f"  · SKIP (thin fetch {fstatus}, {len(body)}c) {short}")
            continue

        post = {"title": title, "subreddit": None,
                "provenance": prov, "source_label": sname}
        prompt = H.copy_prompt(post, body)
        content, tok, gstatus = H.groq_chat(H.COPY_MODEL, prompt, json_mode=False,
                                            reasoning_effort="none")
        time.sleep(COPY_SLEEP)
        if gstatus != "ok" or not content or not content.strip():
            stats["skip_groq"] += 1
            print(f"  ! SKIP (groq {gstatus}) {short}")
            continue
        new = content.strip().strip('"').strip()
        if looks_like_meta(new):
            stats["skip_meta"] += 1
            print(f"  ! SKIP (meta/refusal — wrong page fetched) {short}\n      {new[:120]}")
            continue
        if new == old:
            stats["skip_same"] += 1
            continue

        stats["rewritten"] += 1
        print(f"  ✎ {short}")
        print(f"      OLD ({len(old.split())}w): {old}")
        print(f"      NEW ({len(new.split())}w): {new}\n")
        updates.append({"id": r["id"], "fields": {"Summary": new}})

    print(f"\n{stats['rewritten']} to rewrite · skipped: thin-fetch {stats['skip_thin']}, "
          f"meta/refusal {stats['skip_meta']}, groq {stats['skip_groq']}, "
          f"unchanged {stats['skip_same']}   (old summaries backed up → {BACKUP.name})")

    if DRY:
        print("DRY RUN — nothing written.")
        return
    for i in range(0, len(updates), 10):
        patch(updates[i:i + 10])
    print(f"Wrote {len(updates)} summary updates to Airtable.")


if __name__ == "__main__":
    main()
