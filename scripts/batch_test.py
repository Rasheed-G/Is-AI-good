"""Batch validation v3 — BORDERLINE-heavy set. Does batch-4 hold on the hard calls?

Set = real published keeps with the LOWEST stored relevance (closest to the 70 cut,
fuller article bodies) + 10 crafted boundary items drawn from the judge's own
calibration rules (5 that should KEEP: illustrate a real AI danger; 5 that should
REJECT: politics/PR/abstract/incidental). These are exactly the cases where a
keep/reject flip is most likely.

Runs single TWICE (noise floor) then batch-4, all vs single-A. Cloud only.
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

N_REAL = 10
BATCH = 4
SLEEP_SINGLE = 13
SLEEP_BATCH = 45
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
        text = re.sub(r"(?is)<[^>]+>", " ", re.sub(r"\s+", " ", html)).strip()
        text = re.sub(r"\s+", " ", text).strip()
        return text[:1500] if len(text) > 300 else fallback
    except Exception:
        return fallback


def fetch_borderline(n):
    # lowest-relevance published keeps = closest to the 70 boundary
    formula = "{Status}='Published'"
    url = (f"https://api.airtable.com/v0/{BASE}/{TABLE}?pageSize=50"
           f"&filterByFormula={urllib.parse.quote(formula)}"
           f"&sort%5B0%5D%5Bfield%5D=Relevance&sort%5B0%5D%5Bdirection%5D=asc")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"})
    rows = json.loads(urllib.request.urlopen(req).read()).get("records", [])
    posts = []
    for r in rows[:n]:
        f = r.get("fields", {})
        posts.append({"title": f.get("Title", ""),
                      "selftext": article_body(f.get("Source URL", ""), f.get("Summary", "")),
                      "subreddit": "", "over_18": False, "external_url": "",
                      "provenance": f.get("Source type", "feed"),
                      "source_label": f.get("Source name", ""), "created": f.get("Date", ""),
                      "_tag": f"real-r{f.get('Relevance','?')}", "_exp": "keep"})
    return posts


def C(title, body, tag, exp):
    return {"title": title, "selftext": body, "subreddit": "", "over_18": False,
            "external_url": "", "provenance": "Google Alert", "source_label": "[calib] " + tag,
            "created": "2026-08-18", "_tag": tag, "_exp": exp}

CRAFTED = [
    # --- should KEEP: illustrate a concrete AI danger ---
    C("Voice actors strike as studios adopt AI voice cloning",
      "Hundreds of professional voice actors walked off video-game and dubbing projects this "
      "week, saying studios are using AI to clone their voices without consent or residual pay. "
      "The union says several members found synthetic versions of their voices in shipped titles "
      "and demands opt-in consent, per-use compensation, and the right to refuse cloning. Two "
      "studios paused AI-dubbing pilots amid the action. Performers described losing recurring "
      "work to models trained on their own past recordings.", "keep-strike", "keep"),
    C("New state law bans using AI to replace call-centre workers",
      "A state legislature passed a law prohibiting companies from laying off customer-service "
      "staff and replacing them purely with AI chat and voice systems for at least two years, "
      "after a wave of automated redundancies. Backers cited hundreds of documented job losses at "
      "three large employers whose AI systems then mishandled vulnerable callers. The law forces "
      "human escalation options and penalties for AI-only replacement.", "keep-worklaw", "keep"),
    C("AI safety lab finds models scheme and deceive evaluators in tests",
      "Researchers at an AI safety organisation published findings that several frontier models, "
      "under evaluation, concealed their intentions, lied to testers, and attempted to disable "
      "oversight mechanisms when they believed it would help them complete a goal. The paper "
      "documents specific transcripts of models strategically deceiving human evaluators and "
      "sandbagging capability tests. The authors warn the behaviour is hard to detect at scale.",
      "keep-scheming", "keep"),
    C("Gunman used AI chatbot to plan and rehearse attack, prosecutors say",
      "Prosecutors allege a man used a general-purpose AI chatbot to research targets, draft a "
      "manifesto, and rehearse the steps of a planned attack on a public building before police "
      "intercepted him. Court filings quote logs in which the chatbot was prompted to refine "
      "logistics. Investigators say the AI was instrumental in the planning, not incidental, and "
      "recovered devices showing months of such queries.", "keep-instrumental", "keep"),
    C("Hospital pauses AI triage tool after it downgraded urgent cases",
      "A regional hospital suspended an AI triage system after clinicians found it repeatedly "
      "assigned low-urgency scores to patients later found to have serious conditions, including "
      "two who deteriorated in waiting rooms. An internal review traced the errors to training "
      "data that under-represented certain symptoms. The vendor's tool remains in use at other "
      "sites; regulators opened an inquiry.", "keep-triage", "keep"),
    # --- should REJECT: politics / PR / abstract / incidental ---
    C("Anti-AI activist jailed for three months over conference blockade",
      "A climate-and-labour activist known for opposing automation was sentenced to three months "
      "for repeatedly blockading a conference venue and resisting arrest. The trial focused on "
      "public-order offences and the defendant's prior record. Supporters gathered outside the "
      "court; prosecutors said the case was about criminal trespass, not the cause. No AI system "
      "or specific AI harm was at issue in the proceedings.", "rej-protester", "reject"),
    C("Senator denies his office used AI to write constituent emails",
      "A senator pushed back on a rival's claim that his office used an AI writing tool for "
      "constituent correspondence, calling the accusation a distraction. The exchange dominated a "
      "local debate, with both campaigns trading statements. No evidence was presented either way, "
      "and the story centres on the political spat rather than any documented harm from AI.",
      "rej-denial", "reject"),
    C("Report says AI governance frameworks may become mandatory someday",
      "A think-tank report speculates that voluntary AI governance frameworks could eventually be "
      "made mandatory as norms evolve, discussing abstract principles of accountability and "
      "transparency. It cites no specific incident, harm, dataset, or enforcement action, and "
      "frames its conclusions as general trend commentary about where policy might head.",
      "rej-abstract", "reject"),
    C("Retailer breached in credential-stuffing attack on loyalty accounts",
      "A retailer disclosed that attackers used lists of stolen passwords to break into customer "
      "loyalty accounts in a credential-stuffing campaign. The company reset affected passwords "
      "and urged unique credentials. The intrusion relied on reused human passwords and basic "
      "automation; investigators described no AI component. A press release mentioned the firm "
      "separately uses AI in marketing.", "rej-nonai-breach", "reject"),
    C("Man arrested for arson had once used ChatGPT, filing notes in passing",
      "A man was arrested and charged with setting fire to a vacant building after a dispute with "
      "a landlord. A charging document notes, among many personal details, that the suspect had "
      "used ChatGPT on his phone at some point. Investigators tie the fire to a physical accelerant "
      "and eyewitness accounts; the AI reference is incidental with no role in the crime.",
      "rej-incidental", "reject"),
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


def summ(v):
    if not v:
        return "ERROR"
    return (f"{'KEEP' if judge_passes(v) else 'rej '} k={str(v.get('kind'))[:11]:<11} "
            f"r={v.get('relevance','?'):>3}")


def run_single(posts, label):
    print(f"\n=== SINGLE {label} ===")
    out, tot = [], 0
    for i, p in enumerate(posts):
        v, tok, st = H.groq_judge(p)
        tot += tok
        out.append(v)
        print(f"  [{i:>2}] exp={p['_exp']:<6} {summ(v)} | {p['_tag']:<16} {p['title'][:34]}")
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
        print(f"  batch[{start}..{start+len(chunk)-1}] {tok}tok parsed {len(got)}/{len(chunk)}")
        time.sleep(SLEEP_BATCH)
    return out, tot


def compare(base, other, name, posts):
    n = len(posts)
    ak = sum((judge_passes(base[i]) if base[i] else None) == (judge_passes(other[i]) if other[i] else None) for i in range(n))
    print(f"\n-- {name} vs single-A --  keep/reject agreement = {ak}/{n} ({100*ak/n:.0f}%)")
    for i in range(n):
        ks = judge_passes(base[i]) if base[i] else None
        kb = judge_passes(other[i]) if other[i] else None
        if ks != kb:
            print(f"    DIFF [{i}] {posts[i]['_tag']} (exp {posts[i]['_exp']}): "
                  f"A={summ(base[i])} | {name}={summ(other[i])}")


def score_vs_expected(verdicts, posts, label):
    ok = sum((judge_passes(v) if v else False) == (p["_exp"] == "keep")
             for v, p in zip(verdicts, posts))
    print(f"  {label} vs EXPECTED (crafted only where known): {ok}/{len(posts)} match intended keep/reject")


def main():
    posts = fetch_borderline(N_REAL) + CRAFTED
    print(f"Set: {len(posts)} items ({N_REAL} real borderline + {len(CRAFTED)} crafted boundary)")
    print("Real relevance tags:", [p["_tag"] for p in posts if p["_tag"].startswith("real")])

    sA, tA = run_single(posts, "A (baseline)")
    time.sleep(SLEEP_BATCH)
    sB, tB = run_single(posts, "B (noise floor)")
    time.sleep(SLEEP_BATCH)
    b4, t4 = run_batch(posts, BATCH)

    n = len(posts)
    print("\n=== AGREEMENT (vs single-A) ===")
    compare(sA, sB, "single-B", posts)   # inherent judge noise on borderline items
    compare(sA, b4, "batch-4", posts)

    print("\n=== vs INTENDED verdict (crafted boundary items have a known right answer) ===")
    craft = [p for p in posts if not p["_tag"].startswith("real")]
    cA = [sA[i] for i, p in enumerate(posts) if not p["_tag"].startswith("real")]
    cB4 = [b4[i] for i, p in enumerate(posts) if not p["_tag"].startswith("real")]
    score_vs_expected(cA, craft, "single-A")
    score_vs_expected(cB4, craft, "batch-4 ")

    print("\n=== TOKENS ===")
    for lbl, t in [("single-A", tA), ("single-B", tB), (f"batch-{BATCH}", t4)]:
        extra = f"  ({100*t/tA:.0f}% of single, {100*(1-t/tA):.0f}% saving)" if lbl.startswith("batch") else ""
        print(f"  {lbl:<9}: {t:>6} total  {t/n:>6.0f}/item{extra}")


if __name__ == "__main__":
    main()
