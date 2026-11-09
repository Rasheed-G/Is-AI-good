"""Health check (v3, for the restart): is the gallery getting new stories, is Airtable near its row cap, do
the live stories' links still work? Reads Airtable only, never writes it. No AI.

Prints a short report and saves it to pipeline-data/health/DATE.json. Runs after Sunday's pick, or by hand:
    python scripts/health_check.py
"""
import os, json, random, datetime, urllib.request, urllib.parse, urllib.error
import pipeline_data

TOKEN = os.environ["AIRTABLE_TOKEN"]
BASE = "appEiqYd3rnYwUNE7"
TABLES = {"Stories": "tblnfUTaXWRD3VYk6", "Sources": "tbllWd5Rby0pkgIpB", "Runs": "tblR1wvxB46FtwMia",
          "Gate keywords": "tblvUIs01rOGsMpnC"}
ROW_CAP = 1000            # Airtable free plan: records per base
WARN_AT = 900
NEWEST_WARN_DAYS = 10     # a weekly pick should never leave the gallery this long without a new story
LINK_SAMPLE = 30
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")


def rows(table, fields=(), formula=None):
    out, offset = [], None
    while True:
        u = (f"https://api.airtable.com/v0/{BASE}/{table}?pageSize=100"
             + "".join(f"&fields%5B%5D={urllib.parse.quote(f)}" for f in fields)
             + (f"&filterByFormula={urllib.parse.quote(formula)}" if formula else "")
             + (f"&offset={offset}" if offset else ""))
        req = urllib.request.Request(u, headers={"Authorization": f"Bearer {TOKEN}"})
        data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        out.extend(r.get("fields", {}) for r in data.get("records", []))
        offset = data.get("offset")
        if not offset:
            return out


def link_ok(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status < 400
    except urllib.error.HTTPError as e:
        return e.code in (401, 403, 429)        # blocked to robots, not broken
    except Exception:
        return False


def main():
    counts = {name: len(rows(t)) for name, t in TABLES.items()}
    total = sum(counts.values())
    live = rows(TABLES["Stories"], ("Date Harvested", "Source URL", "Title"), "{Status}='Published'")
    newest = max((f.get("Date Harvested") or "")[:10] for f in live) if live else ""
    age = (datetime.date.today() - datetime.date.fromisoformat(newest)).days if newest else None
    sample = random.sample(live, min(LINK_SAMPLE, len(live)))
    broken = [f.get("Source URL") for f in sample if f.get("Source URL") and not link_ok(f["Source URL"])]
    report = {"date": pipeline_data.today(), "rows": counts, "rows_total": total, "row_cap": ROW_CAP,
              "live_stories": len(live), "newest_story": newest, "newest_age_days": age,
              "links_checked": len(sample), "links_broken": broken}
    warnings = []
    if total >= WARN_AT:
        warnings.append(f"Airtable has {total} rows of {ROW_CAP}: archive before it fills")
    if age is None or age > NEWEST_WARN_DAYS:
        warnings.append(f"newest live story is {age} days old")
    if broken:
        warnings.append(f"{len(broken)} of {len(sample)} checked links look broken")
    report["warnings"] = warnings
    p = pipeline_data._path(f"health/{report['date']}.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"Health check {report['date']}: {total}/{ROW_CAP} Airtable rows ({counts}); {len(live)} live stories, "
          f"newest {newest} ({age} days); {len(broken)}/{len(sample)} sampled links broken.")
    for w in warnings:
        print(f"  ! {w}")
    for u in broken:
        print(f"    broken: {u}")


if __name__ == "__main__":
    main()
