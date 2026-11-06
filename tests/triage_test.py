"""v3 test 3: does triage throw away real stories? Reads Airtable, never writes it.

  1. A practice sweep: collect today's stories and run triage exactly as the daily harvest would
     (TRIAGE_ONLY=1, the same code), saving what it kept and dropped to the results folder.
  2. 50 random drops go to the judge (randomness off, approved outlets trusted, as live).
     A drop the judge would Keep is a real story lost.
Pass mark (owner, 2026-11-02): at most 2 of 50 lost; if more, loosen triage.
Cost: practice sweep about 45K tokens on the triage model; the judge about 75K on the judge model.
Run (GitHub Actions, label "v3-triage-test" on the PR):  python tests/triage_test.py
"""
import os, sys, json, random, pathlib, time
HERE = pathlib.Path(__file__).resolve().parent
OUT = pathlib.Path(os.environ.get("RESULTS", HERE / "results"))
os.environ["AIRTABLE_READONLY"] = "1"
os.environ["TRIAGE_ONLY"] = "1"
os.environ["PIPELINE_DATA"] = str(OUT)
sys.path.insert(0, str(HERE.parent / "scripts"))
import harvest_reddit as H
import outlets

SAMPLE = int(os.environ.get("SAMPLE", "50"))
PASS_MARK = 2


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    H.main()                                           # stops after triage and saves its output (TRIAGE_ONLY)
    dropped = json.loads((OUT / "triage-dropped.json").read_text(encoding="utf-8"))
    kept = json.loads((OUT / "triage-kept.json").read_text(encoding="utf-8"))
    random.seed(20261102)
    sample = random.sample(dropped, min(SAMPLE, len(dropped)))
    for p in sample:
        p.setdefault("over_18", False)
    deadline = time.time() + 40 * 60
    sample = H.read_articles(sample, deadline)        # the judge reads the article, as live
    results, tokens = [], 0
    for i in range(0, len(sample), H.JUDGE_BATCH):
        chunk = sample[i:i + H.JUDGE_BATCH]
        verdicts, tok, status = H.groq_judge_batch(chunk)
        tokens += tok
        for p, t in zip(chunk, verdicts):
            if t:
                outlets.trust(t, H.article_url(p))
            results.append({"title": p["title"], "link": H.article_url(p), "source": p.get("source_label"),
                            "verdict": H.judge_verdict(t) if t else "Not judged", "theme": (t or {}).get("theme"),
                            "overall": H.overall_score(t), "reason": (t or {}).get("reason", "")})
        time.sleep(H.JUDGE_BATCH_SLEEP)
    lost = [r for r in results if r["verdict"] == "Keep"]
    judged = sum(1 for r in results if r["verdict"] != "Not judged")
    summary = {"pool": len(kept) + len(dropped), "triage_kept": len(kept), "triage_dropped": len(dropped),
               "sample": len(sample), "judged": judged, "real_stories_lost": len(lost),
               "pass_mark": f"at most {PASS_MARK} of {SAMPLE}", "passed": len(lost) <= PASS_MARK and judged >= SAMPLE * 0.9,
               "judge_tokens": tokens, "lost": lost, "all": results}
    (OUT / "triage-test.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nTriage kept {len(kept)} and dropped {len(dropped)} of {len(kept) + len(dropped)}. Of {judged} sampled "
          f"drops judged, {len(lost)} would have been kept (pass mark: at most {PASS_MARK}). {tokens} judge tokens.")
    for r in lost:
        print(f"  LOST [{r['overall']}] {r['theme']}: {r['title'][:80]}")


if __name__ == "__main__":
    main()
