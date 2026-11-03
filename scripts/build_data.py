"""Fetch Published stories from Airtable and write site/stories.json and site/themes.json.

The Airtable token is read from the environment (never shipped to the browser).
Run:  source .env.local && python scripts/build_data.py

Rules come from rulebook.md:
  * Only stories in a rulebook theme (or with no theme yet) are shown. A theme that left the rulebook
    disappears from the site at once; its stories stay in Airtable until they're archived.
  * Gallery rules (section 4): at most "Gallery cap" stories per theme, none older than "Age limit"
    months. Featured cards and cards a theme page uses are exempt. Nothing is deleted: stories over the
    limits are just left out of the site. Switched on with GALLERY_LIMITS=1 (off until the live stories
    have been re-judged under the rulebook, release 3 step 5).
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

GALLERY_LIMITS = os.environ.get("GALLERY_LIMITS", "0").strip().lower() in ("1", "true", "yes", "on")

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
    by_theme, out, too_old, over_cap = {}, [], 0, 0
    for s in stories:
        if exempt(s):
            out.append(s)
        elif (s["date"] or "0000") < cutoff:
            too_old += 1
        else:
            by_theme.setdefault(s["theme"], []).append(s)
    for theme, items in by_theme.items():
        room = cap - sum(1 for s in out if s["theme"] == theme)   # exempt cards use up places first
        items.sort(key=lambda s: (s["_score"], s["date"] or ""), reverse=True)   # weakest last
        keep = items[:max(room, 0)] if theme else items          # no theme yet: no cap to apply
        out.extend(keep)
        over_cap += len(items) - len(keep)
    print(f"Gallery limits: {too_old} older than {months} months, {over_cap} over the cap of {cap} per theme.")
    return out

def write_themes():
    data = {"version": rulebook.version(),
            "themes": [{"name": t["name"], "slug": t["slug"], "summary": t["summary"]} for t in rulebook.themes()]}
    THEMES_OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(data['themes'])} themes -> {THEMES_OUT}")

if __name__ == "__main__":
    stories = in_rulebook_theme(fetch_published())
    if GALLERY_LIMITS:
        stories = gallery_limits(stories)
    for s in stories:
        s.pop("_score", None)
    # Featured first, then newest date first, then title
    stories.sort(key=lambda s: (not s["featured"], s.get("date") or "0000", s["title"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"stories": stories}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(stories)} published stories -> {OUT}")
    write_themes()
