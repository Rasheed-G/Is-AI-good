"""Health check (v3, for the restart): is the gallery getting new stories, is Airtable near its row cap, do
the live stories' links still work? Also: do the three Groq models we use still exist, is the token that publishes
the site still valid (and not about to expire), and has any source failed several daily runs in a row?
Reads Airtable only, never writes it. No AI calls (listing Groq's models costs nothing).

A serious warning makes the step fail (exit code 1), so GitHub emails a failure notice; broken links only warn.

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
FAIL_STREAK_WARN = 3      # daily runs in a row a source may fail before we warn
TOKEN_WARN_DAYS = 21      # warn this long before the publish token expires
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


def groq_models_missing():
    """Our Groq models that Groq no longer lists (a retired model breaks its stage). [] if all there."""
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return []
    import re, pathlib
    src = (pathlib.Path(__file__).parent / "harvest_reddit.py").read_text(encoding="utf-8")
    ours = re.findall(r'^(?:TRIAGE|JUDGE|COPY)_MODEL\s*=\s*"([^"]+)"', src, re.M)
    req = urllib.request.Request("https://api.groq.com/openai/v1/models",
                                 headers={"Authorization": f"Bearer {key}", "User-Agent": UA})
    try:
        ids = {m.get("id") for m in json.loads(urllib.request.urlopen(req, timeout=30).read()).get("data", [])}
    except Exception as e:
        return [f"(could not list Groq models: {e})"]
    return [m for m in ours if m not in ids]


def publish_token_problem():
    """None if the token that publishes the site works and isn't expiring soon; otherwise what's wrong."""
    tok = os.environ.get("PUBLISH_TOKEN")
    if not tok:
        return None
    req = urllib.request.Request("https://api.github.com/user",
                                 headers={"Authorization": f"Bearer {tok}", "User-Agent": "health-check"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            exp = r.headers.get("github-authentication-token-expiration")   # e.g. "2026-12-01 10:00:00 UTC"
    except urllib.error.HTTPError as e:
        return f"the publish token (PUBLISH_TOKEN) no longer works (GitHub says {e.code}): new stories can't go live"
    except Exception:
        return None
    if exp:
        days = (datetime.date.fromisoformat(exp[:10]) - datetime.date.today()).days
        if days <= TOKEN_WARN_DAYS:
            return f"the publish token (PUBLISH_TOKEN) expires on {exp[:10]} ({days} days): replace it"
    return None


def main():
    airtable_error = None
    try:
        counts = {name: len(rows(t)) for name, t in TABLES.items()}
        live = rows(TABLES["Stories"], ("Date Harvested", "Source URL", "Title"), "{Status}='Published'")
    except Exception as e:                      # e.g. the monthly request limit: still run the other checks
        airtable_error = f"Airtable can't be read ({e})"
        counts, live = {}, []
    total = sum(counts.values())
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
    if not airtable_error and (age is None or age > NEWEST_WARN_DAYS):
        warnings.append(f"newest live story is {age} days old")
    serious = ([airtable_error] if airtable_error else []) + warnings
    missing = groq_models_missing()
    if missing:
        serious.append("Groq model(s) no longer available: " + ", ".join(missing))
    tok = publish_token_problem()
    if tok:
        serious.append(tok)
    failing = {n: k for n, k in pipeline_data.load_source_failures().items() if k >= FAIL_STREAK_WARN}
    if failing:
        serious.append(f"{len(failing)} source(s) failed {FAIL_STREAK_WARN}+ daily runs in a row: "
                       + ", ".join(f"{n} ({k})" for n, k in sorted(failing.items(), key=lambda x: -x[1])))
    warnings = serious + ([f"{len(broken)} of {len(sample)} checked links look broken"] if broken else [])
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
    if serious:
        raise SystemExit(1)   # fail the step so GitHub sends a failure email


if __name__ == "__main__":
    main()
