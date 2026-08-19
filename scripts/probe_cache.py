"""Probe v3: does Groq prompt caching give us real RATE-LIMIT relief on the judge?

Earlier probes checked usage.prompt_tokens_details.cached_tokens — but Groq may not
populate that field for gpt-oss (litellm #16129), so its absence proves nothing.
This measures the thing that actually matters: the TPM token bucket. Groq returns
`x-ratelimit-remaining-tokens` (TPM) on every response. We fire the SAME real judge
prompt back-to-back (best case for landing on a warm node) and watch how much the
bucket drops per call:
  - drop ~= total_tokens every call  -> NO caching relief (cache miss / node routing)
  - drop << total_tokens on repeats  -> caching IS exempting the cached prefix

Two bursts: our production temperature=0.2, then default temperature (community reports
custom temperature can suppress cache hits). Runs in the cloud (Groq 403s local IP).
No secrets printed.
"""
import os, sys, json, time, pathlib, urllib.request, urllib.error

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

# --- capture the REAL judge prompt + request headers via one spied call -------
grab = {}
_real = H.http_json
def spy(url, headers=None, data=None, method="GET"):
    r = _real(url, headers=headers, data=data, method=method)
    grab["prompt"] = json.loads(data)["messages"][0]["content"]
    grab["headers"] = headers
    return r
H.http_json = spy

BODY = ("A deepfake video circulating on social media falsely showed the finance "
        "minister announcing an investment scheme promising 40% monthly returns. Police "
        "say at least 200 victims transferred a combined $7.4 million before the clip was "
        "traced to an AI voice-cloning and face-swap toolkit. " * 4)[:1500]
post = {"title": "Deepfake minister video drives $7.4M investment scam", "selftext": BODY,
        "provenance": "Google Alert", "source_label": "[Scams & fraud] deepfake scam",
        "external_url": "", "created": "2026-08-18"}
H.groq_judge(post)                 # populate grab{}
H.http_json = _real
PROMPT, HDRS = grab["prompt"], grab["headers"]
print(f"Captured real judge prompt: {len(PROMPT)} chars. Model={H.JUDGE_MODEL}")

URL = "https://api.groq.com/openai/v1/chat/completions"

def raw_call(temperature):
    payload = {"model": H.JUDGE_MODEL,
               "messages": [{"role": "user", "content": PROMPT}],
               "temperature": temperature,
               "response_format": {"type": "json_object"}}
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), method="POST", headers=HDRS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            h, j = r.headers, json.loads(r.read())
            u = j.get("usage") or {}
            det = u.get("prompt_tokens_details") or {}
            return {"ok": True, "pt": u.get("prompt_tokens"), "tt": u.get("total_tokens"),
                    "cached": det.get("cached_tokens", None),
                    "rem_tok": h.get("x-ratelimit-remaining-tokens"),
                    "rem_req": h.get("x-ratelimit-remaining-requests")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "code": e.code,
                "rem_tok": e.headers.get("x-ratelimit-remaining-tokens")}

def burst(tag, temperature, n=9):
    print(f"\n=== BURST {tag}: {n} identical calls back-to-back, temperature={temperature} ===")
    prev = None
    for i in range(n):
        r = raw_call(temperature)
        if not r["ok"]:
            print(f"  call{i+1}: HTTP {r['code']} (rem_tok={r['rem_tok']}) — bucket hit, stopping burst")
            break
        rt = int(r["rem_tok"]) if r["rem_tok"] is not None else None
        drop = (prev - rt) if (prev is not None and rt is not None) else None
        print(f"  call{i+1}: total_tokens={r['tt']}  cached_tokens={r['cached']}  "
              f"rem_TPM={rt}  rem_RPD={r['rem_req']}  bucket_drop={drop}")
        prev = rt
    print("  (bucket_drop << total_tokens on repeats => caching relief; ~equal => none)")

burst("A (production temp)", 0.2)
print("\n… sleeping 65s to let the TPM bucket refill before the next burst …")
time.sleep(65)
burst("B (default temp)", 1.0)

print("\n--- how to read this ---")
print("If every bucket_drop ~= total_tokens (~1700), caching gives us NO rate-limit relief")
print("in our real call pattern. If drops fall to a few hundred on repeat calls, caching works.")
