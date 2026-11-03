"""Same-event duplicate check (v3, rulebook section 3 and 8). Two stories about the same event (same people,
place and incident) are one story: the more reputable outlet is kept, and the others count as extra coverage
for the weekly ranking. A later development is a new story only if it adds a real new fact (owner, 2026-10-30).

Cheapest first:
  1. code, no AI: find pairs that share rare words (names, places, amounts) across today's stories, the week's
     queue and the last 60 days in Airtable;
  2. the cheap triage model answers "same event?" for those pairs only, a few pairs per call.
When the AI fails, the pair counts as different: a missed duplicate costs a little, a lost story costs more.
"""
import re, math, json, time
from collections import Counter, defaultdict

STOP = set("""
about above after again against also among another around because been before being below between both
called could does doing down during each even every from further have having here into itself just like
made make makes many more most much must near never news only other over said says same says should since
some such than that their them then there these they this those through under until upon very want were
what when where which while whom will with within without would year years your week weeks today told
report reports reported according people company companies first last time new news latest update
artificial intelligence using used uses tool tools system systems chatbot chatbots model models generative
openai chatgpt google meta microsoft says amid could""".split())
PAIR_BATCH = 8           # pairs per AI call (each pair ~ 2 headlines + 2 snippets)
SNIPPET = 400            # characters of each story the AI sees
PER_NEW = 8              # closest candidates kept for each new story
RARE_SHARE = 0.03        # a word counts as rare if it is in at most this share of the stories
RANK = "rare"            # how likely pairs are ordered: rare | cosine | title (see candidate_pairs)
_WORD = re.compile(r"[a-z0-9][a-z0-9'’.$£€-]*[a-z0-9]")


def tokens(text):
    out = set()
    for w in _WORD.findall((text or "").lower()):
        w = w.replace("’", "'").removesuffix("'s")
        if w in STOP:
            continue
        if w[0].isdigit():
            if len(w) >= 2 and w not in ("2024", "2025", "2026", "2027"):
                out.add(w)
        elif len(w) >= 4:
            out.add(w)
    return out


def doc(title, text, url, kind, ref=None):
    """One story to compare: kind is 'new', 'queue' or 'airtable'; ref points back to the item."""
    return {"title": title or "", "text": (text or "")[:600], "url": url or "", "kind": kind, "ref": ref,
            "tok": tokens(f"{title} {(text or '')[:600]}"), "ttok": tokens(title)}


def _cos(a, b, w):
    """Overlap of two word sets, weighted by rarity, 0 to 1 (1 = the same words)."""
    shared = sum(w.get(t, 0) ** 2 for t in a & b)
    if not shared:
        return 0.0
    return shared / math.sqrt(sum(w.get(t, 0) ** 2 for t in a) * sum(w.get(t, 0) ** 2 for t in b))


def candidate_pairs(docs, max_pairs, min_shared=2):
    """Pairs (i, j, score) worth asking about. At least one side is a new story. Rare = a word in at most
    3% of the stories (at least 3), so a big event's own words still count. Pairs must share min_shared rare
    words; they are then ordered by RANK:
      rare   - summed rarity of the shared rare words (favours long texts on similar topics);
      cosine - how alike the two stories' words are overall, weighted by rarity (0 to 1);
      title  - the same for the two headlines, counted twice, plus the overall likeness."""
    n = len(docs)
    if n < 2:
        return []
    df = Counter(t for d in docs for t in d["tok"])
    rare_cut = max(3, int(n * RARE_SHARE))
    idf = {t: math.log(n / c) for t, c in df.items() if c <= rare_cut}
    w = {t: math.log(n / c) for t, c in df.items()}
    rare = [{t for t in d["tok"] if t in idf} for d in docs]
    index = defaultdict(list)
    for i, r in enumerate(rare):
        for t in r:
            index[t].append(i)
    best = {}
    for i, d in enumerate(docs):
        if d["kind"] != "new":
            continue
        shared = Counter()
        for t in rare[i]:
            for j in index[t]:
                if j != i:
                    shared[j] += 1
        for j, c in shared.items():
            if c < min_shared:
                continue
            a, b = min(i, j), max(i, j)
            if RANK == "cosine":
                score = _cos(docs[i]["tok"], docs[j]["tok"], w)
            elif RANK == "title":
                score = 2 * _cos(docs[i]["ttok"], docs[j]["ttok"], w) + _cos(docs[i]["tok"], docs[j]["tok"], w)
            else:
                score = sum(idf[t] for t in rare[i] & rare[j])
            best[(a, b)] = max(best.get((a, b), 0), score)
    # Each new story keeps its PER_NEW strongest candidates, then the day's cap keeps the strongest overall.
    per_new = defaultdict(list)
    for (a, b), s in best.items():
        for k in (a, b):
            if docs[k]["kind"] == "new":
                per_new[k].append((s, a, b))
    chosen = set()
    for k, lst in per_new.items():
        for s, a, b in sorted(lst, reverse=True)[:PER_NEW]:
            chosen.add((a, b, s))
    return sorted(chosen, key=lambda x: -x[2])[:max_pairs]


def pair_prompt(pairs, docs):
    lines = []
    for n, (a, b, _s) in enumerate(pairs, 1):
        A, B = docs[a], docs[b]
        lines.append(f"{n}. A: {A['title'][:160]} — {A['text'][:SNIPPET]}\n   B: {B['title'][:160]} — {B['text'][:SNIPPET]}")
    return ("For each numbered pair of news stories, answer whether A and B report the SAME EVENT: the same "
            "people, place and incident. A later development (a ruling, a settlement, charges, a conviction) that "
            "adds a real new fact is NOT the same event. Two similar incidents involving different people or "
            "places are NOT the same event. When unsure, answer false.\n"
            'Return JSON {"results":[{"n":1,"same":true},...]} covering every pair.\n\n' + "\n".join(lines))


def ask_same(pairs, docs, chat, sleep=9):
    """{(a, b): True/False} from the cheap model, PAIR_BATCH pairs per call. chat(prompt) -> (content, tokens, status)."""
    out, spent = {}, 0
    for i in range(0, len(pairs), PAIR_BATCH):
        chunk = pairs[i:i + PAIR_BATCH]
        content, tok, _status = chat(pair_prompt(chunk, docs))
        spent += tok
        verdicts = {}
        if content:
            try:
                verdicts = {int(r["n"]): r.get("same") is True
                            for r in json.loads(content).get("results", []) if "n" in r}
            except Exception:
                verdicts = {}
        for n, (a, b, _s) in enumerate(chunk, 1):
            out[(a, b)] = verdicts.get(n, False)        # missing or failed = different (keep both)
        if i + PAIR_BATCH < len(pairs):
            time.sleep(sleep)
    return out, spent


def groups(n, same_pairs):
    """Union-find: list of index groups of size > 1."""
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in same_pairs:
        parent[find(a)] = find(b)
    g = defaultdict(list)
    for i in range(n):
        g[find(i)].append(i)
    return [v for v in g.values() if len(v) > 1]


def resolve(docs, same_groups, approved, host):
    """Decide each same-event group. Returns (drop_new, twins, upgrades, outlets):
      drop_new - new doc indexes to remove: only copies of a story already in Airtable;
      twins    - {doc index: kept doc index} for the other copies. They are flagged, not dropped (owner,
                 2026-11-03), so a wrong "same" costs a judge slot, not a story: twins are judged last and
                 the weekly pick takes one story per event;
      upgrades - {queue doc index: new doc index} where a queued story (already passed the judge) takes the
                 link of a new copy from an approved outlet, so a passed story is never thrown away for an
                 unjudged one;
      outlets  - {kept doc index: every outlet that covered the event} for the weekly ranking.
    A story already in Airtable always wins (the new copies are dropped). Among new stories only, the approved
    outlet wins, then the longer text."""
    drop_new, twins, upgrades, outlets = set(), {}, {}, {}
    for g in same_groups:
        hosts = {host(docs[i]["url"]) for i in g if docs[i]["url"]}
        for i in g:
            hosts.update(docs[i].get("outlets") or [])
        if any(docs[i]["kind"] == "airtable" for i in g):
            drop_new.update(i for i in g if docs[i]["kind"] == "new")
            continue
        queued = [i for i in g if docs[i]["kind"] == "queue"]
        rank = lambda i: (approved(docs[i]["url"]), len(docs[i]["text"]))
        if queued:
            keep = max(queued, key=rank)
            better = [i for i in g if docs[i]["kind"] == "new" and approved(docs[i]["url"])]
            if better and not approved(docs[keep]["url"]):
                upgrades[keep] = max(better, key=rank)
        else:
            keep = max(g, key=rank)
        for i in g:
            if i != keep:
                twins[i] = keep
        outlets[keep] = hosts
    return drop_new, twins, upgrades, outlets
