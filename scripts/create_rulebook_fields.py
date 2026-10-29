"""One-off: create the Airtable columns the rulebook pipeline writes (release 3, step 4).

Stories: the judge's scores per rulebook criterion, overall score, verdict and reason, the rulebook version
it was judged under, the source's own headline (cards now carry our own title), and the hold fields
(why a card is held, since when, how many summary tries). Step 5 adds the re-judge's theme pick, sensitivity,
hold and whether it read the page, plus the owner's labels (Step 5 sample, Owner label, Owner reason).
Runs: a count per pipeline stage, and a sample of what triage dropped.
Sources: how many of each source's items the judge read (next to Found and Kept).

Idempotent: skips any field that already exists. Airtable, not Groq (no IP block).
Run: source .env.local && python scripts/create_rulebook_fields.py
"""
import os, json, urllib.request, urllib.error

BASE = "appEiqYd3rnYwUNE7"
STORIES, RUNS, SOURCES = "tblnfUTaXWRD3VYk6", "tblR1wvxB46FtwMia", "tbllWd5Rby0pkgIpB"
TOKEN = os.environ["AIRTABLE_TOKEN"]

NUM = {"type": "number", "options": {"precision": 0}}
TEXT = {"type": "singleLineText"}
LONG = {"type": "multilineText"}
DATE = {"type": "date", "options": {"dateFormat": {"name": "iso"}}}
CHECK = {"type": "checkbox", "options": {"icon": "check", "color": "greenBright"}}
def select(*names):
    return {"type": "singleSelect", "options": {"choices": [{"name": n} for n in names]}}

FIELDS = {
    STORIES: [
        ("Score: credibility", NUM), ("Score: consequence", NUM), ("Score: significance", NUM),
        ("Score: audience", NUM), ("Score: theme fit", NUM), ("Overall score", NUM),
        ("Judge verdict", select("Keep", "Weak", "Reject")),
        ("Judge reason", LONG),
        ("Rulebook version", NUM),
        ("Source headline", TEXT),
        ("Hold reason", select("Risky picture or video", "Child could be identified",
                               "Summary failed", "Title and summary differ")),
        ("Held since", DATE),
        ("Summary tries", NUM),
        # Release 3 step 5 (re-judge of the stored stories): what the judge said that isn't applied
        # straight away (the owner sees theme moves first), and the owner's own labels.
        ("Judge theme", TEXT),
        ("Judge sensitive", CHECK),
        ("Judge hold", select("Risky picture or video", "Child could be identified")),
        ("Judge read page", CHECK),
        ("Step 5 sample", CHECK),
        ("Owner label", select("Keep", "Reject")),
        ("Owner reason", TEXT),
    ],
    RUNS: [
        ("Too old", NUM), ("Duplicates", NUM), ("YouTube gate dropped", NUM),
        ("Triage dropped", NUM), ("Not judged (ceiling)", NUM), ("Passed bar", NUM),
        ("Held", NUM), ("Triage drop sample", LONG), ("Rulebook version", NUM),
    ],
    SOURCES: [
        ("Judged last run", NUM), ("Judged (30d)", NUM), ("Judged daily", LONG),
    ],
}

for table, fields in FIELDS.items():
    url = f"https://api.airtable.com/v0/meta/bases/{BASE}/tables/{table}/fields"
    for name, spec in fields:
        body = json.dumps({"name": name, **spec}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            print(f"  + created  {name}")
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")
            if "DUPLICATE" in msg.upper() or "already" in msg.lower() or "same name" in msg.lower():
                print(f"  = exists   {name}")
            else:
                print(f"  ! FAILED   {name}: {e.code} {msg[:200]}")
