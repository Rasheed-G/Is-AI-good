"""A/B test: batched judge vs single-item judge — token savings AND verdict agreement.

The single-item judge sends the full ~1,090-token rubric on every call. A batched
judge sends the rubric ONCE and lists N items, so the fixed rubric is amortised.
This measures both: (a) tokens/item single vs batched, (b) do the verdicts agree
(keep/reject, kind, theme, relevance, sensitivity)?

Reuses the REAL calibrated rubric (captured from groq_judge, split at the Return:
schema line) so the batched prompt's decision rules are byte-identical to production;
only the I/O envelope changes to a JSON array.

Test set = real published rows (keeps, across themes) + a few crafted rejects that
stress SCOPE/COHERENCE/guardrail (rejects aren't stored in Airtable). Runs in the
cloud (Groq 403s local IP). No secrets printed.
"""
import os, sys, json, time, pathlib, urllib.request, urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
envf = ROOT / ".env.local"
if envf.exists():
    for line in envf.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

sys.path.insert(0, str(ROOT / "scripts"))
import harvest_reddit as H  # noqa: E402
from harvest_reddit import BASE, TABLE, AIRTABLE_TOKEN, JUDGE_MODEL, judge_passes  # noqa: E402

BATCH_SIZE = 6
SLEEP_SINGLE = 13   # stay under the 8K TPM free-tier bucket
SLEEP_BATCH = 45

# --- pull real published rows (title + stored summary as body) ----------------
def fetch_published(n=8):
    formula = "{Status}='Published'"
    url = (f"https://api.airtable.com/v0/{BASE}/{TABLE}?pageSize={n}"
           f"&filterByFormula={urllib.parse.quote(formula)}")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"})
    rows = json.loads(urllib.request.urlopen(req).read()).get("records", [])
    posts = []
    for r in rows[:n]:
        f = r.get("fields", {})
        posts.append({"title": f.get("Title", ""), "selftext": f.get("Summary", ""),
                      "subreddit": "", "over_18": False, "external_url": "",
                      "provenance": f.get("Source type", "feed"),
                      "source_label": f.get("Source name", ""),
                      "created": f.get("Date", ""), "_tag": "real-keep"})
    return posts

CRAFTED = [
    {"title": "Anti-AI protesters arrested outside tech conference",
     "selftext": "Police arrested a dozen demonstrators who blocked the entrance to an "
     "industry conference, chanting against automation. Organisers said the event "
     "proceeded on schedule; the protesters face trespassing charges. The dispute "
     "centres on the group's tactics and the city's protest permits.",
     "subreddit": "", "over_18": False, "external_url": "", "provenance": "Google Alert",
     "source_label": "[Jobs] anti-AI protest", "created": "2026-08-18", "_tag": "craft-reject-politics"},
    {"title": "Startup raises $50M to build an 'ethical AI' platform",
     "selftext": "The company announced a Series B led by a venture fund, saying its "
     "platform will help enterprises adopt AI responsibly. The CEO called it a "
     "game-changer and said hiring would double. No product details or customers "
     "were disclosed.",
     "subreddit": "", "over_18": False, "external_url": "", "provenance": "Google Alert",
     "source_label": "[Experts] AI funding PR", "created": "2026-08-18", "_tag": "craft-reject-PR"},
    {"title": "AI deepfake scam drains pensioners' savings, police warn",
     "selftext": "A severe thunderstorm knocked out power to thousands of homes across "
     "the county overnight. Utility crews are working to restore service and officials "
     "urged residents to avoid downed lines. No injuries were reported.",
     "subreddit": "", "over_18": False, "external_url": "", "provenance": "Google Alert",
     "source_label": "[Scams] coherence-mismatch", "created": "2026-08-18", "_tag": "craft-reject-coherence"},
    {"title": "90% of companies will be breached by AI within a year, expert warns",
     "selftext": "A commentator predicted that almost all firms would soon fall victim "
     "to AI-powered attacks, urging everyone to buy protection now. No study, data, "
     "or named source was cited for the figure.",
     "subreddit": "", "over_18": False, "external_url": "", "provenance": "Google Alert",
     "source_label": "[Breaches] unsourced hype", "created": "2026-08-18", "_tag": "craft-reject-hype"},
]

# --- capture the real rubric rules (everything before the Return: schema) ------
grab = {}
_real = H.http_json
def spy(url, headers=None, data=None, method="GET"):
    r = _real(url, headers=headers, data=data, method=method)
    grab["prompt"] = json.loads(data)["messages"][0]["content"]
    return r
H.http_json = spy
H.groq_judge({"title": "x", "selftext": "y", "subreddit": "", "over_18": False,
              "external_url": "", "provenance": "feed", "source_label": "z"})
H.http_json = _real
MARKER = "\nReturn:\n{"
RULES = grab["prompt"].split(MARKER)[0].strip()
print(f"Captured rubric rules: {len(RULES)} chars (split at Return: schema)")

BATCH_IO = """

You are judging MULTIPLE items below. Apply the SAME rules to each item independently.
Return STRICT JSON only, no prose, in exactly this shape:
{"results": [
  {"i": <the item's number>, "kind": "human-impact"|"incident"|"expert-analysis"|"none",
   "grounded": true|false, "theme": one of """ + str(H.THEMES) + """ or "none",
   "relevance": 0-100, "sensitivity": "Safe"|"Sensitive", "reason": "one short line"}
]}
Return exactly one object per item, and set "i" to that item's number. Judge every item.

ITEMS:
"""

def build_batch_prompt(chunk, start):
    lines = []
    for j, p in enumerate(chunk):
        body = (p.get("selftext") or "")[:1500]
        lines.append(f'[{start + j}] {H._origin_line(p)} | Title: {p["title"]} | '
                     f'Body: {body or "(no body text)"}')
    return RULES + BATCH_IO + "\n".join(lines)

def single_judge(p):
    v, tok, status = H.groq_judge(p)
    return v, tok, status

def batch_judge(chunk, start):
    prompt = build_batch_prompt(chunk, start)
    content, tok, status = H.groq_chat(JUDGE_MODEL, prompt)  # json_mode on by default
    verdicts = {}
    if content:
        try:
            for obj in (json.loads(content).get("results") or []):
                verdicts[int(obj.get("i"))] = obj
        except Exception as e:
            print(f"  ! batch parse error: {e}")
    return verdicts, tok, status

def summ(v):
    if not v:
        return "ERROR"
    return f"{'KEEP' if judge_passes(v) else 'rej '} k={v.get('kind','?')[:12]:<12} " \
           f"th={str(v.get('theme'))[:16]:<16} r={v.get('relevance','?'):>3} s={v.get('sensitivity','?')[:4]}"

def main():
    posts = fetch_published(8) + CRAFTED
    print(f"Test set: {len(posts)} items ({sum(1 for p in posts if p['_tag']=='real-keep')} real, "
          f"{sum(1 for p in posts if p['_tag']!='real-keep')} crafted)\n")

    print("=== SINGLE (baseline) ===")
    single = []
    tok_single = 0
    for i, p in enumerate(posts):
        v, tok, st = single_judge(p)
        tok_single += tok
        single.append(v)
        print(f"  [{i}] {tok:>5}tok  {summ(v)}  | {p['title'][:44]}")
        time.sleep(SLEEP_SINGLE)

    print("\n… pause to clear TPM before batched run …")
    time.sleep(SLEEP_BATCH)

    print(f"\n=== BATCHED (size {BATCH_SIZE}) ===")
    batch = [None] * len(posts)
    tok_batch = 0
    for start in range(0, len(posts), BATCH_SIZE):
        chunk = posts[start:start + BATCH_SIZE]
        vmap, tok, st = batch_judge(chunk, start)
        tok_batch += tok
        for j in range(len(chunk)):
            batch[start + j] = vmap.get(start + j)
        print(f"  batch [{start}..{start+len(chunk)-1}]: {tok}tok total "
              f"({tok/len(chunk):.0f}/item), status={st}, parsed {len(vmap)}/{len(chunk)}")
        time.sleep(SLEEP_BATCH)

    print("\n=== PER-ITEM COMPARISON ===")
    agree_keep = agree_kind = agree_theme = 0
    rel_deltas = []
    n = len(posts)
    for i, p in enumerate(posts):
        s, b = single[i], batch[i]
        ks = judge_passes(s) if s else None
        kb = judge_passes(b) if b else None
        ok = "OK " if ks == kb else "DIFF"
        agree_keep += (ks == kb)
        agree_kind += bool(s and b and s.get("kind") == b.get("kind"))
        agree_theme += bool(s and b and s.get("theme") == b.get("theme"))
        if s and b:
            rel_deltas.append(abs(int(s.get("relevance", 0)) - int(b.get("relevance", 0))))
        print(f"  [{i}] {ok} | S: {summ(s)} | B: {summ(b)} | {p['_tag']}")

    print("\n=== SUMMARY ===")
    print(f"  keep/reject agreement : {agree_keep}/{n}  ({100*agree_keep/n:.0f}%)")
    print(f"  kind agreement        : {agree_kind}/{n}")
    print(f"  theme agreement       : {agree_theme}/{n}")
    if rel_deltas:
        print(f"  mean |relevance delta|: {sum(rel_deltas)/len(rel_deltas):.1f} "
              f"(max {max(rel_deltas)})")
    print(f"  tokens SINGLE total   : {tok_single}  ({tok_single/n:.0f}/item)")
    print(f"  tokens BATCHED total  : {tok_batch}  ({tok_batch/n:.0f}/item)")
    if tok_single:
        print(f"  => batched uses {100*tok_batch/tok_single:.0f}% of single-call tokens "
              f"({100*(1-tok_batch/tok_single):.0f}% saving)")

if __name__ == "__main__":
    main()
