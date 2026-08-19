"""Did the batched judge work? Reads the latest Runs rows and prints a verdict.

Signals that matter (from the Runs table + the known pre-batch baseline):
  - Judge mode        -> confirms batching actually ran (expect 'batch-4')
  - Tokens B / Judged  -> per-judge cost. Pre-batch baseline ~1657 tok/judge (measured
                         2026-08-19); target with batch-4 is ~700-800 (~58% less).
  - Judged            -> should be higher (whole pool judged, not early-stopped at 44m)
  - Errors            -> should stay low
NB: runs before 2026-08-19 have blank Tokens columns (the columns didn't exist yet), so
the comparison is against the measured baseline, not an older row. Airtable only, no Groq.
Run: python scripts/check_morning_run.py
"""
import os, json, urllib.request, pathlib

SINGLE_BASELINE = 1657   # measured tok/judge on the single-item judge, 2026-08-19

for line in (pathlib.Path(".env.local").read_text().splitlines()
             if pathlib.Path(".env.local").exists() else []):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

BASE, RUNS = "appEiqYd3rnYwUNE7", "tblR1wvxB46FtwMia"
url = (f"https://api.airtable.com/v0/{BASE}/{RUNS}?pageSize=8"
       "&sort%5B0%5D%5Bfield%5D=Run&sort%5B0%5D%5Bdirection%5D=desc")
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}"})
rows = [r.get("fields", {}) for r in json.loads(urllib.request.urlopen(req).read()).get("records", [])]


def per_judge(f):
    b, j = f.get("Tokens B (judge)") or 0, f.get("Judged") or 0
    return (b / j) if (b and j) else None


def cell(v):
    return "-" if v in (None, "") else str(v)


print(f"{'Run':<20} {'Mode':<9} {'Judged':>6} {'TokensB':>8} {'tok/jdg':>7} {'Kept':>5} {'Err':>4}")
for f in rows:
    pj = per_judge(f)
    print(f"{cell(f.get('Run')):<20} {cell(f.get('Judge mode')):<9} {cell(f.get('Judged')):>6} "
          f"{cell(f.get('Tokens B (judge)')):>8} {(f'{pj:.0f}' if pj else '-'):>7} "
          f"{cell(f.get('Kept')):>5} {cell(f.get('Errors')):>4}")

batch_run = next((f for f in rows if "batch" in str(f.get("Judge mode", "")).lower()), None)
print("\n=== VERDICT ===")
if not batch_run:
    print("  [!] No batch-mode run in the Runs table yet. Latest Judge mode =",
          cell(rows[0].get("Judge mode")) if rows else "(none)")
    print("      The morning batched harvest may not have run yet.")
else:
    print(f"  [OK] Batched run present (Judge mode = {batch_run.get('Judge mode')}).")
    pj = per_judge(batch_run)
    if pj:
        saving = 100 * (1 - pj / SINGLE_BASELINE)
        tag = "[OK] WORKED" if pj < 1000 else "[!] higher than expected"
        print(f"  - Judge cost: {pj:.0f} tok/judge  vs ~{SINGLE_BASELINE} single baseline"
              f"  ->  {saving:.0f}% saving   {tag}")
    else:
        print("  [!] Tokens B (judge) is blank - can't compute per-judge cost.")
    print(f"  - Judged {cell(batch_run.get('Judged'))} | Kept {cell(batch_run.get('Kept'))} | "
          f"Errors {cell(batch_run.get('Errors'))}")
    if (batch_run.get("Errors") or 0) > 5:
        print("  [!] error count is high - check the Actions log for judge failures.")

print("\n  Then eyeball the Actions log lines:")
print("   - 'Stage-B judged X/Y survivors across N call(s)'  (X == Y => whole pool judged, no early stop)")
print("   - any 'Time budget reached during judging' or 'rate-limited' lines (should be absent)")
