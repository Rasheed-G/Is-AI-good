"""One-off backfill: apply the harvester's card enrichment to rows ALREADY in Airtable.

Rows written before the enrichment code (enrich_media / story_source_url) are bare —
plain Text cards with no heading image/video, and Reddit link-posts still pointing at
the Reddit thread instead of the original article. This script re-runs the SAME
enrichment on existing rows, reusing the harvester's functions verbatim (no Groq —
enrichment is plain page-fetch + og-tag parsing).

Safety:
  * Only ever ADDS a Media URL / upgrades Format / repoints a Source URL when
    enrichment actually finds something. Never blanks an existing value, never
    clobbers a row that already has an embed.
  * Skips Rejected rows.
  * Idempotent — safe to re-run (a row that already has media is left untouched).
  * DRY=1 -> report only, no writes.

Run:  set -a; source .env.local; set +a; python scripts/backfill_enrich.py
      (DRY=1 python scripts/backfill_enrich.py  for a no-write preview)
"""
import os, sys, json, time, ssl, pathlib, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET

# Load secrets from .env.local into the environment (harvest_reddit reads them at import
# time). We set os.environ directly and never print any value — same effect as `source`,
# but keeps the shell command plain.
def _load_env_local():
    p = pathlib.Path(__file__).resolve().parent.parent / ".env.local"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env_local()

import harvest_reddit as H  # reuse the exact enrichment logic

# Windows Python often ships without a complete CA bundle, so some news sites (e.g. CNN)
# fail cert verification on an otherwise-fine page. We only GET public pages to read
# their og:image/og:video meta tags — no credentials or data are sent — so for this
# one-off local backfill we fall back to an unverified context to recover those pages.
try:
    ssl._create_default_https_context = ssl._create_unverified_context
except Exception:
    pass

DRY = os.environ.get("DRY", "").strip().lower() in ("1", "true", "yes", "on")
HDRS = {"Authorization": f"Bearer {H.AIRTABLE_TOKEN}"}
API = f"https://api.airtable.com/v0/{H.BASE}/{H.TABLE}"


def all_rows():
    rows, offset = [], None
    while True:
        url = API + "?pageSize=100" + (f"&offset={offset}" if offset else "")
        data = H.http_json(url, headers=HDRS)
        rows.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    return rows


def reddit_external(permalink):
    """Recover the outbound article link of an old Reddit link-post by re-fetching the
    thread. Uses the thread's **.rss** endpoint, NOT /.json: Reddit's JSON API returns
    403 to non-datacenter IPs (this machine), but the .rss feed serves fine. The first
    <entry> is the submission itself; its <content> carries the outbound link, which we
    extract with the harvester's own _pick_external (clip-aware, reddit-links stripped) so
    recovery mirrors the live harvest exactly. Returns "" for self-posts (no external
    href), deleted threads, or any failure."""
    base = permalink.split("?")[0].rstrip("/")
    for attempt in range(3):
        try:
            xml = H.http_text(base + ".rss", headers={"User-Agent": H.UA})
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(30 * (attempt + 1))   # Reddit RSS throttles rapid calls (30s, 60s)
                continue
            print(f"    · reddit re-fetch skipped ({e.code})")
            return ""
        except Exception as e:
            print(f"    · reddit re-fetch skipped ({getattr(e, 'code', e)})")
            return ""
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return ""
        ATOM = "{http://www.w3.org/2005/Atom}"
        entry = root.find(ATOM + "entry")           # first entry = the submission
        if entry is None:
            return ""
        content = entry.find(ATOM + "content")
        ext = H._pick_external(content.text or "" if content is not None else "")
        return "" if H.is_ip_url(ext) else ext   # never repoint an existing row to a raw-IP spam link
    return ""


def patch(batch):
    body = json.dumps({"records": batch, "typecast": True}).encode()
    hdrs = {**HDRS, "Content-Type": "application/json"}
    H.http_json(API, headers=hdrs, data=body, method="PATCH")


def main():
    rows = all_rows()
    print(f"Loaded {len(rows)} rows{'  (DRY RUN — no writes)' if DRY else ''}\n")
    updates, stats = [], {"img": 0, "vid": 0, "repoint": 0, "reddit_fetched": 0}

    for r in rows:
        f = r.get("fields", {})
        if f.get("Status") == "Rejected":
            continue
        prov = f.get("Source type", "") or ""
        title = f.get("Title", "") or ""
        cur_fmt = (f.get("Format") or "Text")
        cur_media = (f.get("Media URL") or "").strip()
        cur_src = (f.get("Source URL") or "").strip()

        external = ""
        # Old Reddit link-posts: recover the article URL so we can repoint + enrich it.
        # Only worth the network hit when the card is still bare (no media yet).
        if prov == "Reddit" and "reddit.com" in cur_src.lower() and not cur_media:
            external = reddit_external(cur_src)
            if external:
                stats["reddit_fetched"] += 1
            time.sleep(25)  # be gentle on Reddit's public RSS (it 429s rapid callers)

        p = {"external_url": external, "permalink": cur_src, "provenance": prov,
             "title": title, "over_18": False}

        new_src = H.story_source_url(p) or cur_src
        fmt, media = cur_fmt, cur_media
        if not media:
            if external:                       # a recovered link might itself be a clip
                fmt, media = H.detect_format_and_media(p)
            fmt, media = H.enrich_media(p, fmt, media)

        changes = {}
        if new_src and new_src != cur_src:
            changes["Source URL"] = new_src
            stats["repoint"] += 1
        if media and media != cur_media:
            changes["Media URL"] = media
            changes["Format"] = fmt
            stats["vid" if fmt == "Video" else "img"] += 1

        if changes:
            tag = " / ".join(f"{k}={str(v)[:60]}" for k, v in changes.items())
            print(f"  ✎ {title[:50]!r}\n      {tag}")
            updates.append({"id": r["id"], "fields": changes})

    print(f"\n{len(updates)} rows to update — "
          f"images:{stats['img']} videos:{stats['vid']} repointed:{stats['repoint']} "
          f"(reddit re-fetched:{stats['reddit_fetched']})")

    if DRY:
        print("DRY RUN — nothing written.")
        return
    for i in range(0, len(updates), 10):
        patch(updates[i:i + 10])
    print(f"Wrote {len(updates)} updates to Airtable.")


if __name__ == "__main__":
    main()
