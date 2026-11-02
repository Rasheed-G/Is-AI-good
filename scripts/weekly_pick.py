"""Sunday's pick (v3, rulebook section 8). The daily harvest judges what's new and queues the stories that
pass the bar (pipeline_data.py). Once a week this script ranks the week's passes, keeps the best per theme,
writes their card copy and stores them in Airtable as Candidates; publish.py then decides what goes live.

Ranking ("Weekly ranking" setting): real consequence + wider significance, plus a boost for each other
outlet that covered the same event (counted by the daily same-event check). The video slot rule still applies.
Passes left out are saved to pipeline-data/not-picked/, not to Airtable (row cap).

Calls Groq (card copy), so it runs in GitHub Actions.
Run:  python scripts/weekly_pick.py   (DRY_RUN=1: rank and print only, write nothing)
"""
import os, re, time
from collections import Counter
import harvest_reddit as H
import pipeline_data, outlets, rulebook

_nums = [int(n) for n in re.findall(r"\d+", rulebook.setting("Weekly ranking"))]
BOOST_PER_OUTLET, BOOST_CAP = (_nums + [5, 20])[:2]


def rank(q):
    t = q["t"]
    extra = max(0, len(q.get("outlets") or []) - 1)
    return H._score(t, "consequence") + H._score(t, "significance") + min(BOOST_CAP, BOOST_PER_OUTLET * extra)


def notes(q):
    """Editor notes for a picked story: where it came from, who else covered it, any better-source swap."""
    p = q["p"]
    parts = [f"Auto-harvested from {p.get('source_label') or 'feed'}."]
    others = [o for o in (q.get("outlets") or []) if o and o != outlets.host(H.article_url(p))]
    if others:
        parts.append(f"Also covered by: {', '.join(others[:8])}.")
    if q.get("swapped_from"):
        parts.append(f"Better source: moved from {q['swapped_from']}.")
    return " ".join(parts)[:900]


def main():
    dry = os.environ.get("DRY_RUN") == "1"
    deadline = time.time() + H.MAX_RUNTIME_MIN * 60
    queue = pipeline_data.load_queue()
    print(("*** DRY RUN — ranking only, nothing written ***\n" if dry else "")
          + f"Weekly pick: {len(queue)} stories passed the bar this week.")
    passed = []
    for q in queue:
        if q.get("theme") not in H.THEMES:
            continue
        q["p"].setdefault("over_18", False)
        q["p"].setdefault("created", "")
        passed.append({"p": q["p"], "t": q["t"], "rel": rank(q), "theme": q["theme"], "q": q})
    selected, _dropped = H.select_best(passed)
    # A story written to Airtable since it was queued (by hand or a re-run) is not written twice.
    known = H.existing_source_urls()
    selected = [it for it in selected if not (H.dedup_keys(it["p"]) & known)]
    by_theme = Counter(it["theme"] for it in selected)
    print(f"  Picked {len(selected)} (best {H.MAX_PER_THEME} per theme): "
          + ", ".join(f"{th} {n}" for th, n in sorted(by_theme.items())))
    for it in selected:
        q = it["q"]
        print(f"  [{it['rel']:>3}] {it['theme'][:22]:<22} outlets {len(q.get('outlets') or []):>2} "
              f"{'approved ' if q.get('approved') else ''}{it['p']['title'][:60]}")
    if dry:
        print("\nDRY RUN complete. Nothing written.")
        return

    tok_copy = H.retry_held_summaries(deadline)       # cards held because their summary failed
    dedup_sigs = H.existing_signatures()
    kept, kept_posts, n_reprint = [], [], 0
    for it in selected:
        if time.time() > deadline:
            print("  ! Time budget reached while writing card copy — the rest stay queued.")
            break
        title, summary, same_event, ctok = H.write_copy(it["p"])
        tok_copy += ctok
        time.sleep(H.COPY_SLEEP)
        dup, match = H.is_reprint(it["p"]["title"], summary, dedup_sigs) if summary else (False, "")
        if dup:
            n_reprint += 1
            print(f"  ⊘ reprint (skipped) — {it['p']['title'][:50]} ≈ {match[:40]}")
            it["q"]["written"] = True
            continue
        fields = H.build_fields(it["p"], it["t"], title, summary, same_event)
        fields["Editor notes"] = notes(it["q"])
        dedup_sigs.append((H.norm_title(it["p"]["title"]), (summary or "").lower().strip()))
        kept.append({"fields": fields})
        kept_posts.append(it["p"])
        it["q"]["written"] = True
        held = f" (held: {fields['Hold reason']})" if fields.get("Hold reason") else ""
        print(f"  + [{it['rel']}] {it['theme']} — {(title or it['p']['title'])[:60]}{held}")
    if kept:
        created = H.create_records(kept)
        H.reenrich_new_rows(created, kept_posts)

    # Picked stories leave the queue; anything picked but not reached (time) stays for next week.
    reached = {id(it["q"]) for it in selected if it["q"].get("written")}
    picked = {id(it["q"]) for it in selected}
    pipeline_data.save_not_picked([q for q in queue if id(q) not in picked])
    pipeline_data.save_queue([q for q in queue if id(q) in picked and id(q) not in reached])
    # Per-source "Kept" counts are recomputed from Stories by the next daily run, so nothing to do here.
    H.write_run({
        "run": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "mode": "Weekly pick",
        "tried": 0, "read": 0, "failed_n": 0, "failed": [], "fetched": 0, "judged": 0,
        "kept": len(kept), "errors": 0, "tokens": tok_copy, "tok_triage": 0, "tok_judge": 0, "tok_copy": tok_copy,
        "judge_mode": "", "passed": len(queue), "held": sum(1 for k in kept if k["fields"].get("Hold reason")),
        "queued": len(queue) - len(picked),
    })
    print(f"\nDone. {len(kept)} stories written as Candidates, {n_reprint} reprints skipped, "
          f"{len(queue) - len(picked)} not picked (saved to not-picked/), {tok_copy} copy tokens.")


if __name__ == "__main__":
    main()
