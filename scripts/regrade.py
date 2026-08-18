"""Re-judge the auto-harvested Reddit Candidate rows already in Airtable, using
the NEW strict judge — the truest before/after comparison. Reads nothing from
Reddit, writes nothing back. Just prints how each existing row scores now.

Run:  source .env.local && python scripts/regrade.py
"""
import urllib.request, urllib.parse, json
from harvest_reddit import groq_triage, judge_passes, BASE, TABLE, AIRTABLE_TOKEN

def fetch_reddit_candidates():
    formula = "AND({Source type}='Reddit',{Status}='Candidate')"
    url = (f"https://api.airtable.com/v0/{BASE}/{TABLE}?pageSize=100"
           f"&filterByFormula={urllib.parse.quote(formula)}")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"})
    return json.loads(urllib.request.urlopen(req).read()).get("records", [])

def main():
    rows = fetch_reddit_candidates()
    print(f"Re-judging {len(rows)} existing auto-harvested Reddit rows with the NEW strict judge:\n")
    keep = rej = 0
    for r in rows:
        f = r.get("fields", {})
        # Feed the judge the same content it would see: title + the stored summary.
        post = {"title": f.get("Title", ""), "selftext": f.get("Summary", ""),
                "subreddit": "", "permalink": f.get("Source URL", ""), "over_18": False}
        t, _tok = groq_triage(post)   # groq_triage returns (verdict, tokens); tokens unused here
        if not t:
            print(f"  ? ERROR — {f.get('Title','')[:60]}")
            continue
        passes = judge_passes(t)
        keep += passes; rej += (not passes)
        old = f.get("Relevance", "?")
        verd = "KEEP  " if passes else "reject"
        print(f"  {verd}  now[{int(t.get('relevance',0)):>3}] was[{old:>3}]  {t.get('kind','?'):<15} "
              f"{f.get('Title','')[:48]} :: {t.get('reason','')[:55]}")
    print(f"\nOf {len(rows)} originally-kept rows, the new judge would KEEP {keep}, reject {rej}.")

if __name__ == "__main__":
    main()
