"""v3 test 1: the same-event duplicate check, on the stories already in Airtable. Reads Airtable, never writes it.

Answer key: the 83 duplicates noted on 2026-10-30 (tests/data/duplicates-2026-10-30.csv), 42 groups, each
naming the story to keep. Two stories in one group are the same event; any other pair counts as different.
The key was made by reading titles and summaries, so it isn't exhaustive: every disagreement is listed in the
results so a person can check it.

Measures:
  1. code step (no AI): how many of the key's pairs the rare-word finder puts forward at all;
  2. the cheap model's yes/no on every pair put forward, against the key.
Pass mark (proposal, 2026-11-02): it agrees with the key on at least 9 in 10 pairs, and rarely calls two
different events the same.
Cost: about 40K tokens on the triage model (about 300 pairs, 8 per call).
Run (GitHub Actions, label "v3-duplicate-test" on the PR):  python tests/duplicate_test.py
"""
import os, sys, csv, json, pathlib, itertools, urllib.parse
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
os.environ["AIRTABLE_READONLY"] = "1"
import harvest_reddit as H
import events

OUT = pathlib.Path(os.environ.get("RESULTS", HERE / "results"))
MAX_PAIRS = int(os.environ.get("MAX_PAIRS", "300"))


def stories():
    out, offset = [], None
    while True:
        u = (f"https://api.airtable.com/v0/{H.BASE}/{H.TABLE}?pageSize=100"
             + "".join(f"&fields%5B%5D={urllib.parse.quote(f)}" for f in ("Title", "Source headline", "Summary", "Source URL"))
             + (f"&offset={offset}" if offset else ""))
        data = H.http_json(u, headers={"Authorization": f"Bearer {H.AIRTABLE_TOKEN}"})
        out.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            return out


def key_groups():
    groups = {}
    with open(HERE / "data" / "duplicates-2026-10-30.csv", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            groups.setdefault(r["Keep row"], {r["Keep row"]}).add(r["Duplicate row"])
    return list(groups.values())


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    recs = stories()
    ids = [r["id"] for r in recs]
    pos = {rid: i for i, rid in enumerate(ids)}
    docs = []
    for r in recs:
        f = r.get("fields", {})
        docs.append(events.doc(f.get("Source headline") or f.get("Title"), f.get("Summary"), f.get("Source URL"), "new"))
    group_of = {}
    for g_n, g in enumerate(key_groups()):
        for rid in g:
            group_of[rid] = g_n
    key_pairs = set()
    for g in key_groups():
        present = sorted(pos[r] for r in g if r in pos)
        key_pairs.update(itertools.combinations(present, 2))

    def truth(a, b):
        return ids[a] in group_of and group_of.get(ids[a]) == group_of.get(ids[b])

    pairs = events.candidate_pairs(docs, MAX_PAIRS)
    asked = {(a, b) for a, b, _s in pairs}
    found = key_pairs & asked
    print(f"{len(docs)} stories; key has {len(key_pairs)} same-event pairs; the code put forward {len(pairs)} pairs, "
          f"{len(found)} of the key's.")
    same, tokens = events.ask_same(pairs, docs, H._triage_chat, sleep=H.TRIAGE_SLEEP)
    agree = sum(1 for (a, b), v in same.items() if v == truth(a, b))
    false_same = [(a, b) for (a, b), v in same.items() if v and not truth(a, b)]
    missed = [(a, b) for (a, b), v in same.items() if not v and truth(a, b)]
    never_asked = sorted(key_pairs - asked)

    def show(a, b):
        return {"A": docs[a]["title"], "A link": docs[a]["url"], "B": docs[b]["title"], "B link": docs[b]["url"]}
    result = {
        "stories": len(docs), "key_pairs": len(key_pairs), "pairs_asked": len(pairs),
        "key_pairs_put_forward_by_code": len(found), "tokens": tokens,
        "agree_with_key": agree, "agree_share": round(agree / len(same), 3) if same else None,
        "ai_said_same_key_says_different": [show(a, b) for a, b in false_same],
        "ai_said_different_key_says_same": [show(a, b) for a, b in missed],
        "key_pairs_the_code_never_put_forward": [show(a, b) for a, b in never_asked],
    }
    (OUT / "duplicate-test.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"AI agreed with the key on {agree}/{len(same)} pairs; said 'same' for {len(false_same)} the key calls "
          f"different; said 'different' for {len(missed)} the key calls same; {len(never_asked)} key pairs never "
          f"put forward by the code. {tokens} tokens. Results: {OUT / 'duplicate-test.json'}")


if __name__ == "__main__":
    main()
