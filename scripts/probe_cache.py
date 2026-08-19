"""One-off probe: does Groq prompt caching activate on our Stage-B judge prompt,
and does the cached prefix show up in `usage.prompt_tokens_details.cached_tokens`?

Reuses the REAL judge prompt by importing harvest_reddit and spying on http_json,
so we measure our actual rubric, not a mock. Makes 3 sequential calls:
  1. item A (cold)                      -> expect cached_tokens = 0
  2. item A again (identical prompt)    -> expect cached_tokens ~= full prompt
  3. item B (different body, same rubric)-> expect cached_tokens ~= rubric prefix
Call 3 is the production case: same fixed rubric, different item each time.

Run in the CLOUD (Groq 403s the owner's local IP). Reads .env.local itself.
No secrets are printed.
"""
import os, sys, json, time, pathlib

# --- self-load .env.local (KEY=VALUE lines), never print values ---------------
ROOT = pathlib.Path(__file__).resolve().parent.parent
envf = ROOT / ".env.local"
if envf.exists():
    for line in envf.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

sys.path.insert(0, str(ROOT / "scripts"))
import harvest_reddit as H  # noqa: E402  (import-time reads AIRTABLE_TOKEN + GROQ_API_KEY)

# --- spy on http_json to capture the outgoing prompt + full usage -------------
real_http = H.http_json
records = []


def spy(url, headers=None, data=None, method="GET"):
    resp = real_http(url, headers=headers, data=data, method=method)
    try:
        payload = json.loads(data)
        prompt = payload["messages"][0]["content"]
    except Exception:
        prompt = ""
    records.append({"prompt": prompt, "usage": resp.get("usage") or {}})
    return resp


H.http_json = spy

# --- two representative items: long news-style bodies, same rubric ------------
BODY_A = ("A deepfake video circulating on social media falsely showed the finance "
          "minister announcing a new investment scheme promising 40% monthly returns. "
          "Police say at least 200 victims transferred a combined $7.4 million before "
          "the clip was traced to an AI voice-cloning and face-swap toolkit. " * 4)[:1500]
BODY_B = ("Researchers at a university lab found that an AI hiring tool systematically "
          "down-ranked applicants whose names were not written in standard English "
          "characters, producing a measurable disparity in callback rates across "
          "otherwise identical resumes submitted over a six-month audit. " * 4)[:1500]

post_a = {"title": "Deepfake minister video drives $7.4M investment scam",
          "selftext": BODY_A, "provenance": "Google Alert",
          "source_label": "[Scams & fraud] deepfake investment scam",
          "external_url": "", "created": "2026-08-18"}
post_b = dict(post_a, title="AI hiring tool penalises non-English names, audit finds",
              selftext=BODY_B,
              source_label="[Bias & discrimination] AI hiring bias")


def show(tag, rec):
    u = rec["usage"]
    det = (u.get("prompt_tokens_details") or {})
    cached = det.get("cached_tokens", "MISSING")
    print(f"\n[{tag}]")
    print(f"  prompt chars      : {len(rec['prompt'])}")
    print(f"  prompt_tokens     : {u.get('prompt_tokens')}")
    print(f"  cached_tokens     : {cached}")
    print(f"  completion_tokens : {u.get('completion_tokens')}")
    print(f"  total_tokens      : {u.get('total_tokens')}")
    print(f"  prompt_time       : {u.get('prompt_time')}")
    print(f"  full usage        : {json.dumps(u)}")


GAP = 10  # seconds between calls — mimic production JUDGE_SLEEP so the cache can warm
print(f"Probing Groq prompt caching on {H.JUDGE_MODEL} (calls {GAP}s apart) ...")
H.groq_judge(post_a); time.sleep(GAP)     # call 1 — cold
H.groq_judge(post_a); time.sleep(GAP)     # call 2 — identical prompt
H.groq_judge(post_b); time.sleep(GAP)     # call 3 — same rubric, different item
H.groq_judge(post_a)                      # call 4 — A again, cache fully warm

labels = ["call1 A cold", "call2 A identical", "call3 B same-rubric", "call4 A warm"]
for lbl, rec in zip(labels, records):
    show(lbl, rec)

print("\n--- verdict ---")
def cached_of(i):
    return (records[i]["usage"].get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
for i, lbl in enumerate(labels):
    if i < len(records):
        print(f"{lbl}: cached_tokens={cached_of(i)}  prompt_time={records[i]['usage'].get('prompt_time')}")
warm = max((cached_of(i) for i in range(1, len(records))), default=0)
if warm > 0:
    print(f"=> CACHING WORKS: up to {warm} prefix tokens cached on a warm call. Those are NOT "
          "counted toward the rate-limit bucket, so the fixed rubric is ~free after the first call.")
else:
    print("=> Still no cache hit even with spacing. Groq is not caching our judge prefix "
          "(no prompt_tokens_details returned). Prefix-caching won't help the token budget here.")
