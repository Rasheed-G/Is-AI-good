"""Re-judge stories already in Airtable with the CURRENT judge (built from rulebook.md).

Used to check a judge change before it goes live (the cloud "Judge check"), and in release 3 step 5
to re-judge every live story under the rulebook. Calls Groq, so it runs in GitHub Actions (Groq
blocks the owner's computer).

The judge sees what it sees at harvest time: the source headline and the start of the article
(fetched again from the Source URL), or the stored summary when the page can't be read.

Settings (environment):
  STATUS=Published         which rows, by Status (comma-separated, e.g. "Published,Candidate")
  LIMIT=0                  re-judge a random sample of this many rows (0 = all of them)
  WRITE=1                  write the scores, verdict, reason and rulebook version to each row
                           (default: print only, nothing written)
  WRITE_THEME=1            with WRITE, also move each row to the theme the judge picked

Run:  source .env.local && python scripts/regrade.py
"""
import os, json, time, random, urllib.parse
from collections import Counter
import harvest_reddit as H
import rulebook

STATUS = [s.strip() for s in os.environ.get("STATUS", "Published").split(",") if s.strip()]
LIMIT = int(os.environ.get("LIMIT", "0") or 0)
WRITE = os.environ.get("WRITE") == "1"
WRITE_THEME = WRITE and os.environ.get("WRITE_THEME") == "1"
API = f"https://api.airtable.com/v0/{H.BASE}/{H.TABLE}"
HDRS = {"Authorization": f"Bearer {H.AIRTABLE_TOKEN}"}


def fetch_rows():
    formula = "OR(" + ",".join(f"{{Status}}='{s}'" for s in STATUS) + ")"
    rows, offset = [], None
    while True:
        url = f"{API}?pageSize=100&filterByFormula={urllib.parse.quote(formula)}" + (f"&offset={offset}" if offset else "")
        data = H.http_json(url, headers=HDRS)
        rows.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    return rows


def as_post(row):
    """The row as the judge would have seen it at harvest time."""
    f = row.get("fields", {})
    url = f.get("Source URL", "")
    body = ""
    if url.startswith("http") and not H.is_social_clip(url):
        body = H._article_text(url)
    return {"title": f.get("Source headline") or f.get("Title", ""),
            "selftext": body or f.get("Summary", ""), "external_url": url, "permalink": url,
            "provenance": f.get("Source type", ""), "source_label": f.get("Source name", ""),
            "subreddit": None, "over_18": False}


def patch(updates):
    for i in range(0, len(updates), 10):
        H.http_json(API, headers={**HDRS, "Content-Type": "application/json"}, method="PATCH",
                    data=json.dumps({"records": updates[i:i + 10], "typecast": True}).encode())


def main():
    rows = fetch_rows()
    if LIMIT and LIMIT < len(rows):
        rows = random.Random(1).sample(rows, LIMIT)
    print(f"Re-judging {len(rows)} {'/'.join(STATUS)} rows with the rulebook v{rulebook.version()} judge"
          f"{' (writing results)' if WRITE else ' (print only, nothing written)'}.\n")
    verdicts, moves, updates, tokens, calls, errors = Counter(), Counter(), [], 0, 0, 0
    by_theme = {}
    for start in range(0, len(rows), H.JUDGE_BATCH):
        chunk = rows[start:start + H.JUDGE_BATCH]
        posts = [as_post(r) for r in chunk]
        replies, tok, status = H.groq_judge_batch(posts)
        tokens += tok
        calls += 1
        for r, t in zip(chunk, replies):
            f = r.get("fields", {})
            if t is None:
                errors += 1
                print(f"  ? ERROR ({status})  {f.get('Title', '')[:60]}")
                continue
            v = H.judge_verdict(t)
            verdicts[v] += 1
            old, new = f.get("Theme", "(none)"), str(t.get("theme", "none"))
            by_theme.setdefault(old, Counter())[v] += 1
            if new != old:
                moves[f"{old} → {new}"] += 1
            flags = ("  [sensitive]" if t.get("sensitive") else "") + (f"  [hold: {t['hold']}]" if t.get("hold") else "")
            print(f"  {v:<6} overall {H.overall_score(t):>3} cred {H._score(t, 'credibility'):>3}  "
                  f"{old[:24]:<24} → {new[:24]:<24} {f.get('Title', '')[:55]}\n"
                  f"         {str(t.get('reason', ''))[:150]}{flags}")
            fx = H.judge_fields(t)
            if WRITE_THEME and new in H.THEMES:
                fx["Theme"] = new
            updates.append({"id": r["id"], "fields": fx})
        time.sleep(H.JUDGE_BATCH_SLEEP)
    judged = sum(verdicts.values())
    print(f"\nOf {judged} re-judged: Keep {verdicts['Keep']}, Weak {verdicts['Weak']}, Reject {verdicts['Reject']}"
          f" ({errors} errors).")
    print("By current theme (Keep / Weak / Reject):")
    for theme, c in sorted(by_theme.items()):
        print(f"  {theme:<34} {c['Keep']:>3} / {c['Weak']:>3} / {c['Reject']:>3}")
    if moves:
        print("Theme the judge would pick instead:")
        for m, n in moves.most_common():
            print(f"  {n:>3}  {m}")
    if calls:
        print(f"Groq tokens: {tokens} in {calls} judge calls of up to {H.JUDGE_BATCH} stories "
              f"(about {tokens // calls} per call, {tokens // max(judged, 1)} per story).")
    if WRITE and updates:
        patch(updates)
        print(f"Wrote judge results to {len(updates)} rows.")


if __name__ == "__main__":
    main()
