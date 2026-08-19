"""Batch A/B v2 — smaller batches (3 & 4) + FULLER bodies + single-vs-single baseline.

Adds vs v1:
  - Real article bodies: fetch each published row's Source URL, strip HTML, take
    ~1500 chars (falls back to the stored summary if the page bot-blocks). This is
    the fuller body the judge would ideally see, not the 60-word card summary.
  - Two batch sizes (3 and 4) to find the fidelity/saving sweet spot.
  - SINGLE run TWICE: single-A is the baseline; single-B measures the judge's own
    run-to-run nondeterminism (temperature 0.2), so we can tell how much batch
    disagreement is really from batching vs inherent variance.
All batches + single-B are compared against single-A. Cloud only (Groq 403s local).
"""
import os, sys, re, json, time, pathlib, urllib.request, urllib.parse, ssl

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

N_REAL = 8
SLEEP_SINGLE = 13
SLEEP_BATCH = 45
_UNVERIFIED = ssl._create_unverified_context()


def article_body(url, fallback):
    """Fetch real article text (HTML stripped, ~1500 chars). Fallback on block."""
    if not (url or "").lower().startswith("http"):
        return fallback
    try:
        req = urllib.request.Request(url, headers={"User-Agent": H.BROWSER_UA})
        try:
            raw = urllib.request.urlopen(req, timeout=15).read()
        except Exception:
            raw = urllib.request.urlopen(req, timeout=15, context=_UNVERIFIED).read()
        html = raw.decode("utf-8", "ignore")
        html = re.sub(r"(?is)<(script|style|noscript|head).*?</\1>", " ", html)
        text = re.sub(r"(?is)<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:1500] if len(text) > 300 else fallback
    except Exception:
        return fallback


def fetch_published(n):
    formula = "{Status}='Published'"
    url = (f"https://api.airtable.com/v0/{BASE}/{TABLE}?pageSize={n}"
           f"&filterByFormula={urllib.parse.quote(formula)}")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"})
    rows = json.loads(urllib.request.urlopen(req).read()).get("records", [])
    posts = []
    for r in rows[:n]:
        f = r.get("fields", {})
        body = article_body(f.get("Source URL", ""), f.get("Summary", ""))
        posts.append({"title": f.get("Title", ""), "selftext": body, "subreddit": "",
                      "over_18": False, "external_url": "",
                      "provenance": f.get("Source type", "feed"),
                      "source_label": f.get("Source name", ""),
                      "created": f.get("Date", ""), "_tag": "real-keep",
                      "_bodylen": len(body)})
    return posts


CRAFTED = [
    {"title": "Anti-AI protesters arrested outside tech conference", "_tag": "reject-politics",
     "selftext": "Police arrested a dozen demonstrators who blocked the entrance to an industry "
     "conference on Tuesday, chanting slogans against automation and job cuts. Organisers said "
     "the event proceeded on schedule and no property was damaged. The protesters, part of a "
     "loose coalition of labour and student groups, face trespassing charges and were released "
     "on bail hours later. City officials said the group had not obtained a permit for the "
     "sidewalk demonstration, while the coalition accused police of heavy-handed tactics. A "
     "spokesperson for the mayor defended the response and said the right to protest was "
     "respected. The conference featured panels on enterprise software and cloud computing."},
    {"title": "Startup raises $50M to build an 'ethical AI' platform", "_tag": "reject-PR",
     "selftext": "A two-year-old startup announced a $50 million Series B funding round led by a "
     "prominent venture fund, with participation from several angel investors. The company says "
     "its platform will help enterprises adopt artificial intelligence responsibly and at scale. "
     "The chief executive called the raise a milestone and a game-changer, and said the firm "
     "planned to double its headcount over the next year and open a second office. The round "
     "brings total funding to $70 million. The company did not disclose revenue, customers, or "
     "specific product capabilities, and declined to name the enterprises it is working with."},
    {"title": "AI deepfake scam drains pensioners' savings, police warn", "_tag": "reject-coherence",
     "selftext": "A severe thunderstorm swept across the county overnight, knocking out power to "
     "roughly eight thousand homes and businesses. Utility crews worked through the early morning "
     "to restore service and warned residents to stay clear of downed lines and flooded roads. "
     "The National Weather Service said winds gusted to sixty miles per hour and more than two "
     "inches of rain fell in under an hour. Several trees fell onto cars and one blocked a major "
     "road during the morning commute. No injuries were reported. Officials opened a shelter at a "
     "community centre for anyone left without heat or electricity, and said crews expected to "
     "restore most service by evening."},
    {"title": "90% of companies will be breached by AI within a year, expert warns", "_tag": "reject-hype",
     "selftext": "Speaking at a vendor webinar, a commentator predicted that almost all firms "
     "would soon fall victim to AI-powered cyberattacks and urged organisations to buy protection "
     "immediately. He said the threat was unprecedented and that companies without his "
     "recommended tools would be overwhelmed within twelve months. He offered no study, dataset, "
     "agency report, or named source for the ninety-percent figure, and did not describe any "
     "specific incident, attack technique, or affected organisation. The webinar was sponsored by "
     "a security-software company that sells the products he recommended."},
]
for c in CRAFTED:
    c.update({"subreddit": "", "over_18": False, "external_url": "", "provenance": "Google Alert",
              "created": "2026-08-18", "_bodylen": len(c["selftext"])})

# --- capture real rubric rules (before the Return: schema) --------------------
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
RULES = grab["prompt"].split("\nReturn:\n{")[0].strip()

BATCH_IO = ("\n\nYou are judging MULTIPLE items below. Apply the SAME rules to each item "
            "independently. Return STRICT JSON only, no prose:\n"
            '{"results": [ {"i": <item number>, "kind": "human-impact"|"incident"|'
            '"expert-analysis"|"none", "grounded": true|false, "theme": one of ' + str(H.THEMES) +
            ' or "none", "relevance": 0-100, "sensitivity": "Safe"|"Sensitive", '
            '"reason": "one short line"} ] }\n'
            "Return exactly one object per item; set \"i\" to that item's number. Judge every item.\n\nITEMS:\n")


def batch_prompt(chunk, start):
    lines = [f'[{start+j}] {H._origin_line(p)} | Title: {p["title"]} | '
             f'Body: {(p.get("selftext") or "")[:1500] or "(no body)"}'
             for j, p in enumerate(chunk)]
    return RULES + BATCH_IO + "\n".join(lines)


def run_single(posts, label):
    print(f"\n=== SINGLE {label} ===")
    out, tot = [], 0
    for i, p in enumerate(posts):
        v, tok, st = H.groq_judge(p)
        tot += tok
        out.append(v)
        print(f"  [{i}] {summ(v)} | {p['title'][:42]}")
        time.sleep(SLEEP_SINGLE)
    return out, tot


def run_batch(posts, size):
    print(f"\n=== BATCH size {size} ===")
    out, tot = [None]*len(posts), 0
    for start in range(0, len(posts), size):
        chunk = posts[start:start+size]
        content, tok, st = H.groq_chat(JUDGE_MODEL, batch_prompt(chunk, start))
        tot += tok
        got = {}
        if content:
            try:
                for o in (json.loads(content).get("results") or []):
                    got[int(o.get("i"))] = o
            except Exception as e:
                print(f"  ! parse error: {e}")
        for j in range(len(chunk)):
            out[start+j] = got.get(start+j)
        print(f"  batch[{start}..{start+len(chunk)-1}] {tok}tok ({tok/len(chunk):.0f}/item) parsed {len(got)}/{len(chunk)}")
        time.sleep(SLEEP_BATCH)
    return out, tot


def summ(v):
    if not v:
        return "ERROR"
    return (f"{'KEEP' if judge_passes(v) else 'rej '} k={str(v.get('kind'))[:11]:<11} "
            f"th={str(v.get('theme'))[:15]:<15} r={v.get('relevance','?'):>3} s={str(v.get('sensitivity'))[:4]}")


def compare(base, other, name, posts):
    ak = akind = ath = 0
    n = len(posts)
    diffs = []
    for i in range(n):
        s, b = base[i], other[i]
        ks = judge_passes(s) if s else None
        kb = judge_passes(b) if b else None
        ak += (ks == kb)
        akind += bool(s and b and s.get("kind") == b.get("kind"))
        ath += bool(s and b and s.get("theme") == b.get("theme"))
        if ks != kb:
            diffs.append(f"    [{i}] {posts[i]['_tag']}: A={summ(s)} | {name}={summ(b)}")
    print(f"\n-- {name} vs single-A --  keep={ak}/{n} ({100*ak/n:.0f}%)  kind={akind}/{n}  theme={ath}/{n}")
    for d in diffs:
        print(d)


def main():
    posts = fetch_published(N_REAL) + CRAFTED
    print(f"Test set: {len(posts)} items. Body lengths (chars): "
          f"{[p['_bodylen'] for p in posts]}")
    print(f"Real bodies fetched fuller than summary: "
          f"{sum(1 for p in posts if p['_tag']=='real-keep' and p['_bodylen']>=300)}/{N_REAL}")

    sA, tA = run_single(posts, "A (baseline)")
    time.sleep(SLEEP_BATCH)
    sB, tB = run_single(posts, "B (variance check)")
    time.sleep(SLEEP_BATCH)
    b3, t3 = run_batch(posts, 3)
    time.sleep(SLEEP_BATCH)
    b4, t4 = run_batch(posts, 4)

    n = len(posts)
    print("\n=== COMPARISONS (all vs single-A) ===")
    compare(sA, sB, "single-B", posts)   # inherent nondeterminism
    compare(sA, b3, "batch-3", posts)
    compare(sA, b4, "batch-4", posts)

    print("\n=== TOKENS ===")
    for lbl, t in [("single-A", tA), ("single-B", tB), ("batch-3", t3), ("batch-4", t4)]:
        print(f"  {lbl:<9}: {t:>6} total  {t/n:>6.0f}/item"
              + (f"   ({100*t/tA:.0f}% of single, {100*(1-t/tA):.0f}% saving)" if lbl.startswith("batch") else ""))


if __name__ == "__main__":
    main()
