"""One cheap question to Airtable before the daily site rebuild: has any story been edited since the site was
last rebuilt? A rebuild re-reads every story (several requests), and Airtable's plan limits requests per month,
so the daily rebuild only runs when the answer is yes (e.g. a Sensitive story approved or a story set to
Rejected). Writes `changed=true|false` to $GITHUB_OUTPUT.

"Since the last rebuild" = since the later of the last commit that changed site/stories.json and 25 hours ago
(the previous daily check), so an edit that did not change the site is not rebuilt for again every day.
If the question itself fails, the answer is "no": the rebuild would fail the same way.
"""
import os, sys, json, datetime, subprocess, urllib.request, urllib.parse

BASE = "appEiqYd3rnYwUNE7"
TABLE = "tblnfUTaXWRD3VYk6"   # Stories


def since():
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)
    try:
        last = subprocess.run(["git", "log", "-1", "--format=%cI", "--", "site/stories.json"],
                              capture_output=True, text=True, check=True).stdout.strip()
        if last:
            t = max(t, datetime.datetime.fromisoformat(last).astimezone(datetime.timezone.utc))
    except Exception:
        pass
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def changed():
    formula = f"IS_AFTER(LAST_MODIFIED_TIME(), '{since()}')"
    url = (f"https://api.airtable.com/v0/{BASE}/{TABLE}?maxRecords=1&fields%5B%5D=Title"
           f"&filterByFormula={urllib.parse.quote(formula)}")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return bool(json.loads(r.read()).get("records"))
    except Exception as e:
        print(f"Could not ask Airtable ({e}); skipping the rebuild.")
        return False


if __name__ == "__main__":
    ans = changed()
    print("Stories edited since the last rebuild: " + ("yes, rebuilding." if ans else "no, nothing to rebuild."))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"changed={'true' if ans else 'false'}\n")
    sys.exit(0)
