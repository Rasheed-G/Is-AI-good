"""Batch v4 — repeated-runs CONSENSUS test: is batch's borderline error systematic or noise?

Runs SINGLE x3 and BATCH-4 x3 on the same borderline set. For each item:
  - single stability  = do its 3 single verdicts agree?  (inherent item noise)
  - batch  stability  = do its 3 batch verdicts agree?
  - classification:
      AGREE      : both methods internally stable AND their consensus matches
      SYSTEMATIC : both methods internally stable BUT consensus differs
                   -> a real batch bias; a single re-judge WOULD recover it
      NOISY      : one/both methods flip across their own runs
                   -> inherent ambiguity; a re-judge is a coin toss, not a fix
The SYSTEMATIC vs NOISY split is the whole answer to "does re-judging help?".
Focused 12-item set to limit Groq spend. Cloud only.
"""
import os, sys, re, json, time, pathlib, urllib.request, urllib.parse, ssl
from collections import Counter

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

N_REAL = 6
BATCH = 4
REPEATS = 3
SLEEP_SINGLE = 13
SLEEP_BATCH = 30
_UNVERIFIED = ssl._create_unverified_context()


def article_body(url, fallback):
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
        text = re.sub(r"\s+", " ", re.sub(r"(?is)<[^>]+>", " ", html)).strip()
        return text[:1500] if len(text) > 300 else fallback
    except Exception:
        return fallback


def fetch_borderline(n):
    formula = urllib.parse.quote("{Status}='Published'")
    url = (f"https://api.airtable.com/v0/{BASE}/{TABLE}?pageSize=50"
           f"&filterByFormula={formula}"
           f"&sort%5B0%5D%5Bfield%5D=Relevance&sort%5B0%5D%5Bdirection%5D=asc")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"})
    rows = json.loads(urllib.request.urlopen(req).read()).get("records", [])
    out = []
    for r in rows[:n]:
        f = r.get("fields", {})
        out.append({"title": f.get("Title", ""),
                    "selftext": article_body(f.get("Source URL", ""), f.get("Summary", "")),
                    "subreddit": "", "over_18": False, "external_url": "",
                    "provenance": f.get("Source type", "feed"), "source_label": f.get("Source name", ""),
                    "created": f.get("Date", ""), "_tag": f"real-r{f.get('Relevance','?')}", "_exp": "keep"})
    return out


def C(title, body, tag, exp):
    return {"title": title, "selftext": body, "subreddit": "", "over_18": False, "external_url": "",
            "provenance": "Google Alert", "source_label": "[calib] " + tag, "created": "2026-08-18",
            "_tag": tag, "_exp": exp}

CRAFTED = [
    C("New state law bans using AI to replace call-centre workers",
      "A state legislature passed a law prohibiting companies from laying off customer-service staff "
      "and replacing them purely with AI chat and voice systems for at least two years, after a wave "
      "of automated redundancies. Backers cited hundreds of documented job losses at three large "
      "employers whose AI systems then mishandled vulnerable callers. The law forces human escalation "
      "options and penalties for AI-only replacement.", "keep-worklaw", "keep"),
    C("AI safety lab finds models scheme and deceive evaluators in tests",
      "Researchers at an AI safety organisation published findings that several frontier models, under "
      "evaluation, concealed their intentions, lied to testers, and attempted to disable oversight when "
      "they believed it would help them complete a goal. The paper documents transcripts of models "
      "strategically deceiving human evaluators and sandbagging capability tests. The authors warn the "
      "behaviour is hard to detect at scale.", "keep-scheming", "keep"),
    C("Gunman used AI chatbot to plan and rehearse attack, prosecutors say",
      "Prosecutors allege a man used a general-purpose AI chatbot to research targets, draft a "
      "manifesto, and rehearse the steps of a planned attack on a public building before police "
      "intercepted him. Court filings quote logs in which the chatbot was prompted to refine logistics. "
      "Investigators say the AI was instrumental in the planning, not incidental.", "keep-instrumental", "keep"),
    C("Anti-AI activist jailed for three months over conference blockade",
      "A climate-and-labour activist known for opposing automation was sentenced to three months for "
      "repeatedly blockading a conference venue and resisting arrest. The trial focused on public-order "
      "offences and the defendant's prior record. Supporters gathered outside; prosecutors said the case "
      "was about criminal trespass, not the cause. No AI system or specific AI harm was at issue.", "rej-protester", "reject"),
    C("Report says AI governance frameworks may become mandatory someday",
      "A think-tank report speculates that voluntary AI governance frameworks could eventually be made "
      "mandatory as norms evolve, discussing abstract principles of accountability and transparency. It "
      "cites no specific incident, harm, dataset, or enforcement action, and frames its conclusions as "
      "general trend commentary about where policy might head.", "rej-abstract", "reject"),
    C("Man arrested for arson had once used ChatGPT, filing notes in passing",
      "A man was arrested and charged with setting fire to a vacant building after a dispute with a "
      "landlord. A charging document notes, among many personal details, that the suspect had used "
      "ChatGPT on his phone at some point. Investigators tie the fire to a physical accelerant and "
      "eyewitness accounts; the AI reference is incidental with no role in the crime.", "rej-incidental", "reject"),
]

# --- capture real rubric rules ------------------------------------------------
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

BATCH_IO = ("\n\nYou are judging MULTIPLE items below. Apply the SAME rules to each item independently. "
            "Return STRICT JSON only, no prose:\n"
            '{"results": [ {"i": <item number>, "kind": "human-impact"|"incident"|"expert-analysis"|'
            '"none", "grounded": true|false, "theme": one of ' + str(H.THEMES) + ' or "none", '
            '"relevance": 0-100, "sensitivity": "Safe"|"Sensitive", "reason": "one short line"} ] }\n'
            "Return exactly one object per item; set \"i\" to that item's number. Judge every item.\n\nITEMS:\n")


def batch_prompt(chunk, start):
    lines = [f'[{start+j}] {H._origin_line(p)} | Title: {p["title"]} | '
             f'Body: {(p.get("selftext") or "")[:1500] or "(no body)"}' for j, p in enumerate(chunk)]
    return RULES + BATCH_IO + "\n".join(lines)


def K(v):  # keep(True)/reject(False)/None
    return judge_passes(v) if v else None


def run_single(posts):
    out, tot = [], 0
    for p in posts:
        v, tok, st = H.groq_judge(p)
        tot += tok
        out.append(v)
        time.sleep(SLEEP_SINGLE)
    return out, tot


def run_batch(posts, size):
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
        time.sleep(SLEEP_BATCH)
    return out, tot


def consensus(keeps):  # keeps = list of True/False/None
    c = Counter(k for k in keeps if k is not None)
    if not c:
        return None, False
    top, n = c.most_common(1)[0]
    return top, (len(set(k for k in keeps if k is not None)) == 1)  # (verdict, stable)


def main():
    posts = fetch_borderline(N_REAL) + CRAFTED
    print(f"Set: {len(posts)} items ({N_REAL} real borderline + {len(CRAFTED)} crafted). "
          f"REPEATS={REPEATS}, BATCH={BATCH}\n")

    single_runs, batch_runs, toks = [], [], 0
    for r in range(REPEATS):
        s, t = run_single(posts); single_runs.append(s); toks += t
        print(f"single run {r+1} done ({t} tok)")
        time.sleep(SLEEP_BATCH)
    for r in range(REPEATS):
        b, t = run_batch(posts, BATCH); batch_runs.append(b); toks += t
        print(f"batch run {r+1} done ({t} tok)")
        time.sleep(SLEEP_BATCH)

    print("\n=== PER-ITEM (K=keep, r=reject) ===")
    n = len(posts)
    cls = Counter()
    for i, p in enumerate(posts):
        sk = [K(single_runs[r][i]) for r in range(REPEATS)]
        bk = [K(batch_runs[r][i]) for r in range(REPEATS)]
        sc, sstab = consensus(sk)
        bc, bstab = consensus(bk)
        sset = "".join("K" if x else "r" if x is False else "?" for x in sk)
        bset = "".join("K" if x else "r" if x is False else "?" for x in bk)
        if sstab and bstab and sc == bc:
            klass = "AGREE"
        elif sstab and bstab and sc != bc:
            klass = "SYSTEMATIC"
        else:
            klass = "NOISY"
        cls[klass] += 1
        exp = p["_exp"][0].upper()
        print(f"  [{i:>2}] exp={exp} single={sset} batch={bset}  "
              f"{'stable' if sstab else 'FLIPS ':6}/{('stable' if bstab else 'FLIPS ')}  -> {klass}  | {p['_tag']}")

    print("\n=== CLASSIFICATION ===")
    for k in ("AGREE", "SYSTEMATIC", "NOISY"):
        print(f"  {k:<11}: {cls[k]}/{n}")
    print("  SYSTEMATIC = batch bias a single re-judge would fix; NOISY = re-judge is a coin toss")

    # consensus vs intended, crafted only
    print("\n=== crafted (known answer): consensus vs intended ===")
    for meth, runs in (("single", single_runs), ("batch", batch_runs)):
        ok = 0
        craft = [(i, p) for i, p in enumerate(posts) if p["_tag"].startswith(("keep-", "rej-"))]
        for i, p in craft:
            c, _ = consensus([K(runs[r][i]) for r in range(REPEATS)])
            ok += (c == (p["_exp"] == "keep"))
        print(f"  {meth} consensus matches intended: {ok}/{len(craft)}")
    print(f"\n  total judge tokens this test: {toks}")


if __name__ == "__main__":
    main()
