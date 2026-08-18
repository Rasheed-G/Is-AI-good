"""Fetch Published stories from Airtable and write site/stories.json.

The Airtable token is read from the environment (never shipped to the browser).
Run:  source .env.local && python scripts/build_data.py
"""
import os, json, urllib.request, urllib.error, urllib.parse, pathlib

TOKEN = os.environ["AIRTABLE_TOKEN"]
BASE = "appEiqYd3rnYwUNE7"
TABLE = "tblnfUTaXWRD3VYk6"

OUT = pathlib.Path(__file__).resolve().parent.parent / "site" / "stories.json"

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
            })
        offset = data.get("offset")
        if not offset:
            break
    return stories

if __name__ == "__main__":
    stories = fetch_published()
    # Featured first, then newest date first, then title
    stories.sort(key=lambda s: (not s["featured"], s.get("date") or "0000", s["title"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"stories": stories}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(stories)} published stories -> {OUT}")
