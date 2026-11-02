"""The pipeline's own working files (v3): not stories for the gallery, so they stay out of Airtable and its
1,000-row cap. In GitHub Actions they live on the `pipeline-data` branch, checked out into PIPELINE_DATA.

  seen.json             every link the triage or judge already looked at (kept 60 days), so a story found again
                        tomorrow (the look-back overlaps) is never paid for twice
  queue.json            stories that passed the bar this week, waiting for Sunday's pick
  triage-drops/DATE.csv what triage threw away each day: headline, snippet, link (owner, 2026-11-02)
  not-picked/DATE.json  passes the weekly pick left out, kept for checks instead of in Airtable
"""
import os, json, csv, pathlib, datetime

DIR = pathlib.Path(os.environ.get("PIPELINE_DATA") or pathlib.Path(__file__).resolve().parent.parent / "pipeline-data")
SEEN_DAYS = 60
BODY_KEEP = 3000          # characters of article text kept per queued story (the judge and copy read 1,500)


def _path(name):
    DIR.mkdir(parents=True, exist_ok=True)
    return DIR / name


def _read(name, empty):
    p = _path(name)
    if not p.exists():
        return empty
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return empty


def _write(name, data):
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def today():
    return datetime.date.today().isoformat()


# --- seen links -------------------------------------------------------------------
def load_seen():
    cut = (datetime.date.today() - datetime.timedelta(days=SEEN_DAYS)).isoformat()
    return {u: d for u, d in _read("seen.json", {}).items() if d >= cut}


def save_seen(seen, posts, keys):
    """Add every post's links (keys(post) -> set of urls) to the seen list and save it."""
    t = today()
    for p in posts:
        for u in keys(p):
            seen.setdefault(u, t)
    _write("seen.json", seen)


# --- the week's queue -------------------------------------------------------------
def slim(post):
    """A post small enough to store: the fields the weekly pick and card copy need."""
    keep = ("title", "selftext", "permalink", "external_url", "provenance", "source_label", "subreddit",
            "created", "over_18", "feed_image")
    out = {k: post.get(k) for k in keep if k in post}
    out["selftext"] = (out.get("selftext") or "")[:BODY_KEEP]
    return out


def load_queue():
    return _read("queue.json", [])


def save_queue(queue):
    _write("queue.json", queue)


# --- triage drops and leftovers ---------------------------------------------------
def save_triage_drops(dropped, snippet_chars=300):
    """Headline, snippet and link of everything triage dropped today (appends if the day runs twice)."""
    if not dropped:
        return None
    p = _path(f"triage-drops/{today()}.csv")
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["Headline", "Snippet", "Link", "Source"])
        for d in dropped:
            w.writerow([(d.get("title") or "")[:300], " ".join((d.get("selftext") or "").split())[:snippet_chars],
                        d.get("external_url") or d.get("permalink") or "", d.get("source_label") or ""])
    return p


def save_not_picked(items):
    if items:
        _write(f"not-picked/{today()}.json", items)
