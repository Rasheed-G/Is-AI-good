"""One-off: create the per-stage token columns (+ Judge mode) on the Runs table.

write_run() has always TRIED to write Tokens / Tokens A/B/C, but the columns never
existed in the base, so its self-heal silently dropped them every run. This creates
them for real via the Airtable metadata API so the morning run persists real numbers.
Idempotent: skips any field that already exists. Airtable, not Groq (no IP block).
Run: python scripts/create_run_token_fields.py
"""
import os, json, urllib.request, urllib.error, pathlib

for line in (pathlib.Path(".env.local").read_text().splitlines()
             if pathlib.Path(".env.local").exists() else []):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

BASE = "appEiqYd3rnYwUNE7"
RUNS = "tblR1wvxB46FtwMia"
TOKEN = os.environ["AIRTABLE_TOKEN"]

NUM = {"type": "number", "options": {"precision": 0}}
FIELDS = [
    ("Tokens", NUM),
    ("Tokens A (triage)", NUM),
    ("Tokens B (judge)", NUM),
    ("Tokens C (copy)", NUM),
    ("Judge mode", {"type": "singleLineText"}),
]

url = f"https://api.airtable.com/v0/meta/bases/{BASE}/tables/{RUNS}/fields"
for name, spec in FIELDS:
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
