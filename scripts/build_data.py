"""Fetch Published stories from Airtable and write site/stories.json and site/themes.json.

The Airtable token is read from the environment (never shipped to the browser).
Run:  source .env.local && python scripts/build_data.py

Rules come from rulebook.md:
  * Only stories in a rulebook theme (or with no theme yet) are shown. A theme that left the rulebook
    disappears from the site at once; its stories stay in Airtable until they're archived.
  * Gallery rules (section 4): at most "Gallery cap" stories per theme, none older than "Age limit"
    months. Featured cards and cards a theme page uses are exempt. Switched on with GALLERY_LIMITS=1.
  * With GALLERY_ARCHIVE=1 as well, a story the limits leave out is archived, then removed from Airtable:
    every field is saved as JSON to the archive repo (ARCHIVE_REPO, ARCHIVE_PATH, ARCHIVE_TOKEN), and only
    once that save is confirmed are the rows deleted. If the save fails, nothing is deleted (the stories
    are just left out of the site). More than ARCHIVE_MAX (default 50) in one run: nothing is deleted.
  * site/themes.json carries each theme's public summary, for the news page.
"""
import os, re, json, datetime, urllib.request, urllib.error, urllib.parse, pathlib
import rulebook

TOKEN = os.environ["AIRTABLE_TOKEN"]
BASE = "appEiqYd3rnYwUNE7"
TABLE = "tblnfUTaXWRD3VYk6"

SITE = pathlib.Path(__file__).resolve().parent.parent / "site"
OUT = SITE / "stories.json"
THEMES_OUT = SITE / "themes.json"

def _on(name):
    return os.environ.get(name, "0").strip().lower() in ("1", "true", "yes", "on")

GALLERY_LIMITS = _on("GALLERY_LIMITS")
GALLERY_ARCHIVE = _on("GALLERY_ARCHIVE")
ARCHIVE_REPO = os.environ.get("ARCHIVE_REPO", "").strip()            # owner/name
ARCHIVE_PATH = (os.environ.get("ARCHIVE_PATH") or "backups/gallery-limits").strip().strip("/")
ARCHIVE_TOKEN = os.environ.get("ARCHIVE_TOKEN", "").strip()
ARCHIVE_MAX = int(os.environ.get("ARCHIVE_MAX", "50"))

RAW = {}   # record id -> the full Airtable record, for the archive

def fetch_published():
    stories, offset = [], None
    while True:
        url = (f"https://api.airtable.com/v0/{BASE}/{TABLE}"
               "?filterByFormula=" + urllib.parse.quote("{Status}='Published'") +
               "&pageSize=100")
        if offset:
            url += f"&offset={offset}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
        data = json.loads(urllib.request.urlopen(req).read())
        for r in data.get("records", []):
            f = r.get("fields", {})
            RAW[r["id"]] = r
            # Only public-facing fields — internal ones (Relevance, Source type,
            # Editor notes) are deliberately left out so they never reach the browser.
            # "_score" is used for the gallery cap below and dropped before writing.
            stories.append({
                "id": r["id"],
                "title": f.get("Title", ""),
                "theme": f.get("Theme", ""),
                "format": f.get("Format", "Text"),
                "summary": f.get("Summary", ""),
                "sourceUrl": f.get("Source URL", ""),
                "mediaUrl": f.get("Media URL", ""),
                "date": f.get("Date", ""),
                "sensitivity": f.get("Sensitivity", "Safe"),
                "featured": bool(f.get("Featured", False)),
                "_score": f.get("Overall score", f.get("Relevance", 0)) or 0,
            })
        offset = data.get("offset")
        if not offset:
            break
    return stories

def theme_page_ids():
    """Story ids the theme pages show (data-ids="rec…,rec…" in site/impact/*/index.html)."""
    ids = set()
    for page in SITE.glob("impact/*/index.html"):
        for m in re.finditer(r'data-ids="([^"]*)"', page.read_text(encoding="utf-8")):
            ids.update(i.strip() for i in m.group(1).split(",") if i.strip())
    return ids

def in_rulebook_theme(stories):
    names = set(rulebook.theme_names())
    kept = [s for s in stories if not s["theme"] or s["theme"] in names]
    gone = len(stories) - len(kept)
    if gone:
        print(f"Left out {gone} stories whose theme is not in the rulebook.")
    return kept

def gallery_limits(stories, today=None):
    """Rulebook section 4: the best `cap` per theme, nothing older than `months` (exemptions kept)."""
    cap = rulebook.setting_int("Gallery cap")
    months = rulebook.setting_int("Age limit")
    today = today or datetime.date.today()
    cutoff = (today - datetime.timedelta(days=round(months * 30.44))).isoformat()
    exempt_ids = theme_page_ids()
    exempt = lambda s: s["featured"] or s["id"] in exempt_ids
    by_theme, out, left_out = {}, [], []
    for s in stories:
        if exempt(s):
            out.append(s)
        elif (s["date"] or "0000") < cutoff:
            left_out.append((s, f"older than {months} months (before {cutoff})"))
        else:
            by_theme.setdefault(s["theme"], []).append(s)
    for theme, items in by_theme.items():
        room = cap - sum(1 for s in out if s["theme"] == theme)   # exempt cards use up places first
        items.sort(key=lambda s: (s["_score"], s["date"] or ""), reverse=True)   # weakest last
        keep = items[:max(room, 0)] if theme else items          # no theme yet: no cap to apply
        out.extend(keep)
        left_out += [(s, f"over the cap of {cap} in {theme}") for s in items[len(keep):]]
    too_old = sum(1 for _, why in left_out if why.startswith("older"))
    print(f"Gallery limits: {too_old} older than {months} months, "
          f"{len(left_out) - too_old} over the cap of {cap} per theme.")
    return out, left_out

def _github(method, path, body=None):
    req = urllib.request.Request(f"https://api.github.com/repos/{ARCHIVE_REPO}/contents/{path}",
                                 data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": f"Bearer {ARCHIVE_TOKEN}",
                                          "Accept": "application/vnd.github+json"})
    return json.loads(urllib.request.urlopen(req).read())

def save_archive(records):
    """Add the records to today's archive file in the archive repo. True only once GitHub confirms it."""
    import base64
    path = f"{ARCHIVE_PATH}/{datetime.date.today().isoformat()}.json"
    existing, sha = [], None
    try:
        cur = _github("GET", path)
        sha = cur["sha"]
        existing = json.loads(base64.b64decode(cur["content"]).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    data = existing + records
    body = {"message": f"Archive {len(records)} stories left out by the gallery limits",
            "content": base64.b64encode(json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")).decode()}
    if sha:
        body["sha"] = sha
    res = _github("PUT", path, body)
    return bool(res.get("content", {}).get("sha"))

def delete_rows(ids):
    for i in range(0, len(ids), 10):
        q = "&".join("records[]=" + urllib.parse.quote(r) for r in ids[i:i + 10])
        req = urllib.request.Request(f"https://api.airtable.com/v0/{BASE}/{TABLE}?{q}", method="DELETE",
                                     headers={"Authorization": f"Bearer {TOKEN}"})
        urllib.request.urlopen(req).read()

def archive_and_remove(left_out):
    """Archive every field of the left-out stories, then delete them from Airtable (only after the save)."""
    if not left_out:
        return
    if not (ARCHIVE_REPO and ARCHIVE_TOKEN):
        print(f"WARNING: archive repo or token not set; {len(left_out)} left-out stories kept in Airtable.")
        return
    if len(left_out) > ARCHIVE_MAX:
        print(f"WARNING: {len(left_out)} stories to archive is over the safety limit of {ARCHIVE_MAX}; "
              "nothing deleted. Raise ARCHIVE_MAX for one run if this is expected.")
        return
    records = [dict(RAW[s["id"]], archived_because=why) for s, why in left_out]
    try:
        saved = save_archive(records)
    except Exception as e:   # any failure: keep the rows
        print(f"WARNING: archive save failed ({e}); nothing deleted.")
        return
    if not saved:
        print("WARNING: archive save not confirmed; nothing deleted.")
        return
    delete_rows([r["id"] for r in records])
    print(f"Archived and removed {len(records)} stories from Airtable.")

def write_themes():
    data = {"version": rulebook.version(),
            "themes": [{"name": t["name"], "slug": t["slug"], "summary": t["summary"]} for t in rulebook.themes()]}
    THEMES_OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(data['themes'])} themes -> {THEMES_OUT}")

if __name__ == "__main__":
    stories = in_rulebook_theme(fetch_published())
    if GALLERY_LIMITS:
        stories, left_out = gallery_limits(stories)
        if GALLERY_ARCHIVE:
            archive_and_remove(left_out)
    for s in stories:
        s.pop("_score", None)
    # Featured first, then newest date first, then title
    stories.sort(key=lambda s: (not s["featured"], s.get("date") or "0000", s["title"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"stories": stories}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(stories)} published stories -> {OUT}")
    write_themes()
