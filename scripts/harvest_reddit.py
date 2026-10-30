"""Story harvester for Is AI Good? (isaigood.org).

Reads the owner's Active sources from Airtable (Reddit, Google Alerts, news feeds, YouTube),
drops what's too old, already stored or off topic, then asks Groq to triage, judge and write the
card copy, following the editorial rulebook (rulebook.md, read by scripts/rulebook.py). The best
stories per theme are written to Airtable as `Candidate` rows; publish.py decides what goes live.

Calls Groq, so it runs in GitHub Actions (Groq blocks the owner's computer).
Run:  source .env.local && python scripts/harvest_reddit.py   (DRY_RUN=1: judge only, write nothing)
"""
import os, sys, json, time, html, re, random, datetime, difflib, email.utils, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from collections import Counter
import rulebook   # rulebook.md: the one home for themes, rules and settings

# Force UTF-8 (Windows cp1252 would crash on unicode titles) and LINE buffering so
# output streams live to CI logs instead of being lost if the job is killed.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

# --- config -----------------------------------------------------------------
AIRTABLE_TOKEN = os.environ["AIRTABLE_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
YOUTUBE_KEY = os.environ.get("YOUTUBE_API_KEY", "")   # optional: enables Lane 2 (YouTube search)
BASE = "appEiqYd3rnYwUNE7"
TABLE = "tblnfUTaXWRD3VYk6"
# Three-stage model pipeline — each stage runs on its OWN Groq per-model rate bucket
# (30 RPM / 1K RPD / 8K TPM / 200K TPD each), so they don't share budget. See
# docs/groq-models.md for the full table + sources. NB the old llama-3.3-70b-versatile
# and llama-3.1-8b-instant were BOTH deprecated (shutdown ~08/16/26) — do not resurrect.
TRIAGE_MODEL = "openai/gpt-oss-20b"    # Stage A: cheap recall-biased junk drop (batched)
JUDGE_MODEL  = "openai/gpt-oss-120b"   # Stage B: keep/reject + theme + score + sensitivity
COPY_MODEL   = "qwen/qwen3.8-27b"      # Stage C: card summary, keepers only (qwen3.6-27b decommissioned 09/14/26)
GROQ_MODEL = JUDGE_MODEL               # back-compat alias for any external caller

# Sources (subreddits / keywords / feeds) live in the Airtable "Sources" table so the
# owner can add or pause them with NO code. The harvester reads Active rows each run.
SOURCES_TABLE = "tbllWd5Rby0pkgIpB"
RUNS_TABLE = "tblR1wvxB46FtwMia"
SWEEP = os.environ.get("SWEEP") == "1"   # weekly top-of-week sweep (vs the daily "new" run)
# Option A (social clips). When a Reddit post links an Instagram/TikTok/YouTube clip we
# surface that embed as the story's media instead of the article/text (always on — see
# _pick_external). CLIPS_ONLY additionally DROPS Reddit posts that have no such clip,
# making the site clip-first. Default OFF = "enrich" mode (clips preferred, text kept).
CLIPS_ONLY = os.environ.get("CLIPS_ONLY", "").strip().lower() in ("1", "true", "yes", "on")

THEMES = rulebook.theme_names()   # the 13 themes, from rulebook.md section 2

# Recency window (A): each run only looks at RECENT posts, so we don't re-judge stale
# items every day (rejected posts aren't stored, so without this they'd be re-judged
# forever). Daily = last 2 days; weekly sweep = last 7. Reddit's `t` param + YouTube's
# publishedAfter bound server-side; feeds/`new.rss` are filtered client-side by date.
WINDOW_DAYS = 7 if SWEEP else 2
REDDIT_T = "week" if SWEEP else "day"   # Reddit `top`/search time filter (hour/day/week/month/year/all)
PER_SOURCE = 25           # max posts fetched per source-pass (the window bounds the rest, not this)
# Theme-balanced writing (C): spread candidates across the themes rather than letting
# one loud theme fill the batch. Per-theme cap + an overall ceiling; judge widely so every
# theme gets a look. Actual judged count is now bounded by how many NEW items the window holds.
MAX_PER_THEME = 4         # keep the BEST N per theme (by relevance) after judging everything
MAX_NEW = len(THEMES) * MAX_PER_THEME   # overall ceiling on new candidates per run (13 themes × 4)
MAX_JUDGE = 250           # safety ceiling on judge calls (a runaway pool); the real bound is now
                          # the judge's 200K-TPD bucket (~168 judges) + the wall-clock budget.
                          # Selection is judge-EVERYTHING-then-pick-best-per-theme, not cap-early.
STATS_WINDOW_SHORT = 10   # short rolling window for per-source "Found (10d)"/"Kept (10d)" yield
STATS_WINDOW_LONG = 30    # long rolling window for per-source "Found (30d)"/"Kept (30d)" yield
YT_SEARCH_DURATIONS = ("short", "medium")  # Lane 2: short <4m (incl Shorts) + medium 4–20m (segments)
YT_PER_QUERY = 6          # Lane 2: results fetched per duration pass, per query
# Stage A (pre-triage) knobs — batched so ~hundreds of posts cost ~tens of cheap 20b calls.
TRIAGE_BATCH = 15         # posts per pre-triage call (keep batch tokens < 8K TPM)
TRIAGE_SNIPPET = 200      # chars of body per item shown to the pre-filter
MAX_TRIAGE = 1000         # safety ceiling; set well above a realistic pool so Stage A screens
                          # the WHOLE pool (nothing reaches the judge un-screened)
REDDIT_SLEEP = 12         # seconds between Reddit fetches. NB: cloud IPs get IP-throttled by
                          # Reddit regardless of spacing, so keep runs short enough to finish + log.
# Per-stage Groq pacing. Each model has its own 8K-TPM bucket, so stages don't share a budget;
# spacing just keeps each stage under its own TPM. Tune per model in the refine phase.
TRIAGE_SLEEP = 9          # 20b, batched ~1–2K tok/call
JUDGE_SLEEP  = 8          # 120b, ~900 tok/call (single-item path)
COPY_SLEEP   = 6          # qwen, ~700 tok/call, low volume (keepers only)
GROQ_SLEEP = JUDGE_SLEEP  # back-compat alias
# Stage B (judge) batching — send N items per call under ONE rubric copy (like Stage A). The
# ~1,090-token rubric travels once per batch instead of once per item, cutting judge tokens ~58%
# and ~doubling throughput under the TPM ceiling (A/B validated 2026-08-19: 12/12 keep-agreement
# on clear items; borderline errors mostly systematic but low-stakes). JUDGE_BATCH=1 (env) reverts
# to the proven single-item path instantly — no code change.
JUDGE_BATCH = int(os.environ.get("JUDGE_BATCH", "4"))
JUDGE_BATCH_SLEEP = int(os.environ.get("JUDGE_BATCH_SLEEP", "20"))  # sec between batch calls.
                          # A batch ≈ 4×~650 = ~2.6K tok; at the old 15s that's ~10.3K tok/min,
                          # OVER the 120b's 8K-TPM bucket — the 08-20 run thrashed ~20 min in 30s
                          # backoff and left 29/189 survivors unjudged. 20s (~7.8K tok/min) keeps
                          # it just under the ceiling. Retune if tok/judge or JUDGE_BATCH changes.
ENRICH_RETRY_DELAY = int(os.environ.get("ENRICH_RETRY_DELAY", "15"))  # sec to wait before the
                          # one post-write retry of media on rows that came out bare (see
                          # reenrich_new_rows). Long enough for a transient block/throttle to clear;
                          # scoped to THIS run's new rows only, so it stays cheap.
MAX_RUNTIME_MIN = 70      # hard wall-clock budget (CI timeout is 80m): stop gracefully + log.
                          # 38→44 (added Stage-A/C); 44→70 once judging went batched — the ceiling
                          # only CAPS a run (a normal run ends when work is done), so on the public
                          # repo (unlimited Actions minutes) a generous ceiling costs nothing on
                          # normal days and guarantees a FULL survivor pool is judged even if fetch
                          # or the pool runs large. Runaway is bounded by the rate-limit/error bails.
UA = "AIHarmWatch/0.1 (personal research tool; contact: owner)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# --- helpers ----------------------------------------------------------------
def http_json(url, headers=None, data=None, method="GET"):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def http_text(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")

_TAGS = re.compile(r"<[^>]+>")
ATOM = "{http://www.w3.org/2005/Atom}"

def source_urls(src):
    """RSS URL(s) to fetch for a Sources row. Subreddits get TWO passes — newest AND
    top-of-window — so popular posts aren't missed; keyword/feed are a single URL.
    (Date filtering to the window happens after fetch, in within_window.)"""
    name = (src.get("name") or "").strip()
    typ = src.get("type")
    if typ == "Subreddit":
        sub = urllib.parse.quote((name[2:] if name.lower().startswith("r/") else name).strip("/"))
        base = f"https://www.reddit.com/r/{sub}"
        top = base + "/top.rss?" + urllib.parse.urlencode({"t": REDDIT_T, "limit": PER_SOURCE})  # top of window
        if SWEEP:
            # Weekly backstop: newest + top-of-week (two passes) so nothing recent slips through.
            return [base + "/new.rss?" + urllib.parse.urlencode({"limit": PER_SOURCE}), top]
        # Daily: ONE pass, top-of-day only — upvote-filtered (higher signal, less junk) and
        # ~half the Reddit rate-limit backoff, which is the run's biggest time drain.
        return [top]
    if typ == "Keyword":
        return ["https://www.reddit.com/search.rss?" +
                urllib.parse.urlencode({"q": name, "sort": "new", "t": REDDIT_T, "limit": PER_SOURCE})]
    if typ == "RSS feed":
        return [src.get("url") or name]   # feeds carry their address in the URL field
    return []

def _cutoff():
    """ISO date (YYYY-MM-DD) WINDOW_DAYS ago — the recency floor for this run."""
    return (datetime.date.today() - datetime.timedelta(days=WINDOW_DAYS)).isoformat()

def within_window(post, cutoff):
    """Keep posts dated on/after the cutoff. Undated posts are kept (recall-biased —
    better to spend a judge call than silently drop a real story with a missing date)."""
    d = (post.get("created") or "").strip()[:10]
    return (not d) or (d >= cutoff)

def load_sources():
    """Active rows from the Airtable Sources table (owner-editable, no code).
    Airtable caps a page at 100 records and returns an `offset` when more remain, so we
    MUST follow it — otherwise sources past the 100th are silently never read (we have
    137 active). Mirrors the offset loop already used by existing_source_urls et al."""
    out, offset = [], None
    while True:
        url = (f"https://api.airtable.com/v0/{BASE}/{SOURCES_TABLE}?pageSize=100"
               f"&filterByFormula={urllib.parse.quote('{Active}=1')}")
        if offset:
            url += f"&offset={urllib.parse.quote(offset)}"
        data = http_json(url, headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"})
        for r in data.get("records", []):
            f = r.get("fields", {})
            if f.get("Name") and f.get("Type"):
                out.append({"name": f["Name"], "type": f["Type"], "url": f.get("URL", "")})
        offset = data.get("offset")
        if not offset:
            break
    return out

# Embeddable social clips (see app.js detectMedia). When a Reddit post links one of
# these, we prefer it over any article link so the story shows the clip, not the text.
# Match the SPECIFIC post/video permalink shapes app.js can actually embed — NOT a bare
# host. Platform browse/landing pages (tiktok.com/discover/…, /tag/…, an IG profile) share
# the host but carry no post id, so they must NOT be treated as clips or they render as
# broken cards. These regexes mirror app.js detectMedia one-for-one.
CLIP_URL_RES = (
    re.compile(r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)[A-Za-z0-9_-]{11}", re.I),
    re.compile(r"instagram\.com/(?:p|reel|tv)/[A-Za-z0-9_-]+", re.I),
    re.compile(r"tiktok\.com/(?:.*/video/|embed/v2/|embed/)\d+", re.I),
    re.compile(r"(?:twitter\.com|x\.com)/[^/]+/status/\d+", re.I),
)

def is_social_clip(url):
    """True if the URL is a specific, embeddable IG / TikTok / YouTube / X permalink —
    not a platform browse/landing page. Mirrors app.js detectMedia so whatever we store
    as Media URL is exactly what the site knows how to embed."""
    low = (url or "").strip()
    return any(rx.search(low) for rx in CLIP_URL_RES)

# A bare-IP host (http://166.88.134.62/…) is never a credible article source — it's
# spam / a self-hosted scraper repost / a malware bait link. A post whose outbound link
# is a raw IP is DROPPED before judging (see the spam-link filter in the harvest loop),
# so it never reaches the site — not merely re-sourced to the Reddit thread.
_IP_HOST = re.compile(r"^https?://\d{1,3}(?:\.\d{1,3}){3}(?:[:/]|$)", re.I)

def is_ip_url(url):
    """True if the URL's host is a bare IPv4 address (a spam/malware signal here)."""
    return bool(_IP_HOST.match((url or "").strip()))

def _pick_external(content_html):
    """Choose the post's outbound link. Prefer an embeddable clip (IG/TikTok first,
    then YouTube) over a generic article link, so a Reddit post that links BOTH a
    clip and an article surfaces the clip as the story's media (Option A). Raw-IP links
    ARE returned here (not hidden) so the harvest loop's spam-link filter can see and
    reject the whole post."""
    hrefs = [html.unescape(h) for h in re.findall(r'href="([^"]+)"', content_html)
             if "reddit.com" not in h.lower()]
    for tier in (("instagram.com", "tiktok.com"), ("youtube.com", "youtu.be")):
        for h in hrefs:
            if any(host in h.lower() for host in tier):
                return h
    return hrefs[0] if hrefs else ""

def fetch_atom(url, label):
    """Fetch + parse a Reddit RSS/Atom feed into post dicts. No app/OAuth needed."""
    root = None
    for attempt in range(3):
        try:
            xml = http_text(url, headers={"User-Agent": UA})
            root = ET.fromstring(xml)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                wait = 15 * (attempt + 1)   # 15s, 30s — brief; a blocked cloud IP won't clear anyway
                print(f"  … rate-limited on {label}, waiting {wait}s")
                time.sleep(wait); continue
            print(f"  ! Reddit error {e.code} for {label}")
            return None                     # None = the source failed (for run stats)
        except ET.ParseError as e:
            print(f"  ! RSS parse error for {label}: {e}")
            return None
    if root is None:
        return None

    posts = []
    for entry in root.findall(ATOM + "entry"):
        title_el = entry.find(ATOM + "title")
        updated_el = entry.find(ATOM + "updated")
        content_el = entry.find(ATOM + "content")
        cat_el = entry.find(ATOM + "category")
        # permalink = the alternate link
        permalink = ""
        for lk in entry.findall(ATOM + "link"):
            if lk.get("rel", "alternate") == "alternate":
                permalink = lk.get("href", ""); break
        if not permalink:
            continue
        content_html = content_el.text or "" if content_el is not None else ""
        # external submission link — prefer an embeddable clip over an article link
        external = _pick_external(content_html)
        body = html.unescape(_TAGS.sub(" ", content_html))
        body = re.sub(r"\s+", " ", body).strip()[:1500]
        posts.append({
            "title": html.unescape((title_el.text or "").strip()),
            "selftext": body,
            "subreddit": cat_el.get("term", "") if cat_el is not None else "",
            "source_label": label,          # e.g. "r/aidangers"
            "provenance": "Reddit",
            "permalink": permalink,
            "external_url": external,
            "created": (updated_el.text or "")[:10] if updated_el is not None else "",
            "over_18": False,   # not exposed via RSS; Groq flags sensitivity instead
        })
    return posts

# --- Generic feed ingestion (non-Reddit: Google Alerts, news, arXiv, incident DBs) ----

def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()

def _child(el, *names):
    """First direct child whose local tag matches any of `names`, by name priority.
    NB: uses `is not None`, never truthiness — a childless element (e.g. <pubDate>)
    is falsy in ElementTree, so `_child(a) or _child(b)` would silently drop it."""
    found = {}
    for c in el:
        lt = _local(c.tag)
        if lt in names and lt not in found:
            found[lt] = c
    for n in names:
        if n in found:
            return found[n]
    return None

def _clean(text, limit):
    if not text:
        return ""
    return re.sub(r"\s+", " ", _TAGS.sub(" ", html.unescape(text))).strip()[:limit]

def _parse_date(s):
    if not s:
        return ""
    s = s.strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)          # ISO 8601 (Atom, dc:date)
    if m:
        return m.group(1)
    try:                                             # RFC 822 (RSS pubDate) — handles
        dt = email.utils.parsedate_to_datetime(s)    # GMT / numeric offsets / EDT/PST/…
        if dt is not None:
            return dt.strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    return ""

def _unwrap_google(url):
    if "google.com/url" in url:
        m = re.search(r"[?&]url=([^&]+)", url)
        if m:
            return urllib.parse.unquote(m.group(1))
    return url

def _provenance(feed_url):
    u = feed_url.lower()
    if "google.com/alerts" in u:
        return "Google Alert"
    if "incidentdatabase.ai" in u:
        return "Incident DB"
    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube"
    return "News/RSS"

# YouTube channel Atom feeds carry the body inside <media:group><media:description>,
# not <summary>/<content>, so pull it out explicitly or the judge only sees the title.
MRSS = "{http://search.yahoo.com/mrss/}"

def _media_description(entry):
    grp = entry.find(MRSS + "group")
    if grp is not None:
        desc = grp.find(MRSS + "description")
        if desc is not None and desc.text:
            return desc.text
    d = entry.find(MRSS + "description")
    return d.text if (d is not None and d.text) else ""

_IMG_SRC = re.compile(r'<img\b[^>]*?\bsrc="([^"]+)"', re.I)
_IMG_EXT = re.compile(r"\.(?:jpe?g|png|webp|gif|avif)(?:\?|$)", re.I)

def _feed_image(item, body_html):
    """An image URL the FEED itself advertises for this item — used as a last-resort card
    image when the destination page blocks us / has no og:image. Checks, in order:
    media:content (image), media:thumbnail, <enclosure type=image>, then the first <img>
    in the item's own description/content HTML. Returns "" if none. (YouTube feeds carry a
    media:thumbnail too, but those items already embed as video, so this only matters for
    news/alert articles.)"""
    for tag in (MRSS + "content", MRSS + "thumbnail"):
        for el in item.iter(tag):
            url = (el.get("url") or "").strip()
            medium = (el.get("medium") or el.get("type") or "").lower()
            if url and (tag.endswith("thumbnail") or "image" in medium
                        or _IMG_EXT.search(url)):
                return url
    for el in item.iter():
        if _local(el.tag) == "enclosure" and "image" in (el.get("type") or "").lower():
            u = (el.get("url") or "").strip()
            if u:
                return u
    m = _IMG_SRC.search(body_html or "")
    if m:
        return html.unescape(m.group(1).strip())
    return ""

def fetch_feed(url, label):
    """Generic RSS/Atom/RDF parser for non-Reddit sources → same post dict shape."""
    try:
        xml = http_text(url, headers={"User-Agent": BROWSER_UA})
        root = ET.fromstring(xml)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"  ! Feed error for {label}: {getattr(e, 'code', e)}")
        return None
    except ET.ParseError as e:
        print(f"  ! Feed parse error for {label}: {e}")
        return None

    prov = _provenance(url)
    posts = []
    if _local(root.tag) == "feed":                   # Atom
        for e in [x for x in root if _local(x.tag) == "entry"]:
            t = _child(e, "title")
            title = _clean(t.text if t is not None else "", 250)
            link = ""
            for c in e:
                if _local(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                    link = c.get("href", "") or (c.text or "")
                    if link:
                        break
            body_el = _child(e, "summary", "content")
            body_text = body_el.text if body_el is not None else ""
            if not body_text:                        # YouTube: body lives in media:description
                body_text = _media_description(e)
            d = _child(e, "published", "updated")
            title, link = title, _unwrap_google(link)
            if title and link:
                posts.append(_feed_post(title, _clean(body_text, 1500),
                                        link, _parse_date(d.text if d is not None else ""), label, prov,
                                        _feed_image(e, body_text)))
    else:                                            # RSS 2.0 / RDF
        for it in root.iter():
            if _local(it.tag) != "item":
                continue
            t, l = _child(it, "title"), _child(it, "link")
            d, pd = _child(it, "description"), _child(it, "pubdate", "date")
            title = _clean(t.text if t is not None else "", 250)
            link = _unwrap_google((l.text or "").strip() if l is not None else "")
            raw_desc = d.text if d is not None else ""
            if title and link:
                posts.append(_feed_post(title, _clean(raw_desc, 1500),
                                        link, _parse_date(pd.text if pd is not None else ""), label, prov,
                                        _feed_image(it, raw_desc)))
    return posts

def _feed_post(title, body, link, date, label, prov, feed_image=""):
    return {"title": title, "selftext": body, "subreddit": "", "source_label": label,
            "provenance": prov, "permalink": link, "external_url": link,
            "created": date, "over_18": False, "feed_image": feed_image}

# --- Lane 2: YouTube Data API search ----------------------------------------
# Open discovery across the themes (rows of Type "YouTube search" in the Sources
# table; the row Name IS the query, like Keyword rows). Two passes per query — short
# (<4m, incl Shorts) + medium (4–20m segments) — to get the short-vs-longform mix.
# Provenance is "YouTube search" (NOT "YouTube"), so these BYPASS the AI pre-gate: the
# query is already AI-targeted, unlike Lane 1's whole-channel feeds. Judge still applies.
def fetch_youtube_search(query):
    """Lane 2 search for one theme query → post dicts (same shape). None on failure."""
    if not YOUTUBE_KEY:
        return None                       # no key yet — treated as a skipped source upstream
    posts, seen = [], set()
    # Only videos published within the recency window (2d daily / 7d weekly) — RFC 3339.
    published_after = (datetime.datetime.utcnow()
                       - datetime.timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT00:00:00Z")
    for dur in YT_SEARCH_DURATIONS:
        params = urllib.parse.urlencode({
            "key": YOUTUBE_KEY, "part": "snippet", "q": query, "type": "video",
            "videoDuration": dur, "maxResults": YT_PER_QUERY, "order": "relevance",
            "relevanceLanguage": "en", "safeSearch": "moderate", "publishedAfter": published_after,
        })
        try:
            data = http_json("https://www.googleapis.com/youtube/v3/search?" + params,
                             headers={"User-Agent": UA})
        except urllib.error.HTTPError as e:
            # 403 = quota exhausted (100 units/search); a partial result is still useful.
            print(f"  ! YouTube API error {e.code} for {query!r} ({dur} pass)")
            return posts or None
        except Exception as e:
            print(f"  ! YouTube API error for {query!r}: {e}")
            return posts or None
        for it in data.get("items", []):
            vid = (it.get("id") or {}).get("videoId")
            sn = it.get("snippet") or {}
            if not vid or vid in seen:
                continue
            seen.add(vid)
            watch = f"https://www.youtube.com/watch?v={vid}"
            posts.append({
                "title": html.unescape(sn.get("title", "")),
                "selftext": html.unescape(sn.get("description", "")),
                "subreddit": "", "source_label": query, "provenance": "YouTube search",
                "permalink": watch, "external_url": watch,
                "created": (sn.get("publishedAt", "") or "")[:10], "over_18": False,
            })
    return posts

def existing_source_urls():
    urls, offset = set(), None
    while True:
        u = f"https://api.airtable.com/v0/{BASE}/{TABLE}?pageSize=100&fields%5B%5D=Source%20URL"
        if offset:
            u += f"&offset={offset}"
        data = http_json(u, headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"})
        for r in data.get("records", []):
            su = r.get("fields", {}).get("Source URL")
            if su:
                urls.add(su)
        offset = data.get("offset")
        if not offset:
            break
    return urls

# --- AI-relevance pre-gate (YouTube only) -----------------------------------
# YouTube Lane 1 pulls each trusted channel's WHOLE upload feed — mostly non-AI news
# (earthquakes, sport, elections). This keyword gate keeps those out of the Groq judge
# budget. Runs ONLY on YouTube-provenance posts; every other source (Reddit, Google
# Alerts, incident DBs, news RSS) is already AI-targeted and bypasses the gate. Recall-
# biased on purpose — the judge does final precision; the gate only strips zero-signal posts.
#
# Terms live in the Airtable "Gate keywords" table so the owner can SEE and EDIT them.
# SAFETY: if that table is empty or unreadable, the gate DISABLES itself (all posts pass) —
# never the reverse. So a bad edit only wastes some judge budget; it can never silently
# drop real stories. Match types: Acronym (\b..\b, case-sensitive, e.g. AI/GPT), Word
# (\b..\b, case-insensitive, e.g. Sora), Phrase (substring, case-insensitive — default).
GATE_TABLE = "tblvUIs01rOGsMpnC"

def load_gate_matchers():
    """Build (acronym_re, terms_re) from Active 'Gate keywords' rows, or None to disable."""
    try:
        url = (f"https://api.airtable.com/v0/{BASE}/{GATE_TABLE}?pageSize=100"
               f"&filterByFormula={urllib.parse.quote('{Active}=1')}")
        data = http_json(url, headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"})
    except Exception as e:
        print(f"  ! Gate keywords unreadable ({e}) — gate DISABLED (all YouTube posts pass).")
        return None
    acro, ci = [], []
    for r in data.get("records", []):
        f = r.get("fields", {})
        term = (f.get("Term") or "").strip()
        if not term:
            continue
        match = f.get("Match", "Phrase")
        if match == "Acronym":
            acro.append(re.escape(term))                    # \b..\b, case-SENSITIVE
        elif match == "Word":
            ci.append(r"\b" + re.escape(term) + r"\b")       # \b..\b, case-insensitive
        else:
            ci.append(re.escape(term))                       # substring, case-insensitive
    if not acro and not ci:
        print("  ! Gate keywords table has no active terms — gate DISABLED (all YouTube posts pass).")
        return None
    acro_re = re.compile(r"\b(?:" + "|".join(acro) + r")\b") if acro else None
    ci_re = re.compile("|".join(ci), re.I) if ci else None
    print(f"  Gate loaded: {len(acro)} acronym + {len(ci)} phrase/word terms from Airtable.")
    return (acro_re, ci_re)

def ai_relevant(post, matchers):
    """True if the post shows any AI signal in its title (or the gate is disabled).
    TITLE ONLY for YouTube: the gate only ever runs on `YouTube` whole-channel feeds, whose
    `selftext` is the full video DESCRIPTION — channel boilerplate plus multi-topic roundup
    listings. Matching the body there let a single incidental AI mention keep an entirely
    non-AI video (e.g. a Bloomberg Iran episode kept because its description listed an OpenAI
    segment). The title names the video's actual subject, so gate on that alone. Other
    provenances (should any ever be gated) still get title+body."""
    if matchers is None:
        return True
    acro_re, ci_re = matchers
    text = (post.get("title", "") if post.get("provenance") == "YouTube"
            else f"{post.get('title', '')} {post.get('selftext', '')}")
    return bool((acro_re and acro_re.search(text)) or (ci_re and ci_re.search(text)))

def detect_format_and_media(post):
    """Format + embeddable Media URL from the post's external link (if any). Only a
    SPECIFIC clip/post permalink embeds (see is_social_clip); platform browse/landing
    pages (tiktok.com/discover/…, /tag/…, a bare IG profile) fall through to a plain
    article card so they never render as a broken embed."""
    url = post["external_url"]
    low = url.lower()
    if is_social_clip(url):
        if "youtube.com" in low or "youtu.be" in low:
            return "Video", url
        if "tiktok.com" in low:
            return "Video", url
        if "instagram.com" in low:
            return "Image", url
        if "twitter.com" in low or "x.com" in low:
            return "Text", url          # renders as a tweet embed, not video/image
    if any(low.endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp", ".gif")) or "i.redd.it" in low:
        return "Image", url
    return "Text", ""     # self-post, article, or non-embeddable social page -> text card

def article_url(p):
    """The outbound article/page link to open when enriching (og tags). For a Reddit
    link-post this is the linked article; for a feed item it equals the permalink."""
    return (p.get("external_url") or p.get("permalink") or "").strip()

def story_source_url(p):
    """The public 'View source' link. For a Reddit LINK post to an article (not a clip),
    link straight to the ORIGINAL ARTICLE rather than the Reddit thread — the story is the
    article, not the discussion. Self-posts and clip posts keep the Reddit permalink (clips
    embed via Media URL — Option A). Non-Reddit feeds already have permalink == the article."""
    ext = (p.get("external_url") or "").strip()
    if p.get("provenance") == "Reddit" and ext and not is_social_clip(ext):
        return ext
    return p.get("permalink", "")

def dedup_keys(p):
    """URLs that identify this post for de-duplication vs Airtable. We check BOTH the Reddit
    permalink AND the article URL we actually store, so (a) new article-linked rows dedupe on
    the article and (b) rows written under the old permalink-only scheme are still recognised
    (no one-off re-harvest when this behaviour changes)."""
    return {u for u in (p.get("permalink", ""), story_source_url(p), article_url(p)) if u}

# --- Title cleanup: drop a trailing " | Outlet" / " - Outlet" source suffix -------
# Feed/page <title>s usually append the publication ("… | ABS-CBN News", "… - Barta24").
# Strip it CONSERVATIVELY — only when the tail looks like a publication name AND a
# substantial head remains — so a real title with a mid-sentence dash ("… - what it means")
# is never truncated. A cleaner title also makes cross-outlet REPRINTS collide in dedup.
_TITLE_SEP = re.compile(r"\s+[|–—-]\s+")   # ' | ', ' - ', ' – ', ' — '
_OUTLET_WORDS = {"news","business","times","post","update","media","tv","radio","classroom",
                 "wire","journal","herald","tribune","daily","report","magazine","network",
                 "press","today","online","digital","gazette","observer","chronicle","standard",
                 "review","weekly","bulletin","insider","dispatch","globe","mail","sun","star"}

def _looks_like_outlet(tail):
    tail = tail.strip()
    if not tail or tail[-1] in ".!?:,":
        return False                         # ends like a sentence fragment, not a masthead
    toks = tail.split()
    if not toks or len(toks) > 5:
        return False
    if re.search(r"\.(com|news|org|net|io|co)\b", tail.lower()):
        return True                          # 'Polygon.com', 'UA.NEWS'
    if any(t.strip(".").lower() in _OUTLET_WORDS for t in toks):
        return True                          # contains a masthead word
    if len(toks) == 1:
        return True                          # single token: 'Barta24', 'flyingpenguin', 'JDSupra'
    alpha = [t for t in toks if any(c.isalpha() for c in t)]
    return bool(alpha) and all(t[0].isupper() for t in alpha)   # 'The Washington Post'

def clean_title(title):
    """Peel up to 3 trailing ' | Outlet' / ' - Outlet' segments off a title, but only while
    the tail looks like a publication and the head stays substantial (>=3 words, >=15 chars)."""
    t = (title or "").strip()
    for _ in range(3):
        last = None
        for m in _TITLE_SEP.finditer(t):
            last = m                          # rightmost separator
        if not last:
            break
        head, tail = t[:last.start()].strip(), t[last.end():].strip()
        if len(head.split()) >= 3 and len(head) >= 15 and _looks_like_outlet(tail):
            t = head
        else:
            break
    return t

def norm_title(title):
    """Lowercased, punctuation-flattened, outlet-stripped title — the cross-run dedup key."""
    return re.sub(r"\W+", " ", clean_title(title).lower()).strip()

# --- Cross-run near-duplicate detection (no model needed) ---------------------------
# Exact-URL dedup already drops the SAME article. This catches the same EVENT reprinted at
# a DIFFERENT url (wire copy run by two outlets). It is deliberately conservative — it needs
# BOTH a near-identical title AND a near-identical summary — so a DEVELOPING story (same topic,
# new facts → different summary) is KEPT, only true reprints are dropped. Lexical, not semantic.
DEDUP_TITLE_RATIO = 0.90
DEDUP_SUMMARY_RATIO = 0.72
DEDUP_WINDOW_DAYS = 14

def existing_signatures():
    """(norm_title, summary_lower) for recent, still-standing rows — the reprint corpus."""
    cut = (datetime.date.today() - datetime.timedelta(days=DEDUP_WINDOW_DAYS)).isoformat()
    formula = f"AND(IS_AFTER({{Date Harvested}}, '{cut}'), {{Status}}!='Rejected')"
    sigs, offset = [], None
    while True:
        u = (f"https://api.airtable.com/v0/{BASE}/{TABLE}?pageSize=100"
             f"&fields%5B%5D=Title&fields%5B%5D=Source%20headline&fields%5B%5D=Summary"
             f"&filterByFormula={urllib.parse.quote(formula)}")
        if offset:
            u += f"&offset={offset}"
        data = http_json(u, headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"})
        for r in data.get("records", []):
            f = r.get("fields", {})
            nt = norm_title(f.get("Source headline") or f.get("Title", ""))   # Title is now our own
            if nt:
                sigs.append((nt, (f.get("Summary", "") or "").lower().strip()))
        offset = data.get("offset")
        if not offset:
            break
    return sigs

def is_reprint(title, summary, sigs):
    """True if (title, summary) closely matches an existing signature — a reprint, not a
    develop­ment. Requires BOTH title and summary to be near-identical (keep-both when unsure)."""
    nt, sm = norm_title(title), (summary or "").lower().strip()
    for ent, esum in sigs:
        if difflib.SequenceMatcher(None, nt, ent).ratio() >= DEDUP_TITLE_RATIO and \
           difflib.SequenceMatcher(None, sm, esum).ratio() >= DEDUP_SUMMARY_RATIO:
            return True, ent
    return False, None

# Enrichment: open a KEPT story's page once to pull a heading image (og:image) or an
# embedded YouTube video, so plain article cards aren't bare. Only kept stories are
# fetched (a handful per run), and any failure leaves the card exactly as it was.
_META_TAG = re.compile(r"<meta\b[^>]*>", re.I)
_META_ATTR = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"' r"|([\w:-]+)\s*=\s*'([^']*)'", re.I)
_YT_EMBED = re.compile(r"youtube(?:-nocookie)?\.com/embed/([A-Za-z0-9_-]{11})", re.I)
_YT_ANY = re.compile(r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})", re.I)
_CSPAN = re.compile(r"c-span\.org/video/standalone/\?c(\d+)", re.I)

def _og_tags(html_text):
    """Map of og:*/twitter:* meta property -> content (first wins), attribute-order agnostic."""
    props = {}
    for tag in _META_TAG.finditer(html_text):
        attrs = {}
        for m in _META_ATTR.finditer(tag.group(0)):
            k = (m.group(1) or m.group(3) or "").lower()
            v = m.group(2) if m.group(2) is not None else m.group(4)
            if k:
                attrs[k] = html.unescape(v or "")
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and "content" in attrs and key not in props:
            props[key] = attrs["content"]
    return props

def enrich_media(p, fmt, media):
    """If the story is a plain Text/article card, open the page and pull an embedded
    YouTube video (preferred) or the article's heading image (og:image). Best-effort:
    non-HTML pages, blocks, timeouts or missing tags all leave the card unchanged.
    NB: Facebook-hosted video isn't freely embeddable, so a FB-only clip falls back to
    the og:image here rather than a video embed."""
    if media or fmt != "Text":
        return fmt, media                      # already has an embed/clip
    # Reddit self-posts have no outbound article — don't fetch the thread itself (its og:image
    # is just Reddit's logo). Only enrich Reddit LINK posts and non-Reddit feed articles.
    if p.get("provenance") == "Reddit" and not (p.get("external_url") or "").strip():
        return fmt, media
    url = article_url(p)
    if not url.lower().startswith("http"):
        return _feed_fallback(p, fmt, media)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            if "html" not in (r.headers.get("Content-Type") or "").lower():
                return _feed_fallback(p, fmt, media)
            body = r.read(600_000).decode("utf-8", "replace")   # og tags live in <head>; cap the read
    except Exception as e:
        print(f"    · enrich: skipped {url[:60]} ({getattr(e, 'code', e)})")
        return _feed_fallback(p, fmt, media)
    og = _og_tags(body)
    # 1) embedded YouTube video — playable on-site, so prefer it over a still image.
    for v in (og.get("og:video:url"), og.get("og:video:secure_url"), og.get("og:video")):
        m = _YT_ANY.search(v or "")
        if m:
            print(f"    · enrich: found embedded video for {p['title'][:40]}")
            return "Video", f"https://www.youtube.com/watch?v={m.group(1)}"
    m = _YT_EMBED.search(body)                  # a real <iframe> embed in the article body
    if m:
        print(f"    · enrich: found embedded video for {p['title'][:40]}")
        return "Video", f"https://www.youtube.com/watch?v={m.group(1)}"
    m = _CSPAN.search(body)                      # C-SPAN standalone player (its embed form)
    if m:
        print(f"    · enrich: found C-SPAN video for {p['title'][:40]}")
        return "Video", f"https://www.c-span.org/video/standalone/?c{m.group(1)}"
    # 2) heading image (og:image / twitter:image).
    img = (og.get("og:image") or og.get("og:image:url") or og.get("og:image:secure_url")
           or og.get("twitter:image") or og.get("twitter:image:src"))
    if img:
        img = urllib.parse.urljoin(url, img.strip())
        if img.lower().startswith("http"):
            print(f"    · enrich: added heading image for {p['title'][:40]}")
            return "Image", img
    return _feed_fallback(p, fmt, media)

def _feed_fallback(p, fmt, media):
    """Last resort when the destination page gives us nothing: use the image the FEED
    itself advertised for this item (media:content/thumbnail/enclosure/first <img>). Lets
    a card carry a picture even when the article is bot-blocked or has no og:image."""
    if media or fmt != "Text":
        return fmt, media
    fi = (p.get("feed_image") or "").strip()
    if fi.lower().startswith("http"):
        print(f"    · enrich: used feed image for {p['title'][:40]}")
        return "Image", fi
    return fmt, media

def _strip_reasoning(text):
    """Belt-and-suspenders: some Groq models are reasoning models that can wrap their
    chain-of-thought in <think>…</think> before the real answer. Never let that scratchpad
    reach a card — keep only what follows the final </think>."""
    if text and "</think>" in text:
        text = text.rsplit("</think>", 1)[-1]
    return text

def groq_chat(model, prompt, temperature=0.2, json_mode=True, reasoning_effort=None):
    """One Groq chat-completion call, shared by all three stages.
    Returns (content_str_or_None, total_tokens, status) where status is:
      'ok'            — got a reply
      'rate_limited'  — 429 that survived our retries (transient / bucket exhausted).
                        Callers should back off + CONTINUE, not treat as a hard failure.
      'error'         — a genuine failure (other HTTP code, parse, network)
    Splitting 429 from real errors is what lets the judge loop skip a rate-limited
    item without tripping the consecutive-error abort.
    reasoning_effort='none' disables a reasoning model's chain-of-thought (used for the
    copy stage — qwen answers in ~87 tokens instead of ~950 when thinking is off)."""
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    body = json.dumps(payload).encode()
    for attempt in range(5):
        try:
            resp = http_json("https://api.groq.com/openai/v1/chat/completions",
                             headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                                      "Content-Type": "application/json",
                                      "User-Agent": BROWSER_UA},
                             data=body, method="POST")
            tokens = int((resp.get("usage") or {}).get("total_tokens") or 0)
            return _strip_reasoning(resp["choices"][0]["message"]["content"]), tokens, "ok"
        except urllib.error.HTTPError as e:
            if e.code == 429:
                ra = e.headers.get("retry-after")
                if attempt < 4:
                    wait = min((int(ra) + 2) if (ra and str(ra).isdigit()) else 15, 30)
                    print(f"  … {model} rate-limited, waiting {wait}s")
                    time.sleep(wait); continue
                return None, 0, "rate_limited"     # persistent 429 — let caller decide
            print(f"  ! {model} error {e.code}")
            return None, 0, "error"
        except Exception as e:
            print(f"  ! {model} call/parse error: {e}")
            return None, 0, "error"
    return None, 0, "rate_limited"

_OUTLETS = {}

def outlet(url):
    """Who published the item: its web address, or the channel for a YouTube video. Our feed labels are
    search names ("AI Incident Database", "[Bias] credit / loan bias"), not publishers, so without this the
    judge can't tell the Guardian from a blog and rejects good stories as "no credible source"."""
    if url in _OUTLETS:
        return _OUTLETS[url]
    host = urllib.parse.urlparse(url or "").netloc.lower().removeprefix("www.").removeprefix("m.")
    name = host
    if host.endswith(("youtube.com", "youtu.be")):
        try:
            channel = http_json("https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url)).get("author_name")
            name = f"YouTube channel {channel}" if channel else host
        except Exception:
            pass
    _OUTLETS[url] = name
    return name

def _origin_line(post):
    # Source-agnostic origin — the harvester feeds Reddit, news RSS, Google Alerts, YouTube
    # and incident-DB entries, so never tell the judge everything is a "Reddit post".
    if post.get("subreddit"):
        return f"Source: Reddit — r/{post['subreddit']}"
    prov, lbl = post.get("provenance", ""), post.get("source_label", "")
    line = "Found via: " + (f"{prov} — {lbl}" if (prov and lbl) else (prov or lbl or "unknown"))
    pub = outlet(post.get("external_url", ""))
    return line + (f" | Published by: {pub}" if pub else "")

# --- Stage B: the editorial judge (gpt-oss-120b) ----------------------------
# The judge's instructions are BUILT FROM rulebook.md (purpose, themes, story criteria, examples,
# sensitivity), so a rule changed there changes the judge. One prompt for every call: a single item
# is just a batch of one. The judge scores; the code applies the bar (judge_verdict), so the pass
# numbers live in the rulebook's settings and the model can't move them.
SCORE_KEYS = ("credibility", "consequence", "significance", "audience", "theme_fit")
SCORE_FIELDS = {"credibility": "Score: credibility", "consequence": "Score: consequence",
                "significance": "Score: significance", "audience": "Score: audience",
                "theme_fit": "Score: theme fit"}
# "70, plus credibility at least 60, and 70 for unproven claims" -> overall, credibility floor,
# credibility needed for an unproven claim (rulebook section 8, "Pass score").
PASS_OVERALL, PASS_CREDIBILITY, PASS_UNPROVEN = (
    int(n) for n in re.findall(r"\d+", rulebook.setting("Pass score"))[:3])
JUDGE_READ = rulebook.setting_int("Judge reading length")   # chars of each item the judge reads
HOLD_REASONS = ("Risky picture or video", "Child could be identified")

def _judge_rules():
    """The judge's rules, from the rulebook. Tables become one line per row to keep the prompt short."""
    themes = "\n".join(f"- {t['name']}. In: {t['in']}. Out: {t['out']}" for t in rulebook.themes())
    two = rulebook.plain(rulebook.section(2).split("**When a story fits two themes:**", 1)[1].split("|", 1)[0]).strip()
    scores = "\n".join(f'- "{k}" = {row[0]}: {row[1]} About 90: {row[2]}. About 50: {row[3]}. Below 30: {row[4]}.'
                       for k, row in zip(SCORE_KEYS, rulebook.table(3, "Score")))
    examples = "\n".join(f"- {r[0]} → {r[1]}{', ' + r[2] if r[2] not in ('—', '') else ''}. {r[3]}"
                         for r in rulebook.table(5, "Story"))
    sens = rulebook.plain(rulebook.section(6))
    return f"""ABOUT THE SITE ("Is AI Good?")
{rulebook.plain(rulebook.section(1))}

THEMES. Reader: a thoughtful, experienced professional; topics are never filtered by audience.
{themes}
When a story fits two themes:
{two}

INSTANT REJECTS (any one is enough; give its number in "reject")
{rulebook.plain(rulebook.part(3, "Instant rejects"))}

SCORES (each 0 to 100)
{scores}
"overall" (0 to 100): how strongly this belongs in the gallery, all things considered.
"unproven": true when the harm rests on a claim not yet proven (a lawsuit filed, a police referral, an
accusation). A ruling, settlement, charge or conviction counts as proven.
"harm_documented": true when the harm itself is shown as fact (e.g. tenants did pay more), not only claimed.

WORKED EXAMPLES (the owner's own calls)
{examples}

SENSITIVITY
{sens}"""

JUDGE_RULES = _judge_rules()

def judge_prompt(batch):
    """The one judge prompt: rules from the rulebook + the numbered items + the reply format."""
    lines, any_clip = [], False
    for i, p in enumerate(batch):
        clip = is_social_clip(p.get("external_url", "") or "")
        any_clip = any_clip or clip
        mark = " [social-video clip]" if clip else ""
        body = (p.get("selftext") or "")[:JUDGE_READ] or "(no body text)"
        lines.append(f'[{i}]{mark} {_origin_line(p)} | Title: {p["title"]} | Body: {body}')
    clip_hint = ("\nItems marked [social-video clip] are a short caption over a video you cannot see "
                 "(Instagram / TikTok / YouTube). Judge the topic; don't reject only because the caption is "
                 "short.\n" if any_clip else "")
    return f"""You are the editorial judge for "Is AI Good?" (isaigood.org), a gallery of real, sourced stories of
harm from AI. Judge EACH numbered item on its own against the rules below. An item may be a news article, a
Google Alert hit, a Reddit post, a YouTube video or an incident-database entry. "Found via" is our own feed or
search name, not the publisher; "Published by" is the outlet (web address or YouTube channel): judge source
credibility on that. Return STRICT JSON only.

{JUDGE_RULES}
{clip_hint}
Return one object per item, matching its [i] number:
{{"results": [
  {{"i": <number>,
    "reject": <the instant-reject number that applies, or null>,
    "theme": one of {THEMES} or "none",
    "scores": {{"credibility": 0-100, "consequence": 0-100, "significance": 0-100, "audience": 0-100, "theme_fit": 0-100}},
    "overall": 0-100,
    "unproven": true/false,
    "harm_documented": true/false,
    "sensitive": true/false,
    "hold": "{HOLD_REASONS[0]}" | "{HOLD_REASONS[1]}" | null,
    "reason": "one short line: why it is kept, weak or rejected"}}
]}}
Judge EVERY item; return exactly one object per item. Pick the theme and fill in every score even when you
reject an item: a story rejected only for its source can then be looked for elsewhere.

ITEMS:
{chr(10).join(lines)}"""

def _score(t, key):
    try:
        return int(float((t.get("scores") or {}).get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0

def judge_verdict(t):
    """Apply the rulebook's bar (section 3) to a judge reply: "Keep", "Weak" or "Reject"."""
    if not t:
        return "Reject"
    if t.get("reject") not in (None, "", 0, "null", "none"):
        return "Reject"
    if t.get("theme") not in THEMES:
        return "Weak"
    try:
        overall = int(float(t.get("overall", 0) or 0))
    except (TypeError, ValueError):
        overall = 0
    cred = _score(t, "credibility")
    if overall < PASS_OVERALL or cred < PASS_CREDIBILITY:
        return "Weak"
    if t.get("unproven") and not (cred >= PASS_UNPROVEN and t.get("harm_documented")):
        return "Weak"
    return "Keep"

def judge_passes(t):
    return judge_verdict(t) == "Keep"

def overall_score(t):
    try:
        return int(float((t or {}).get("overall", 0) or 0))
    except (TypeError, ValueError):
        return 0

def judge_fields(t):
    """The Airtable columns a judge reply fills (scores, verdict, reason, rulebook version)."""
    reject = t.get("reject")
    reason = (t.get("reason") or "").strip()
    if reject not in (None, "", 0, "null", "none"):
        reason = f"Reject {reject}. {reason}"
    fields = {SCORE_FIELDS[k]: _score(t, k) for k in SCORE_KEYS}
    fields.update({"Overall score": overall_score(t), "Judge verdict": judge_verdict(t),
                   "Judge reason": reason[:900], "Rulebook version": int(rulebook.version())})
    return fields

def groq_judge_batch(batch):
    """Stage B — judge up to JUDGE_BATCH items in ONE call sharing one copy of the rules.
    Returns (verdicts, tokens, status): `verdicts` is a list aligned to `batch`, each a reply dict
    or None (an item the model omitted / unparseable); status is 'ok' | 'rate_limited' | 'error'.
    Callers single-re-judge any None and fall back to single items on a whole-batch failure, so
    batching can never silently lose an item."""
    content, tokens, status = groq_chat(JUDGE_MODEL, judge_prompt(batch))
    if content is None:
        return [None] * len(batch), tokens, status   # rate_limited or error
    try:
        results = json.loads(content).get("results", [])
        by_i = {}
        for o in results:
            if isinstance(o, dict) and "i" in o:
                try:
                    by_i[int(o["i"])] = o
                except (ValueError, TypeError):
                    pass
        return [by_i.get(k) for k in range(len(batch))], tokens, "ok"
    except Exception as e:
        print(f"  ! judge JSON parse error: {e}")
        return [None] * len(batch), tokens, "error"

def groq_judge(post):
    """Stage B for one item: the same prompt as a batch of one. Returns (reply_or_None, tokens, status)."""
    verdicts, tokens, status = groq_judge_batch([post])
    if status == "ok" and verdicts[0] is None:
        status = "error"
    return verdicts[0], tokens, status

# --- Stage A: cheap recall-biased pre-triage (gpt-oss-20b, batched) ----------
# Its rules are the rulebook's "Triage rules" (section 9): drop only the obvious, when unsure keep.
TRIAGE_RULES = rulebook.plain(rulebook.part(9, "Triage rules"))

def triage_prompt(batch):
    items = "\n".join(
        f'{j+1}. {p["title"][:150]} — {(p.get("selftext") or "")[:TRIAGE_SNIPPET]}'
        for j, p in enumerate(batch))
    return ('You are a fast pre-filter for "Is AI Good?", a gallery of real, sourced stories of harm '
            "from AI. For each numbered item, decide KEEP or DROP, following these rules:\n\n"
            f"{TRIAGE_RULES}\n\n"
            'Return JSON {"results":[{"n":1,"v":"KEEP"},...]} covering every item.\n\n' + items)

def pretriage(posts, deadline):
    """Drop OBVIOUS junk before the expensive judge, in batches on the 20b bucket.
    Recall-biased: an item is dropped only on an explicit DROP; when unsure, KEEP. FAIL-OPEN — any
    batch that errors, times out, or returns malformed output passes through untouched, so a broken
    pre-filter can only waste judge budget, never silently lose a real story. Posts beyond
    MAX_TRIAGE skip pre-triage entirely. Returns (survivors, tokens, dropped_posts)."""
    if not posts:
        return posts, 0, []
    pool, rest = posts[:MAX_TRIAGE], posts[MAX_TRIAGE:]
    survivors, dropped, batches, spent = [], [], 0, 0
    for i in range(0, len(pool), TRIAGE_BATCH):
        if time.time() > deadline:                 # out of time → keep everything left (fail-open)
            survivors.extend(pool[i:])
            break
        batch = pool[i:i + TRIAGE_BATCH]
        content, tok, _status = groq_chat(TRIAGE_MODEL, triage_prompt(batch))
        spent += tok
        batches += 1
        keep_idx = None
        if content:
            try:
                data = json.loads(content)
                verds = {int(r["n"]): str(r.get("v", "KEEP")).upper()
                         for r in data.get("results", []) if "n" in r}
                keep_idx = {n for n in range(1, len(batch) + 1) if verds.get(n, "KEEP") != "DROP"}
            except Exception:
                keep_idx = None
        if keep_idx is None:                        # fail-open: keep the whole batch
            survivors.extend(batch)
        else:
            for j, p in enumerate(batch):
                (survivors if (j + 1) in keep_idx else dropped).append(p)
        time.sleep(TRIAGE_SLEEP)
    survivors.extend(rest)
    print(f"  Stage-A pre-triage ({TRIAGE_MODEL}): kept {len(survivors)}/{len(posts)} "
          f"({len(dropped)} obvious-junk dropped across {batches} batches"
          f"{f'; {len(rest)} past MAX_TRIAGE passed through' if rest else ''}).")
    return survivors, spent, dropped

TRIAGE_SAMPLE = 40   # dropped items saved to the Runs row each sweep, so triage can be checked (step 6)

def triage_sample(dropped):
    """A random sample of what triage dropped, one 'title — source' line each."""
    pick = random.sample(dropped, min(TRIAGE_SAMPLE, len(dropped)))
    return "\n".join(f"{p['title'][:140]} — {p.get('source_label') or '?'}" for p in pick)[:95000]

# --- Stage C: card copy for keepers only (qwen3.8-27b) -----------------------
COPY_MIN_BODY = 200        # below this a feed snippet is too thin to summarise from → fetch the full article first

# When handed a thin body the copy model rightly REFUSES and emits a meta-comment ABOUT the
# source ("The provided source is incomplete…") instead of a summary. That text is not card
# copy — it must never be stored. Shared verbatim with rewrite_summaries.py (single source of
# truth) so the live path and the bulk rewrite catch the exact same refusals.
META_MARKERS = (
    "provided source", "source text", "the source text", "no factual summary",
    "cannot be generated", "can be generated", "no summary can", "does not illustrate",
    "not an ai risk", "not an ai incident", "not an ai story", "website navigation",
    "boilerplate", "this item describes", "this does not", "lacks a specific",
    "no specific incident", "insufficient information", "unable to summarize",
    "impossible to write", "lacks specific", "lacks the necessary", "summary criteria",
)

def looks_like_meta(text):
    """True if the copy model returned a comment about the source rather than a real summary."""
    t = (text or "").lower()
    return any(m in t for m in META_MARKERS)

def _strip_tags(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", fragment))).strip()

def _ld_article_body(raw):
    """The article text many news sites publish for search engines (JSON-LD "articleBody"): the
    cleanest source there is, with no menus or adverts. The longest wins, as a page may also list
    related stories."""
    best = ""
    for block in re.findall(r"(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>", raw):
        try:
            todo = [json.loads(block.strip())]
        except ValueError:
            continue
        while todo:
            node = todo.pop()
            if isinstance(node, list):
                todo.extend(node)
            elif isinstance(node, dict):
                todo.extend(v for v in node.values() if isinstance(v, (list, dict)))
                body = node.get("articleBody")
                if isinstance(body, str) and len(body) > len(best):
                    best = body
    return _strip_tags(best)

def _story_paragraphs(raw):
    """The page's story paragraphs: inside its <article> or <main> when it has one, with menus,
    headers, footers and link lists left out. Site menus use up the judge's fixed reading length
    (and the card copy's) before the story starts."""
    raw = re.sub(r"(?is)<(nav|header|footer|aside|form|menu)\b.*?</\1>", " ", raw)
    blocks = re.findall(r"(?is)<(?:article|main)\b[^>]*>(.*?)</(?:article|main)>", raw)
    scope = max(blocks, key=len) if blocks else raw
    kept = []
    for para in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", scope):
        text = _strip_tags(para)
        linked = sum(len(_strip_tags(a)) for a in re.findall(r"(?is)<a\b[^>]*>(.*?)</a>", para))
        if len(text) >= 40 and linked <= len(text) / 2:     # short lines and link lists aren't the story
            kept.append(text)
    return " ".join(kept)

def _article_text(url):
    """Full article body for a feed/article URL: og:description, then the publisher's own article
    text or else the story paragraphs (never site menus). Gives the judge and Stage C a real body
    when the feed snippet is too thin (the failure that let refusal text reach live cards).
    Best-effort: returns '' on any error so the caller keeps the snippet."""
    try:
        raw = http_text(url, headers={"User-Agent": BROWSER_UA})
    except Exception:
        return ""
    body = _ld_article_body(raw)
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    m = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\']'
                  r'[^>]+content=["\']([^"\']+)', raw, re.I)
    desc = html.unescape(m.group(1)) if m else ""
    if len(body) < 200:
        body = _story_paragraphs(raw)
    if len(body) < 200:     # an unusual page: better some menu text than no story at all
        body = " ".join(_strip_tags(p) for p in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", raw))
    return re.sub(r"\s+", " ", (desc + " " + body).strip())

def copy_prompt(post, body):
    """The Stage-C card-copy prompt, built from the rulebook's "Card copy" rules (section 7). The ONE
    prompt for card copy, shared by the live harvester (write_copy) and the bulk rewrite
    (rewrite_summaries.py), so rewritten cards read like new ones. Asks for our own title, the
    summary, and whether the source headline and the summary are about the same event."""
    return ('Write the card for the item below, for "Is AI Good?", a gallery of real, sourced stories '
            "of harm from AI. Follow these rules exactly:\n\n"
            f"{COPY_RULES}\n\n"
            'Return STRICT JSON only: {"title": "our own title", "summary": "the summary", '
            '"same_event": true or false}. "same_event" is false when the source headline below is '
            "about a different event from what the body (and so the summary) describes.\n"
            # The model doesn't know today's date: without the published date it invents one when it
            # turns "Wednesday" or "last week" into a date (seen in the first judge check).
            "Work out dates only from the published date below and what the body says; if a date "
            "can't be worked out exactly, leave it out rather than guess.\n\n"
            f"{_origin_line(post)}\nPublished: {(post.get('created') or 'unknown')[:10]}\n"
            f"Source headline: {post['title']}\n"
            f"Body: {body[:JUDGE_READ] or '(no body text)'}")

COPY_RULES = rulebook.plain(rulebook.section(7))

def write_copy(post, body=None):
    """Stage C — our own title and the card summary for a KEPT item, on the qwen bucket.
    Returns (title, summary, same_event, tokens). title and summary are "" when the copy can't be
    written (the AI fails or refuses): the card is then HELD, never filled with raw article text
    (rulebook section 7), and the summary is retried next sweep. `body` overrides the post's own
    text (rewrite_summaries.py passes the article it fetched)."""
    body = (post.get("selftext") or "").strip() if body is None else body
    # A thin feed snippet is what made qwen refuse and leak meta-text onto live cards. When the
    # body is too short, fetch the real article body first (best-effort, skipped for social clips
    # whose "body" is the video itself). enrich_media re-opens the page later for media only.
    if len(body) < COPY_MIN_BODY:
        url = article_url(post)
        if url.lower().startswith("http") and not is_social_clip(url):
            full = _article_text(url)
            if len(full) > len(body):
                body = full
    # reasoning_effort='none' → qwen answers directly instead of thinking first (far fewer tokens).
    content, tokens, status = groq_chat(COPY_MODEL, copy_prompt(post, body), reasoning_effort="none")
    if status != "ok" or not content:
        return "", "", True, tokens
    try:
        data = json.loads(content)
    except Exception:
        return "", "", True, tokens
    title = (data.get("title") or "").strip().strip('"').strip()
    summary = (data.get("summary") or "").strip().strip('"').strip()
    # Store the copy only if it's real — never a refusal/meta-comment about the source.
    if not title or not summary or looks_like_meta(summary) or looks_like_meta(title):
        return "", "", True, tokens
    return title, summary, data.get("same_event") is not False, tokens

def _is_clip_item(it):
    """True if a passed item is an embeddable social clip (its external link is an
    IG/TikTok/YouTube/X permalink) — used to reserve it a selection slot (see select_best)."""
    return is_social_clip(it["p"].get("external_url", "") or "")

def select_best(passed):
    """Pick the final keepers from EVERYTHING that passed the judge: the best MAX_PER_THEME per
    theme (highest relevance), then trim to MAX_NEW overall by relevance. This is why we judge
    the whole pool first — so each theme keeps its strongest items, not merely its first few.
    `passed` is a list of {"p","t","rel","theme"}. Returns (selected_list, dropped_count)."""
    by_theme = {}
    for it in passed:
        by_theme.setdefault(it["theme"], []).append(it)
    selected, dropped = [], 0
    for items in by_theme.values():
        items.sort(key=lambda it: it["rel"], reverse=True)   # best first
        keep = items[:MAX_PER_THEME]
        # Reserve ONE per-theme slot for an embeddable social clip (IG/TikTok/YouTube). Clips
        # arrive as thin text-only captions, so they score lower and get crowded out by richer
        # articles — but the video IS the story and social reach matters. If no clip made the
        # theme's top MAX_PER_THEME, promote the best clip that DID pass the judge into the last
        # slot (displacing the weakest non-clip). Only ever promotes an item already over the
        # bar — never lowers the pass score, just guarantees at most one clip a theme isn't buried.
        if not any(_is_clip_item(it) for it in keep):
            clip = next((it for it in items[MAX_PER_THEME:] if _is_clip_item(it)), None)
            if clip:
                keep = keep[:MAX_PER_THEME - 1] + [clip]
        selected.extend(keep)
        dropped += max(0, len(items) - len(keep))
    selected.sort(key=lambda it: it["rel"], reverse=True)
    if len(selected) > MAX_NEW:
        dropped += len(selected) - MAX_NEW
        selected = selected[:MAX_NEW]
    return selected, dropped

def create_records(records):
    """POST the new rows in chunks of 10; return the created records (each with its id +
    stored fields), in the SAME order they were sent — Airtable preserves request order, so
    created[i] corresponds to records[i]. The caller uses the ids to retry media (see
    reenrich_new_rows)."""
    created = []
    for i in range(0, len(records), 10):
        chunk = records[i:i+10]
        body = json.dumps({"records": chunk, "typecast": True}).encode()
        resp = http_json(f"https://api.airtable.com/v0/{BASE}/{TABLE}",
                         headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}",
                                  "Content-Type": "application/json"},
                         data=body, method="POST")
        created.extend(resp.get("records", []))
    return created


def reenrich_new_rows(created, posts):
    """Give the rows THIS run just wrote a SECOND shot at heading media — scoped to this
    run's new stories only, never a whole-table sweep.

    Harvest-time enrichment (build_fields -> enrich_media) is one-shot: a page that's
    momentarily blocked/slow/throttled when the row is first built leaves the card bare with
    no retry, so it shows the placeholder forever even though the og:image is reachable
    seconds later. Here we wait a short beat (ENRICH_RETRY_DELAY), then re-open ONLY the
    just-created rows that came out with no Media URL and PATCH any that now yield an
    image/video - BEFORE publish.py promotes them to Published, so a card that CAN carry an
    image gets one before it goes live. `created[i]` pairs with `posts[i]` (same order).
    Best-effort throughout: any fetch/PATCH failure leaves the row exactly as it was."""
    bare = [(rec, p) for rec, p in zip(created, posts)
            if not (rec.get("fields", {}).get("Media URL") or "").strip()
            and (rec.get("fields", {}).get("Format") or "Text") == "Text"]
    if not bare:
        return
    print(f"  Re-enrich: {len(bare)} new card(s) came out bare; retrying media in "
          f"{ENRICH_RETRY_DELAY}s (this run's rows only)...")
    time.sleep(ENRICH_RETRY_DELAY)
    updates = []
    for rec, p in bare:
        try:
            fmt, media = enrich_media(p, "Text", "")
        except Exception as e:
            print(f"    . re-enrich skipped {p.get('title', '')[:40]} ({getattr(e, 'code', e)})")
            continue
        if media:
            updates.append({"id": rec["id"], "fields": {"Media URL": media, "Format": fmt}})
    for i in range(0, len(updates), 10):
        try:
            http_json(f"https://api.airtable.com/v0/{BASE}/{TABLE}",
                      headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}",
                               "Content-Type": "application/json"},
                      data=json.dumps({"records": updates[i:i+10], "typecast": True}).encode(),
                      method="PATCH")
        except Exception as e:
            print(f"    . re-enrich PATCH failed ({getattr(e, 'code', e)})")
            return
    if updates:
        print(f"  Re-enrich: recovered media for {len(updates)}/{len(bare)} new card(s).")

# --- main -------------------------------------------------------------------
def build_fields(p, t, title, summary, same_event=True):
    """The new Stories row for a kept item. Holds (rulebook sections 6 and 7) keep it out of the
    gallery: a risky picture or identifiable child (the judge's flag), a summary that couldn't be
    written (retried next sweep), or a title and summary about different events."""
    theme = t.get("theme") if t.get("theme") in THEMES else None
    fmt, media = detect_format_and_media(p)
    fmt, media = enrich_media(p, fmt, media)   # open the page for a heading image / embedded video
    sens = "Sensitive" if (t.get("sensitive") or p["over_18"]) else "Safe"
    headline = clean_title(p["title"])[:250]
    fields = {
        "Title": (title or headline)[:250], "Source headline": headline,
        "Status": "Candidate", "Summary": summary,
        "Source URL": story_source_url(p), "Media URL": media, "Format": fmt,
        "Relevance": overall_score(t), "Sensitivity": sens,
        "Source type": p.get("provenance", "Manual"),
        "Editor notes": f"Auto-harvested from {p.get('source_label') or 'feed'}."[:900],
        "Source name": (p.get("source_label") or "")[:250],
        **judge_fields(t),
    }
    hold = None
    if t.get("hold") in HOLD_REASONS:
        hold = t["hold"]
    elif not summary:
        hold, fields["Summary tries"] = "Summary failed", 1
    elif not same_event:
        hold = "Title and summary differ"
    if hold:
        fields["Hold reason"] = hold
        fields["Held since"] = datetime.date.today().isoformat()
    if theme:
        fields["Theme"] = theme
    if p["created"]:
        fields["Date"] = p["created"]
    return fields

def retry_held_summaries(deadline):
    """Rulebook section 7: a card whose summary couldn't be written is held and retried here, once a
    sweep. Success clears the hold (publish.py then treats it like any new card); a second failure
    archives the story. Returns copy tokens spent."""
    formula = "AND({Status}='Candidate', {Hold reason}='Summary failed')"
    rows, offset, spent = [], None, 0
    try:
        while True:
            u = (f"https://api.airtable.com/v0/{BASE}/{TABLE}?pageSize=100"
                 f"&filterByFormula={urllib.parse.quote(formula)}" + (f"&offset={offset}" if offset else ""))
            data = http_json(u, headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"})
            rows.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset:
                break
    except Exception as e:
        print(f"  ! Could not read held summaries: {e}")
        return 0
    updates = []
    for r in rows:
        if time.time() > deadline:
            break
        f = r.get("fields", {})
        post = {"title": f.get("Source headline") or f.get("Title", ""), "selftext": "",
                "permalink": f.get("Source URL", ""), "external_url": f.get("Source URL", ""),
                "provenance": f.get("Source type", ""), "source_label": f.get("Source name", ""),
                "subreddit": None, "created": f.get("Date", "")}
        title, summary, same, tok = write_copy(post)
        spent += tok
        time.sleep(COPY_SLEEP)
        tries = int(f.get("Summary tries") or 1) + 1
        if summary:
            fx = {"Title": title[:250], "Summary": summary, "Summary tries": tries,
                  "Hold reason": None if same else "Title and summary differ",
                  "Held since": None if same else datetime.date.today().isoformat()}
        elif tries >= 2:
            fx = {"Status": "Archived", "Summary tries": tries}
        else:
            fx = {"Summary tries": tries}
        updates.append({"id": r["id"], "fields": fx})
    for i in range(0, len(updates), 10):
        try:
            http_json(f"https://api.airtable.com/v0/{BASE}/{TABLE}",
                      headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"},
                      data=json.dumps({"records": updates[i:i+10], "typecast": True}).encode(), method="PATCH")
        except Exception as e:
            print(f"  ! Could not update held summaries: {e}")
            break
    if rows:
        fixed = sum(1 for u in updates if "Summary" in u["fields"])
        gone = sum(1 for u in updates if u["fields"].get("Status") == "Archived")
        print(f"  Held summaries: {len(rows)} retried, {fixed} written, {gone} archived after 2 tries.")
    return spent

def kept_windows(n_short, n_long):
    """One Stories scan → (kept_short, kept_long) Counters of {Source name: count} for
    still-standing (not owner-Rejected) stories harvested in the last n_short / n_long days.
    'Kept' is authoritative from the Stories table (each kept row carries Source name +
    Date Harvested), so these rollups are always exact — no accumulated state needed."""
    today = datetime.date.today()
    cut_long = (today - datetime.timedelta(days=n_long)).isoformat()
    cut_short = (today - datetime.timedelta(days=n_short)).isoformat()
    formula = f"AND(IS_AFTER({{Date Harvested}}, '{cut_long}'), {{Status}}!='Rejected')"
    short, long, offset = Counter(), Counter(), None
    while True:
        u = (f"https://api.airtable.com/v0/{BASE}/{TABLE}?pageSize=100"
             f"&fields%5B%5D=Source%20name&fields%5B%5D=Date%20Harvested"
             f"&filterByFormula={urllib.parse.quote(formula)}")
        if offset:
            u += f"&offset={offset}"
        data = http_json(u, headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"})
        for r in data.get("records", []):
            f = r.get("fields", {})
            name = (f.get("Source name") or "").strip()
            if not name:
                continue
            long[name] += 1
            d = (f.get("Date Harvested") or "")[:10]
            if d and d > cut_short:          # within the short window too
                short[name] += 1
        offset = data.get("offset")
        if not offset:
            break
    return short, long

def _parse_found_daily(s):
    """'2026-08-17:5, 2026-08-16:3' → {date: count}. Tolerant of junk/blank cells."""
    out = {}
    for part in (s or "").split(","):
        part = part.strip()
        if ":" not in part:
            continue
        d, _, c = part.rpartition(":")
        d = d.strip()
        if len(d) == 10 and c.strip().isdigit():
            out[d] = out.get(d, 0) + int(c.strip())
    return out

def _fmt_found_daily(hist):
    """{date: count} → 'YYYY-MM-DD:count, …' newest first."""
    return ", ".join(f"{d}:{c}" for d, c in sorted(hist.items(), reverse=True))

def _fetch_yield_rows(table_id, key_field, active_only):
    """All rows of a yield target (id + key_field + existing 'Found daily'), paged."""
    rows, offset = [], None
    while True:
        u = (f"https://api.airtable.com/v0/{BASE}/{table_id}?pageSize=100"
             f"&fields%5B%5D={urllib.parse.quote(key_field)}&fields%5B%5D=Found%20daily"
             f"&fields%5B%5D=Judged%20daily")
        if active_only:
            u += f"&filterByFormula={urllib.parse.quote('{Active}=1')}"
        if offset:
            u += f"&offset={offset}"
        data = http_json(u, headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"})
        rows.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    return rows

def _write_yield(table_id, key_field, rows, found_now, kept_now, kept10, kept30,
                 today_s, cut_short, cut_long, judged_now=None):
    """Compute the yield columns + accumulated 'Found daily' / 'Judged daily' for each row (keyed
    by key_field) and PATCH them. Returns rows written, or -1 if the yield fields are absent
    (422 → skip, don't raise). 'Found' and 'Judged' are accumulated into history strings; 'Kept'
    is the exact rolling count passed in."""
    updates = []
    for r in rows:
        key = (r.get("fields", {}).get(key_field) or "").strip()
        if not key:
            continue
        hist = _parse_found_daily(r.get("fields", {}).get("Found daily", ""))
        hist[today_s] = hist.get(today_s, 0) + int(found_now.get(key, 0))
        hist = {d: c for d, c in hist.items() if d > cut_long}       # trim to long window
        extra = {}
        if judged_now is not None:      # how many of the source's items the judge actually read
            jh = _parse_found_daily(r.get("fields", {}).get("Judged daily", ""))
            jh[today_s] = jh.get(today_s, 0) + int(judged_now.get(key, 0))
            jh = {d: c for d, c in jh.items() if d > cut_long}
            extra = {"Judged last run": int(judged_now.get(key, 0)), "Judged (30d)": sum(jh.values()),
                     "Judged daily": _fmt_found_daily(jh)}
        updates.append({"id": r["id"], "fields": {**extra,
            "Found last run": int(found_now.get(key, 0)),
            "Kept last run": int(kept_now.get(key, 0)),
            "Found (10d)": sum(c for d, c in hist.items() if d > cut_short),
            "Found (30d)": sum(hist.values()),
            "Kept (10d)": int(kept10.get(key, 0)), "Kept (30d)": int(kept30.get(key, 0)),
            "Found daily": _fmt_found_daily(hist),
        }})
    for i in range(0, len(updates), 10):
        try:
            http_json(f"https://api.airtable.com/v0/{BASE}/{table_id}",
                      headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}",
                               "Content-Type": "application/json"},
                      data=json.dumps({"records": updates[i:i+10], "typecast": True}).encode(),
                      method="PATCH")
        except urllib.error.HTTPError as e:
            if e.code == 422:
                return -1
            raise
    return len(updates)

def update_yields(found_by_src, kept_by_src, judged_by_src):
    """Roll this run's per-source yield onto the Sources table (keyed by Name, active rows), so the
    owner can see which sources are actually producing:
      • Found/Judged/Kept last run — what the source supplied, how much the judge read, what it landed.
      • Found (10d)/(30d), Judged (30d) — rolling sums. Neither can be recovered from Airtable
        (rejected items aren't stored) so they're ACCUMULATED into 'Found daily' / 'Judged daily'.
      • Kept (10d)/(30d) — exact rolling counts from the Stories table (Rejected excluded).
    Best-effort: never breaks the run; self-heals (logs) if the fields are absent."""
    try:
        today = datetime.date.today()
        today_s = today.isoformat()
        cut_long = (today - datetime.timedelta(days=STATS_WINDOW_LONG)).isoformat()
        cut_short = (today - datetime.timedelta(days=STATS_WINDOW_SHORT)).isoformat()
        kept10, kept30 = kept_windows(STATS_WINDOW_SHORT, STATS_WINDOW_LONG)   # per Source name
        n = _write_yield(SOURCES_TABLE, "Name", _fetch_yield_rows(SOURCES_TABLE, "Name", True),
                         found_by_src, kept_by_src, kept10, kept30, today_s, cut_short, cut_long,
                         judged_now=judged_by_src)
        print("  ! Source-yield fields missing — skipped." if n < 0
              else f"  Per-source yield → {n} active Sources.")
    except Exception as e:
        print(f"  ! Could not update yield stats: {e}")

def write_run(s):
    """Log one row to the Runs table so coverage is visible, not silent."""
    fields = {
        "Run": s["run"], "Mode": s["mode"],
        "Sources tried": s["tried"], "Sources read": s["read"], "Sources failed": s["failed_n"],
        "Failed list": ", ".join(s["failed"])[:900] if s["failed"] else "",
        "Fetched": s["fetched"], "Judged": s["judged"], "Kept": s["kept"], "Errors": s["errors"],
        "Tokens": s.get("tokens", 0),
        # Per-stage token spend — each model has its OWN 200K/day bucket, so a combined total
        # can't show which bucket is near-throttle. Optional: self-healed away if not yet in base.
        "Tokens A (triage)": s.get("tok_triage", 0),
        "Tokens B (judge)": s.get("tok_judge", 0),
        "Tokens C (copy)": s.get("tok_copy", 0),
        "Judge mode": s.get("judge_mode", ""),
        # Count per stage (release 3): where items drop out on their way to the gallery.
        "Too old": s.get("too_old", 0), "Duplicates": s.get("duplicates", 0),
        "YouTube gate dropped": s.get("yt_dropped", 0), "Triage dropped": s.get("triage_dropped", 0),
        "Not judged (ceiling)": s.get("not_judged", 0), "Passed bar": s.get("passed", 0),
        "Held": s.get("held", 0), "Triage drop sample": s.get("triage_sample", ""),
        "Rulebook version": int(rulebook.version()),
    }
    # Token fields may not exist in the base yet (added via Airtable UI or metadata API later).
    # A 422 UNKNOWN_FIELD_NAME names one absent field at a time; pop the named one and retry,
    # so the run row still logs with whatever token columns DO exist.
    OPTIONAL = ("Tokens", "Tokens A (triage)", "Tokens B (judge)", "Tokens C (copy)", "Judge mode",
                "Too old", "Duplicates", "YouTube gate dropped", "Triage dropped", "Not judged (ceiling)",
                "Passed bar", "Held", "Triage drop sample", "Rulebook version")
    def _post(fx):
        http_json(f"https://api.airtable.com/v0/{BASE}/{RUNS_TABLE}",
                  headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"},
                  data=json.dumps({"records": [{"fields": fx}], "typecast": True}).encode(), method="POST")
    for _ in range(len(OPTIONAL) + 1):
        try:
            _post(fields)
            return
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            # Find which optional field Airtable rejected and drop just that one. Match the
            # QUOTED name so "Tokens" doesn't shadow "Tokens B (judge)" (substring trap).
            missing = next((f for f in OPTIONAL if f in fields and f'"{f}"' in body), None)
            if e.code == 422 and missing:
                fields.pop(missing)
                continue
            # Unparseable 422 but optional token fields remain → drop them all and retry once.
            if e.code == 422 and any(f in fields for f in OPTIONAL):
                for f in OPTIONAL:
                    fields.pop(f, None)
                continue
            print(f"  ! Could not log run: {e}")
            return
        except Exception as e:
            print(f"  ! Could not log run: {e}")
            return

def main():
    dry = os.environ.get("DRY_RUN") == "1"
    mode = "Weekly sweep" if SWEEP else ("Manual" if dry else "Daily")
    print(("*** DRY RUN — judging only, nothing written ***\n" if dry else "")
          + f"Harvest mode: {mode}. Window: last {WINDOW_DAYS} days. Fetching candidates…")
    deadline = time.time() + MAX_RUNTIME_MIN * 60
    sources = load_sources()
    print(f"  {len(sources)} active sources loaded from Airtable.")

    seen, posts, failed, read_ok, yt_skipped = set(), [], [], 0, 0
    for s in sources:
        if time.time() > deadline:
            print("  ! Time budget reached during fetch — stopping early.")
            break
        typ = s["type"]
        if typ == "YouTube search":                       # Lane 2 (API, not a URL fetch)
            if not YOUTUBE_KEY:
                yt_skipped += 1
                continue                                  # no key yet — skip cleanly, no "failed" noise
            res = fetch_youtube_search(s["name"])
        elif typ == "RSS feed":
            urls = source_urls(s)
            res = fetch_feed(urls[0], s["name"]) if urls else None
        else:                                             # Subreddit (2 passes: new+top) / Keyword
            urls = source_urls(s)
            merged, any_ok = [], False
            for i, u in enumerate(urls):
                r = fetch_atom(u, s["name"])
                if r is not None:
                    any_ok = True
                    merged.extend(r)
                if i < len(urls) - 1:
                    time.sleep(REDDIT_SLEEP)               # space the two Reddit passes apart
            res = merged if any_ok else None               # None only if every pass failed
        if res is None:
            failed.append(s["name"])       # source failed (429/404/parse/quota)
        else:
            read_ok += 1
            for p in res:
                if p["permalink"] in seen:
                    continue
                seen.add(p["permalink"])
                posts.append(p)
        # Only Reddit IP-throttles, so only Reddit needs the long spacing. Feeds,
        # Google Alerts and YouTube don't — a token pause keeps them polite without
        # burning the wall-clock budget.
        time.sleep(REDDIT_SLEEP if typ in ("Subreddit", "Keyword") else 1)
    if yt_skipped:
        print(f"  Lane 2: skipped {yt_skipped} YouTube-search sources (no YOUTUBE_API_KEY set yet).")
    fetched_total = len(posts)
    print(f"  {read_ok}/{len(sources)} sources read ({len(failed)} failed), {fetched_total} unique posts fetched.")

    # Recency window (A): drop anything older than the cutoff so we don't re-judge stale
    # items every run (rejected posts aren't stored, so undated/old ones would recur).
    cutoff = _cutoff()
    before_win = len(posts)
    posts = [p for p in posts if within_window(p, cutoff)]
    n_too_old = before_win - len(posts)
    print(f"  Recency window: kept {len(posts)}/{before_win} posts dated ≥ {cutoff} "
          f"(last {WINDOW_DAYS}d; undated kept).")

    # Spam-link filter: a post whose outbound link is a bare IP address is almost always
    # spam / a malware bait link / a scraper repost — never a credible source. Drop it
    # outright (pre-judge, so it costs no Groq) rather than showing it. Applies to every
    # source, though in practice only Reddit user-submissions ever carry raw-IP links.
    before_ip = len(posts)
    posts = [p for p in posts if not is_ip_url(p.get("external_url", ""))]
    if before_ip != len(posts):
        print(f"  Spam-link filter: dropped {before_ip - len(posts)} post(s) linking a raw IP.")

    known = existing_source_urls()
    before_dup = len(posts)
    if dry:
        n_in = sum(1 for p in posts if dedup_keys(p) & known)
        print(f"  {n_in} of these are already in Airtable (shown as [in-base]).")
    else:
        posts = [p for p in posts if not (dedup_keys(p) & known)]
        print(f"  {len(posts)} after removing ones already in Airtable.")
    n_dup = before_dup - len(posts)
    n_yt_dropped = 0

    # AI-relevance gate for YouTube whole-channel feeds (see ai_relevant). Other
    # sources bypass it. Applied before shuffle so filtered posts never cost a judge call.
    yt_before = sum(1 for p in posts if p.get("provenance") == "YouTube")
    if yt_before:
        gate = load_gate_matchers()
        posts = [p for p in posts if p.get("provenance") != "YouTube" or ai_relevant(p, gate)]
        yt_after = sum(1 for p in posts if p.get("provenance") == "YouTube")
        n_yt_dropped = yt_before - yt_after
        print(f"  YouTube AI-gate: kept {yt_after}/{yt_before} YouTube posts "
              f"({yt_before - yt_after} non-AI filtered before judging).")

    # Clips-only toggle (Option A). When CLIPS_ONLY is set, keep only Reddit posts that
    # link an embeddable IG/TikTok/YouTube clip — making the site clip-first. Applied
    # before shuffle so dropped posts never cost a judge call. Off by default (enrich).
    if CLIPS_ONLY:
        red_before = sum(1 for p in posts if p.get("provenance") == "Reddit")
        posts = [p for p in posts
                 if p.get("provenance") != "Reddit" or is_social_clip(p.get("external_url", ""))]
        red_after = sum(1 for p in posts if p.get("provenance") == "Reddit")
        print(f"  CLIPS_ONLY: kept {red_after}/{red_before} Reddit posts "
              f"(dropped {red_before - red_after} with no IG/TikTok/YouTube clip).")

    random.shuffle(posts)   # mix sources so the judge budget isn't eaten by whoever loaded first

    # Snapshot each source's supply to the judging pool BEFORE pre-triage, so the Sources /
    # Coverage-Map "Found" yield reflects what a source actually supplied (in-window, deduped),
    # not what survived our own filter. Stage A's own "kept X/Y" log shows the filter's effect.
    found_by_src = Counter(p.get("source_label", "") for p in posts)

    # Stage A — cheap pre-triage (gpt-oss-20b) drops obvious junk so the expensive judge's
    # scarce budget is spent on plausible candidates. Fail-open; its own rate-limit bucket.
    posts, tok_triage, triage_dropped = pretriage(posts, deadline)

    kept, seen_titles, judged, errors, n_keep, n_rej = [], set(), 0, 0, 0, 0
    kept_posts = []                          # post objects parallel to `kept`, for the media retry
    consec_err, consec_rl = 0, 0             # genuine-error streak vs rate-limit streak (tracked apart)
    # Per-model token metering: each stage runs on its OWN 200K/day bucket, so track them apart
    # (a single lumped number can't tell you which bucket is nearing its cap).
    tok_judge, tok_copy = 0, 0
    # Judge EVERY survivor, collecting all that pass the bar; SELECTION happens after, so we
    # pick the best-per-theme rather than the first-per-theme. (Copy is written only for the
    # final picks, further down — never for an item that a higher-scoring rival displaces.)
    passed = []          # [{"p":post, "t":verdict, "rel":int, "theme":str}, …]
    judged_by_src = Counter()                # per source: how many of its items the judge read
    # Pre-dedup: drop same-title cross-posts BEFORE batching, so batch slots aren't spent on dupes
    # and the [i] indices stay clean.
    to_judge = []
    for p in posts:
        norm = norm_title(p["title"])        # outlet-stripped, so cross-outlet reprints collide
        if norm in seen_titles:
            n_dup += 1
            continue
        seen_titles.add(norm)
        to_judge.append(p)
    batched = JUDGE_BATCH > 1
    sleep_between = JUDGE_BATCH_SLEEP if batched else JUDGE_SLEEP
    n_batches, stop = 0, False
    print(f"  Stage-B judge ({JUDGE_MODEL}): {'batched ×' + str(JUDGE_BATCH) if batched else 'single'}"
          f" — {len(to_judge)} survivors to judge")
    # Judge EVERY survivor (batched by JUDGE_BATCH), collecting all that pass the bar; SELECTION
    # happens after so we pick best-per-theme, not first-per-theme.
    for start in range(0, len(to_judge), JUDGE_BATCH):
        if judged >= MAX_JUDGE:              # safety ceiling only; normally never hit
            print(f"  ! MAX_JUDGE ({MAX_JUDGE}) reached — stopping.")
            break
        if time.time() > deadline:
            print("  ! Time budget reached during judging — stopping early.")
            break
        chunk = to_judge[start:start + JUDGE_BATCH]
        # Stage B — the judge (gpt-oss-120b): one call per batch (or per item when JUDGE_BATCH=1).
        if batched:
            verdicts, tok, status = groq_judge_batch(chunk)
        else:
            v, tok, status = groq_judge(chunk[0]); verdicts = [v]
        tok_judge += tok
        n_batches += 1
        # A transient 429 (bucket briefly exhausted) is NOT a failure: back off, skip this chunk,
        # keep going. Only a PERSISTENT streak (the daily cap genuinely hit) stops the run.
        if status == "rate_limited":
            consec_rl += 1
            print(f"  ~ rate-limited, skipping {len(chunk)} item(s) (not judged)")
            time.sleep(sleep_between)
            if consec_rl >= 5:
                print("  ! Judge rate-limited persistently — likely daily cap; stopping early.")
                break
            continue
        consec_rl = 0
        # Whole-batch parse failure → fall back to the proven single path for this chunk, so a
        # malformed batch degrades gracefully instead of dropping every item in it.
        if batched and status == "error" and not any(verdicts):
            for k, p in enumerate(chunk):
                sv, stok, sstat = groq_judge(p); tok_judge += stok
                verdicts[k] = sv if sstat == "ok" else None
                time.sleep(JUDGE_SLEEP)
        for k, p in enumerate(chunk):
            t = verdicts[k] if k < len(verdicts) else None
            # An item the model omitted from an otherwise-parsed batch → one single re-judge, so
            # batching never silently loses a story (rare — array parsing was 100% in testing).
            if t is None and batched and status == "ok":
                sv, stok, sstat = groq_judge(p); tok_judge += stok
                t = sv if sstat == "ok" else None
                time.sleep(JUDGE_SLEEP)
            tag = " [in-base]" if (dedup_keys(p) & known) else ""
            judged += 1
            judged_by_src[p.get("source_label", "")] += 1
            if not t:                        # genuine error (bad HTTP / unparseable / omitted)
                errors += 1
                consec_err += 1
                print(f"  ? ERROR                  {p['title'][:55]}{tag}")
                if consec_err >= 4:          # judge truly down/blocked — stop grinding, log, exit
                    print("  ! Judge failing repeatedly — stopping early (blocked or down).")
                    stop = True
                    break
                continue
            consec_err = 0
            verd = judge_verdict(t)
            if dry:
                n_keep += verd == "Keep"; n_rej += verd != "Keep"
                print(f"  {verd:<6} [{overall_score(t):>3}] {str(t.get('theme', '?'))[:22]:<22}{tag} "
                      f"{p['title'][:50]} :: {str(t.get('reason', ''))[:55]}")
            if verd != "Keep":
                continue
            passed.append({"p": p, "t": t, "rel": overall_score(t), "theme": t["theme"]})
        if stop:
            break
        time.sleep(sleep_between)            # pace under the judge's TPM bucket between batches
    print(f"  Stage-B judged {judged}/{len(to_judge)} survivors across {n_batches} call(s).")

    # --- SELECTION: keep the best per theme, then cap overall (see select_best) ---
    selected, dropped_by_cap = select_best(passed)
    print(f"  Selection: {len(passed)} passed → {len(selected)} kept "
          f"(best {MAX_PER_THEME}/theme; {dropped_by_cap} lower-scoring in-scope items dropped).")

    def _tok_line():                         # per-bucket metering vs each model's 200K/day cap
        per = f", {tok_judge // judged}/judge" if judged else ""
        print(f"  Groq tokens this run: {tok_triage + tok_judge + tok_copy} total — "
              f"A/pre-triage {tok_triage} ({TRIAGE_MODEL}), "
              f"B/judge {tok_judge} ({JUDGE_MODEL}{per}), C/copy {tok_copy} ({COPY_MODEL}).")

    if dry:
        _tok_line()
        print(f"\nDRY RUN complete — {n_keep} passed the bar, {n_rej} weak or rejected, {judged} judged, "
              f"{len(selected)} would be kept (best {MAX_PER_THEME}/theme). Nothing written.")
        return

    # Cross-run reprint corpus: recent still-standing rows to compare new copy against.
    dedup_sigs = existing_signatures()
    n_reprint = 0
    # Cards held last sweep because their summary failed get their second try first.
    tok_copy += retry_held_summaries(deadline)
    # Stage C — write the card copy (qwen bucket) for the FINAL selected keepers only.
    for it in selected:
        title, summary, same_event, ctok = write_copy(it["p"])
        tok_copy += ctok
        time.sleep(COPY_SLEEP)
        # Cross-run near-dup: same event reprinted at a different URL (needs BOTH title AND
        # summary near-identical, so a developing story with new facts is kept).
        dup, match = is_reprint(it["p"]["title"], summary, dedup_sigs) if summary else (False, "")
        if dup:
            n_reprint += 1
            print(f"  ⊘ reprint (skipped) — {it['p']['title'][:50]} ≈ {match[:40]}")
            continue
        fields = build_fields(it["p"], it["t"], title, summary, same_event)
        dedup_sigs.append((norm_title(it["p"]["title"]), (summary or "").lower().strip()))  # catch same-run reprints too
        kept.append({"fields": fields})
        kept_posts.append(it["p"])   # keep the post alongside so reenrich_new_rows can retry bare cards
        held = f" (held: {fields['Hold reason']})" if fields.get("Hold reason") else ""
        print(f"  + [{it['rel']}] {it['theme']} — {(title or it['p']['title'])[:60]}{held}")
    if n_reprint:
        print(f"  Cross-run dedup: {n_reprint} reprint(s) of existing stories skipped.")
    _tok_line()
    total_tokens = tok_triage + tok_judge + tok_copy

    if kept:
        created = create_records(kept)
        reenrich_new_rows(created, kept_posts)   # second, patient media attempt on THIS run's bare rows
    # Per-source yield → Sources table. "Found" = this source's share of the judging pool it
    # supplied (snapshotted above, BEFORE pre-triage); "Kept" = how many it landed. Lets the
    # owner see which alerts/feeds/subs earn their place vs which are noise. (kept carries each
    # row's "Source name".)
    kept_by_src = Counter((k["fields"].get("Source name") or "") for k in kept)
    update_yields(found_by_src, kept_by_src, judged_by_src)
    write_run({
        "run": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "mode": mode,
        "tried": len(sources), "read": read_ok, "failed_n": len(failed), "failed": failed,
        "fetched": fetched_total, "judged": judged, "kept": len(kept), "errors": errors,
        "tokens": total_tokens,
        "tok_triage": tok_triage, "tok_judge": tok_judge, "tok_copy": tok_copy,
        "judge_mode": f"batch-{JUDGE_BATCH}" if JUDGE_BATCH > 1 else "single",
        "too_old": n_too_old, "duplicates": n_dup, "yt_dropped": n_yt_dropped,
        "triage_dropped": len(triage_dropped), "not_judged": max(0, len(to_judge) - judged),
        "passed": len(passed), "held": sum(1 for k in kept if k["fields"].get("Hold reason")),
        "triage_sample": triage_sample(triage_dropped),
    })
    print(f"\nDone. Mode={mode}. {read_ok}/{len(sources)} sources read, {judged} judged, "
          f"{errors} errors, {total_tokens} Groq tokens, wrote {len(kept)} new Candidate rows. Logged to Runs table.")

if __name__ == "__main__":
    main()
