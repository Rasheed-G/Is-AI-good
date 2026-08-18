"""Seed the Airtable Sources table with the social-clip (TikTok + Instagram) Google-Alert
rows so you can copy each query straight out of Airtable when building the alerts.

What it creates (see docs/social-alert-queries.md for the rationale):
  * one `RSS feed` Sources row per social alert, Name = "[Theme] social — …"
  * the alert Query goes in the row's **Notes** (the project's canonical place for it)
  * the **Theme** label is set; **Active is left OFF and URL blank** on purpose — a row
    with no feed URL must NOT be Active or the harvester would try to fetch its Name.

Your workflow after running this:
  1. Open a row → copy the Query from Notes → google.com/alerts → Show options →
     Deliver to RSS feed → copy the feed URL.
  2. Paste that feed URL into the row's URL field and tick Active.
  3. Do the START-HERE rows first (proven themes, TikTok + IG); roll out the rest if worth it.
  4. These platform-split rows REPLACE the 14 old combined "[Theme] social — …" rows — after
     activating a platform row, untick Active on its old combined counterpart so yield tracks cleanly.

Idempotent: rows whose Name already exists in Sources are skipped, so re-running is safe.

Run locally:  source .env.local && python scripts/seed_social_sources.py
(stdlib only, same as the harvester; never reads .env.local itself — token comes from env)
"""
import os, sys, json, time, urllib.request, urllib.parse, urllib.error

# Windows cp1252 console would crash on the em-dash / arrow in output; stream as UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

AIRTABLE_TOKEN = os.environ["AIRTABLE_TOKEN"]
BASE = "appEiqYd3rnYwUNE7"
SOURCES_TABLE = "tbllWd5Rby0pkgIpB"
API = f"https://api.airtable.com/v0/{BASE}/{SOURCES_TABLE}"
HEADERS = {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}

# --- Platform-specific split + query variety (see docs/social-alert-queries.md) --------------
# Each VARIANT below is expanded into TWO Sources rows — one `site:tiktok.com`, one
# `site:instagram.com` — so TikTok gets its OWN alert quota instead of being crowded out of a
# combined `(tiktok OR instagram)` feed (which returned mostly IG). Higher-yield themes carry a
# 2nd query variant for wider recall. These REPLACE the 14 old combined rows: once you activate
# a platform row, untick Active on the matching old "[Theme] social — …" combined row so
# per-source yield tracks cleanly (this seeder leaves the old rows alone — different Names).
#
# (tag, Theme, descriptor, core_query_without_site, start_here)
VARIANTS = [
    ("Scams", "Scams & fraud", "voice-clone / deepfake fraud",
     '(deepfake OR "voice cloning" OR "AI voice") (scam OR fraud OR victim OR "lost money")', True),
    ("Scams", "Scams & fraud", "AI romance scam",
     '(AI OR chatbot OR deepfake) ("romance scam" OR "pig butchering" OR catfish)', True),
    ("Deepfakes", "Deepfakes & image abuse", "non-consensual images",
     'deepfake (nudify OR "non-consensual" OR nonconsensual OR "without consent" OR "fake nudes")', True),
    ("Deepfakes", "Deepfakes & image abuse", "sextortion / minors",
     '(deepfake OR "AI generated") (sextortion OR explicit OR school) (victim OR teen OR arrested)', False),
    ("AI companions", "AI companions & mental health", "chatbot mental-health harm",
     '("AI chatbot" OR "AI companion" OR Character.AI OR chatbot) (suicide OR "mental health" OR "self harm" OR harm)', True),
    ("AI companions", "AI companions & mental health", "teen attachment to AI",
     '("AI boyfriend" OR "AI girlfriend" OR "AI companion" OR Character.AI) (teen OR addicted OR obsessed OR lonely)', False),
    ("Kids", "Kids & safety", "AI harm to children",
     '(AI OR chatbot OR deepfake) (child OR kid OR teen) (danger OR harm OR grooming OR victim)', False),
    ("Kids", "Kids & safety", "deepfake bullying at school",
     '(deepfake OR "AI generated" OR "fake images") (school OR classmate OR students OR bullying)', False),
    ("Jobs", "Jobs & livelihoods", "replaced by AI",
     '("replaced by AI" OR "laid off" OR "lost my job") (AI OR automation OR chatbot)', False),
    ("Jobs", "Jobs & livelihoods", "AI took creative work",
     '(AI OR "generative AI") (artist OR writer OR "voice actor" OR freelancer) (replaced OR "no work" OR "out of work")', False),
    ("Surveillance", "Surveillance", "facial-recognition wrongful",
     '("facial recognition" OR "face scan") (wrongful OR arrested OR misidentified OR privacy)', False),
    ("Surveillance", "Surveillance", "AI tracking / privacy",
     '("AI surveillance" OR "AI camera" OR "license plate" OR tracking) (privacy OR watched OR police)', False),
    ("Misinformation", "Misinformation", "AI fakes / hoaxes",
     '("AI generated" OR deepfake OR "fake video") (misinformation OR hoax OR "fake news" OR fooled)', False),
    ("Breaches", "Breaches & system failures", "AI failure / data leak",
     '(AI OR chatbot OR algorithm) (glitch OR "data leak" OR breach OR "went wrong" OR failed)', False),
    ("Experts", "Even the experts are worried", "insiders warn",
     '("AI researcher" OR "AI expert" OR "ex-OpenAI" OR whistleblower) (warns OR danger OR risk OR quit)', False),
    ("Bias", "Bias & discrimination", "biased / discriminatory AI",
     '(AI OR algorithm OR "facial recognition") (racist OR biased OR discrimination OR denied)', False),
    ("Automated", "Automated decisions & welfare", "algorithm denied me",
     '(algorithm OR AI OR automated) (denied OR "cut off" OR benefits OR wrongly OR fired)', False),
    ("Environment", "Environmental / data-centre impact", "data-centre impact",
     '("data center" OR "data centre") (water OR "power bill" OR noise OR residents)', False),
]

# Expand each variant into one TikTok row and one Instagram row.
PLATFORMS = [("TikTok", "site:tiktok.com"), ("IG", "site:instagram.com")]
ROWS = []
for tag, theme, descriptor, core, sh in VARIANTS:
    for label, site in PLATFORMS:
        ROWS.append((f"[{tag}] {label} — {descriptor}", theme, f"{core} {site}", sh))


def existing_names():
    """Set of Names already in the Sources table (all rows, paged)."""
    names, offset = set(), None
    while True:
        url = API + "?pageSize=100&fields%5B%5D=Name"
        if offset:
            url += "&offset=" + urllib.parse.quote(offset)
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        for rec in data.get("records", []):
            n = rec.get("fields", {}).get("Name")
            if n:
                names.add(n.strip())
        offset = data.get("offset")
        if not offset:
            return names


def create(batch):
    """POST up to 10 records. typecast=true so the Theme/Type single-selects accept the
    label if the option doesn't exist yet (Airtable creates it)."""
    payload = {"records": [{"fields": f} for f in batch], "typecast": True}
    req = urllib.request.Request(API, data=json.dumps(payload).encode(), headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    have = existing_names()
    to_make, skipped, start_here = [], [], []
    for name, theme, query, sh in ROWS:
        if name in have:
            skipped.append(name)
            continue
        to_make.append({"Name": name, "Type": "RSS feed", "Theme": theme,
                        "Notes": query, "Active": False})
        if sh:
            start_here.append(name)

    print(f"Sources rows: {len(have)} existing. To create: {len(to_make)}. Already present: {len(skipped)}.")
    for n in skipped:
        print(f"  · skip (exists): {n}")

    made = 0
    for i in range(0, len(to_make), 10):
        chunk = to_make[i:i + 10]
        try:
            res = create(chunk)
            made += len(res.get("records", []))
            for rec in res.get("records", []):
                print(f"  + created: {rec['fields'].get('Name')}")
        except urllib.error.HTTPError as e:
            print(f"  ! HTTP {e.code} creating batch {i//10 + 1}: {e.read().decode('utf-8', 'replace')[:300]}")
        time.sleep(0.3)  # be gentle with the API

    print(f"\nDone. Created {made} row(s), all Active=OFF with blank URL (fill after building each alert).")
    if start_here:
        print(f"\n▶ Build these {len(start_here)} alerts FIRST (proven-theme recall test, "
              f"TikTok + IG so you can compare per-platform yield):")
        for n in start_here:
            print(f"    {n}")


if __name__ == "__main__":
    main()
