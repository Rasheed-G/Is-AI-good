"""Reddit harvester for AI Harm Watch.

Reads Reddit's PUBLIC search JSON (no app / no OAuth / no Responsible-Builder
sign-up needed), asks Groq to judge + summarise each post, dedupes against what
is already in Airtable, and writes genuinely relevant new stories as
`Candidate` rows for the owner to review.

Run locally:  source .env.local && python scripts/harvest_reddit.py
"""
import os, sys, json, time, html, re, random, datetime, difflib, email.utils, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from collections import Counter

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
COPY_MODEL   = "qwen/qwen3.6-27b"      # Stage C: card summary, keepers only
GROQ_MODEL = JUDGE_MODEL               # back-compat alias for any external caller

# Sources (subreddits / keywords / feeds) live in the Airtable "Sources" table so the
# owner can add or pause them with NO code. The harvester reads Active rows each run.
SOURCES_TABLE = "tbllWd5Rby0pkgIpB"
RUNS_TABLE = "tblR1wvxB46FtwMia"
CMAP_TABLE = "tblq6vFY3ldPy0KPM"   # Coverage Map (concepts). Per-source yield rolls up here by
# each Source's stored `Concept` (set once), so the owner sees which concepts are producing.
SWEEP = os.environ.get("SWEEP") == "1"   # weekly top-of-week sweep (vs the daily "new" run)
# Option A (social clips). When a Reddit post links an Instagram/TikTok/YouTube clip we
# surface that embed as the story's media instead of the article/text (always on — see
# _pick_external). CLIPS_ONLY additionally DROPS Reddit posts that have no such clip,
# making the site clip-first. Default OFF = "enrich" mode (clips preferred, text kept).
CLIPS_ONLY = os.environ.get("CLIPS_ONLY", "").strip().lower() in ("1", "true", "yes", "on")

THEMES = [
    "Deepfakes & image abuse", "Scams & fraud", "Environmental / data-centre impact",
    "Jobs & livelihoods", "Kids & safety", "Surveillance", "Misinformation",
    "Breaches & system failures", "Even the experts are worried",
    "Bias & discrimination", "Automated decisions & welfare", "AI companions & mental health",
]

# Recency window (A): each run only looks at RECENT posts, so we don't re-judge stale
# items every day (rejected posts aren't stored, so without this they'd be re-judged
# forever). Daily = last 2 days; weekly sweep = last 7. Reddit's `t` param + YouTube's
# publishedAfter bound server-side; feeds/`new.rss` are filtered client-side by date.
WINDOW_DAYS = 7 if SWEEP else 2
REDDIT_T = "week" if SWEEP else "day"   # Reddit `top`/search time filter (hour/day/week/month/year/all)
PER_SOURCE = 25           # max posts fetched per source-pass (the window bounds the rest, not this)
# Theme-balanced writing (C): spread candidates across the 12 themes rather than letting
# one loud theme fill the batch. Per-theme cap + an overall ceiling; judge widely so every
# theme gets a look. Actual judged count is now bounded by how many NEW items the window holds.
MAX_PER_THEME = 4         # keep the BEST N per theme (by relevance) after judging everything
MAX_NEW = 48              # overall ceiling on new candidates per run (= 12 themes × 4)
MAX_JUDGE = 250           # safety ceiling on judge calls (a runaway pool); the real bound is now
                          # the judge's 200K-TPD bucket (~168 judges) + the wall-clock budget.
                          # Selection is judge-EVERYTHING-then-pick-best-per-theme, not cap-early.
MIN_RELEVANCE = 70        # drop anything the judge scores below this (strict judge)
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
JUDGE_BATCH_SLEEP = int(os.environ.get("JUDGE_BATCH_SLEEP", "15"))  # sec between batch calls;
                          # batch-tok/sec ≈ the old single 8s spacing, so TPM headroom is unchanged.
MAX_RUNTIME_MIN = 55      # hard wall-clock budget (CI timeout is 65m): stop gracefully + log.
                          # 38→44 to fit the added Stage-A/Stage-C calls; 44→55 once judging went
                          # batched (public repo = unlimited Actions minutes) so a FULL survivor
                          # pool gets judged with margin instead of the old early-stop at 44m.
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
# Open discovery across the 12 themes (rows of Type "YouTube search" in the Sources
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
             f"&fields%5B%5D=Title&fields%5B%5D=Summary"
             f"&filterByFormula={urllib.parse.quote(formula)}")
        if offset:
            u += f"&offset={offset}"
        data = http_json(u, headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"})
        for r in data.get("records", []):
            f = r.get("fields", {})
            nt = norm_title(f.get("Title", ""))
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

def _origin_line(post):
    # Source-agnostic origin — the harvester feeds Reddit, news RSS, Google Alerts, YouTube
    # and incident-DB entries, so never tell the judge everything is a "Reddit post".
    if post.get("subreddit"):
        return f"Source: Reddit — r/{post['subreddit']}"
    prov, lbl = post.get("provenance", ""), post.get("source_label", "")
    return "Source: " + (f"{prov} — {lbl}" if (prov and lbl) else (prov or lbl or "unknown"))

# --- Stage B: the editorial judge (gpt-oss-120b) ----------------------------
# The calibrated decision rules (STEP 1/2 + GUARDRAIL + SCOPE + COHERENCE) — the SINGLE source
# of truth for judge behaviour, shared verbatim by the single judge and the batched judge so the
# two can never drift. Only the I/O envelope (single item + single schema vs many items + array)
# differs between them; the rules the model applies are identical.
JUDGE_RUBRIC = """STEP 1 — classify "kind":
- "human-impact": a real, identifiable person or community harmed by AI (deepfake victim, scam loss, wrongful arrest, job loss), INCLUDING a genuine FIRST-PERSON account of emotional, psychological, or relational harm from AI (e.g. dependence on an AI companion, chatbot mental-health effects). Firsthand lived experience only — not speculation or someone theorising about others.
- "incident": a specific, verifiable AI-related event — a named AI-platform breach/hack/leak, a model failure causing harm, a large-scale deepfake/fraud campaign.
- "expert-analysis": a journalist or researcher explaining a REAL AI harm/finding/trend — OR a striking statistic about AI harm (e.g. "AI-driven fraud up 1,400%") BUT ONLY if it attributes a credible source (a named report, study, agency, or news outlet). An unsourced statistic with no attribution is hype, not analysis.
- "none": anything else.

STEP 2 — apply the bar. Force kind="none" unless it clearly meets its type:
- human-impact needs an identifiable person/community AND a concrete harm, OR a clear first-person account of real emotional/psychological/relational harm from AI.
- incident needs a specific, verifiable event (name the org/system, what happened, roughly when). "Breaches are rising" is NOT enough; a named breach IS.
- expert-analysis must be grounded in a specific finding/event/data from a credible source; a statistic or trend MUST cite its source.

UNIVERSAL GUARDRAIL — force kind="none" if it is: an UNSOURCED hype/fear-marketing stat ("X% of firms exposed" with no credible attribution), a product/vendor promo, a generic how-to/advice listicle, opinion/speculation with no real event, a meme/joke, NOT in English, or AI-generated/unverifiable.

SCOPE — the test for EVERY item: does it help a general reader understand a REAL risk, danger, or harm of AI, or how AI is concretely affecting people or the world? If yes, it is IN scope even without a single named victim. If it only reports the human politics/process AROUND AI and illustrates no concrete AI danger, it is out. Force kind="none" for:
- Protest / activism / arrests-of-protesters / PAC / lobbying / political he-said-she-said that CENTERS on the politics or the arrest rather than an AI danger (e.g. an anti-AI protester being jailed; a lawmaker denying staff used AI). KEEP when the same subject instead illustrates a real AI danger or a group responding to a concrete AI threat: performers striking over voice-cloning; a law BANNING AI worker-replacement (illustrates the job-loss danger); a company pausing a feature over deepfake risk (illustrates that risk).
- Abstract regulatory / trend meta-commentary with no concrete danger or event ("frameworks may become mandatory").
- Corporate PR, funding, or product-strategy puffery with no danger and no harm. (But a lab's safety research that DESCRIBES a real AI danger — e.g. models scheming or deceiving — IS in scope as expert-analysis.)
- A generic cybersecurity vulnerability/breach with NO AI role (AI merely name-dropped).
- AI that is INCIDENTAL — merely mentioned or present with no role in the harm. But KEEP when AI is actively USED as a tool to plan, generate, or carry out the harm (an AI-generated manifesto for a planned attack; using a chatbot to rehearse a killing) — that illustrates a real danger. Bare correlation ("happened to use ChatGPT") is not enough; instrumental use or a causal role IS.

COHERENCE — if the Title and the Body describe DIFFERENT events (the headline is about one topic and the body about another), set kind="none" and say so in reason. We cannot publish a card whose title and content disagree."""

def groq_judge(post):
    """Stage B — classify + grade. Returns (verdict_dict_or_None, tokens, status).
    NO summary here: Stage C (write_summary) writes the card copy for keepers only,
    so the judge's scarce budget isn't spent writing prose for rejected items."""
    # Social clips (IG/TikTok/YouTube) reach us as a short indexed caption — the STORY is the
    # video, which the judge can't see. Tell it so, so it doesn't reject on thinness alone; the
    # scope/coherence bar still applies fully.
    clip_note = ""
    if is_social_clip(post.get("external_url", "") or ""):
        clip_note = ("\n\nNOTE: This item is a short social-video clip (Instagram / TikTok / "
                     "YouTube). The text below is only a brief caption the platform indexed — the "
                     "actual content is the VIDEO, which you cannot see. Judge it on the topic and "
                     "whether it points at a real AI risk/harm; do NOT force kind=\"none\" merely "
                     "because the caption is short or thin. Apply the same SCOPE and COHERENCE "
                     "tests as every other item.")
    prompt = f"""You are the editorial judge for "AI Harm Watch", a public site documenting REAL stories of people/communities negatively affected by AI, plus real AI incidents and credible expert reporting on AI harms. Audience: the general public.

Judge the content item below in two steps — it may be a Reddit post, a news article, a YouTube video, a Google-Alert hit, or an incident-database entry. Return STRICT JSON only, no prose.

{JUDGE_RUBRIC}{clip_note}

Return:
{{
  "kind": "human-impact" | "incident" | "expert-analysis" | "none",
  "grounded": true/false,   // does it point at a SPECIFIC real person/org/event/finding (not abstraction)?
  "theme": one of {THEMES} or "none",
  "relevance": 0-100,       // how strongly it shows real AI harm/disruption to a general reader
  "sensitivity": "Safe" | "Sensitive",   // Sensitive if minors, sexual content, suicide/self-harm, or graphic harm
  "reason": "one short line: why kept or rejected"
}}

{_origin_line(post)}
Title: {post['title']}
Body: {post['selftext'] or '(no body text)'}"""
    content, tokens, status = groq_chat(JUDGE_MODEL, prompt)
    if content is None:
        return None, tokens, status
    try:
        return json.loads(content), tokens, "ok"
    except Exception as e:
        print(f"  ! judge JSON parse error: {e}")
        return None, tokens, "error"

def groq_judge_batch(batch):
    """Stage B, BATCHED — judge up to JUDGE_BATCH items in ONE call sharing a single copy of
    JUDGE_RUBRIC (the ~1,090-token rubric travels once, not once per item ⇒ ~58% fewer judge
    tokens). Same calibrated rules as the single judge; only the I/O envelope differs.
    Returns (verdicts, tokens, status): `verdicts` is a list aligned to `batch`, each a verdict
    dict or None (an item the model omitted / unparseable); status is 'ok' | 'rate_limited' |
    'error'. Callers single-re-judge any None and fall back to the single path on a whole-batch
    failure, so batching can never silently lose an item."""
    lines, any_clip = [], False
    for i, p in enumerate(batch):
        clip = is_social_clip(p.get("external_url", "") or "")
        any_clip = any_clip or clip
        mark = " [social-video clip]" if clip else ""
        body = (p.get("selftext") or "")[:1500] or "(no body text)"
        lines.append(f'[{i}]{mark} {_origin_line(p)} | Title: {p["title"]} | Body: {body}')
    clip_hint = ("\nItems marked [social-video clip] are a short indexed caption over a video you "
                 "cannot see (Instagram / TikTok / YouTube): judge those on topic + the SCOPE/"
                 "COHERENCE tests, do NOT force kind=\"none\" merely because the caption is thin.\n"
                 if any_clip else "")
    prompt = f"""You are the editorial judge for "AI Harm Watch", a public site documenting REAL stories of people/communities negatively affected by AI, plus real AI incidents and credible expert reporting on AI harms. Audience: the general public.

Judge EACH numbered item below independently in two steps — an item may be a Reddit post, a news article, a YouTube video, a Google-Alert hit, or an incident-database entry. Return STRICT JSON only, no prose.

{JUDGE_RUBRIC}
{clip_hint}
Return one object PER ITEM, matching each item's [i] number:
{{"results": [
  {{"i": <the item's number>,
    "kind": "human-impact" | "incident" | "expert-analysis" | "none",
    "grounded": true/false,
    "theme": one of {THEMES} or "none",
    "relevance": 0-100,
    "sensitivity": "Safe" | "Sensitive",
    "reason": "one short line: why kept or rejected"}}
]}}
Judge EVERY item; return exactly one object per item.

ITEMS:
{chr(10).join(lines)}"""
    content, tokens, status = groq_chat(JUDGE_MODEL, prompt)
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
        print(f"  ! judge batch JSON parse error: {e}")
        return [None] * len(batch), tokens, "error"

def groq_triage(post):
    """Back-compat shim for regrade.py (which imports groq_triage). Delegates to the
    Stage-B judge and drops the status, returning the old (verdict, tokens) shape."""
    v, tok, _status = groq_judge(post)
    return v, tok

# --- Stage A: cheap recall-biased pre-triage (gpt-oss-20b, batched) ----------
def pretriage(posts, deadline):
    """Drop OBVIOUS junk before the expensive judge, in batches on the 20b bucket.
    Recall-biased: DROP only clear non-stories (promo/meme/how-to/off-topic/non-English);
    when unsure, KEEP. FAIL-OPEN — any batch that errors, times out, or returns malformed
    output passes through untouched, so a broken pre-filter can only waste judge budget,
    never silently lose a real story. Posts beyond MAX_TRIAGE skip pre-triage entirely."""
    if not posts:
        return posts, 0
    pool, rest = posts[:MAX_TRIAGE], posts[MAX_TRIAGE:]
    survivors, dropped, batches, spent = [], 0, 0, 0
    for i in range(0, len(pool), TRIAGE_BATCH):
        if time.time() > deadline:                 # out of time → keep everything left (fail-open)
            survivors.extend(pool[i:])
            break
        batch = pool[i:i + TRIAGE_BATCH]
        items = "\n".join(
            f'{j+1}. {p["title"][:150]} — {(p.get("selftext") or "")[:TRIAGE_SNIPPET]}'
            for j, p in enumerate(batch))
        prompt = ("You are a fast pre-filter for a site documenting REAL stories of AI harm "
                  "(real people harmed by AI, real AI incidents, credible reporting on AI harm). "
                  "For each numbered item, decide KEEP or DROP. DROP only if it is OBVIOUSLY not "
                  "such a story: product/marketing promo, meme/joke, generic how-to or listicle, "
                  "an unrelated topic, or not in English. When unsure, KEEP (a stricter judge "
                  'runs next). Return JSON {"results":[{"n":1,"v":"KEEP"},...]} covering every '
                  "item.\n\n" + items)
        content, tok, _status = groq_chat(TRIAGE_MODEL, prompt)
        spent += tok
        batches += 1
        keep_idx = None
        if content:
            try:
                data = json.loads(content)
                verds = {int(r["n"]): str(r.get("v", "KEEP")).upper()
                         for r in data.get("results", []) if "n" in r}
                # recall-biased: an item is dropped only on an explicit DROP; missing → KEEP
                keep_idx = {n for n in range(1, len(batch) + 1) if verds.get(n, "KEEP") != "DROP"}
            except Exception:
                keep_idx = None
        if keep_idx is None:                        # fail-open: keep the whole batch
            survivors.extend(batch)
        else:
            for j, p in enumerate(batch):
                if (j + 1) in keep_idx:
                    survivors.append(p)
                else:
                    dropped += 1
        time.sleep(TRIAGE_SLEEP)
    survivors.extend(rest)
    print(f"  Stage-A pre-triage ({TRIAGE_MODEL}): kept {len(survivors)}/{len(posts)} "
          f"({dropped} obvious-junk dropped across {batches} batches"
          f"{f'; {len(rest)} past MAX_TRIAGE passed through' if rest else ''}).")
    return survivors, spent

# --- Stage C: card copy for keepers only (qwen3.6-27b) -----------------------
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

def _article_text(url):
    """Full article body (og:description + <p> text) for a feed/article URL — gives Stage C a
    real body when the feed snippet is too thin to summarise from (the failure that let refusal
    text reach live cards). Best-effort: returns '' on any error so the caller keeps the snippet."""
    try:
        raw = http_text(url, headers={"User-Agent": BROWSER_UA})
    except Exception:
        return ""
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    m = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\']'
                  r'[^>]+content=["\']([^"\']+)', raw, re.I)
    desc = html.unescape(m.group(1)) if m else ""
    paras = re.findall(r"(?is)<p[^>]*>(.*?)</p>", raw)
    text = " ".join(html.unescape(re.sub(r"(?s)<[^>]+>", " ", p)) for p in paras)
    return re.sub(r"\s+", " ", (desc + " " + text).strip())

def copy_prompt(post, body):
    """The Stage-C card-copy prompt — the SINGLE source of truth for card voice, shared by
    the live harvester (write_summary) and the one-off bulk rewrite (rewrite_summaries.py) so
    back-filled cards read identically to newly-harvested ones. Voice: plain-language, for a
    reader NEW to AI; lead with the harm/risk to people and the concrete 'so what'; preserve
    source specifics verbatim (dates/numbers/names) and drop reader-irrelevant metadata."""
    return ("Write a plain-language summary (max 60 words) of the item below, for a "
            "general-public card on a site about the dangers and risks of AI. Your reader is "
            "new to AI — assume no technical background and use no jargon. Say plainly what "
            "happened AND why it matters: who was harmed or put at risk, and the real-world "
            "consequence (the 'so what'). Be concrete and specific (who / what / when). "
            "Preserve every specific detail — dates, numbers, names, places — exactly as the "
            "source gives them; never guess, round, infer, or shift them. "
            "Convert any relative time ('last week', 'yesterday', 'today') to the actual "
            "calendar date — never leave relative time on the card. "
            "Omit source metadata a general reader doesn't need — author/byline lists, "
            "submission timestamps, outlet boilerplate — unless it is itself the story. "
            "Factual, never alarmist or speculative — no hype, no opinion, no preamble. "
            "Return only the summary.\n\n"
            f"{_origin_line(post)}\nTitle: {post['title']}\nBody: {body[:1500] or '(no body text)'}")

def write_summary(post):
    """Stage C — write the <=60-word card summary for a KEPT item on the qwen bucket.
    Returns (summary, tokens). Never blank: on any failure, falls back to a trimmed
    body/title so the card always has text."""
    body = (post.get("selftext") or "").strip()
    # A thin feed snippet is what made qwen refuse and leak meta-text onto live cards. When the
    # body is too short, fetch the real article body first (best-effort, skipped for social clips
    # whose "body" is the video itself). enrich_media re-opens the page later for media only.
    if len(body) < COPY_MIN_BODY:
        url = article_url(post)
        if url.lower().startswith("http") and not is_social_clip(url):
            full = _article_text(url)
            if len(full) > len(body):
                body = full
    prompt = copy_prompt(post, body)
    # reasoning_effort='none' → qwen answers directly (~87 tok) instead of thinking (~950 tok).
    content, tokens, status = groq_chat(COPY_MODEL, prompt, json_mode=False, reasoning_effort="none")
    new = (content or "").strip().strip('"').strip()
    # Store the summary only if it's a real one — never a refusal/meta-comment or an error echo.
    if status == "ok" and new and not looks_like_meta(new):
        return new, tokens
    return (body[:200] or post["title"])[:200], tokens   # fail-safe: never a blank/refusal card

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
        # bar — never lowers MIN_RELEVANCE, just guarantees at most one clip a theme isn't buried.
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
    for i in range(0, len(records), 10):
        chunk = records[i:i+10]
        body = json.dumps({"records": chunk, "typecast": True}).encode()
        http_json(f"https://api.airtable.com/v0/{BASE}/{TABLE}",
                  headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}",
                           "Content-Type": "application/json"},
                  data=body, method="POST")

# --- main -------------------------------------------------------------------
def judge_passes(t):
    return (t.get("kind") in ("human-impact", "incident", "expert-analysis")
            and t.get("grounded") and t.get("relevance", 0) >= MIN_RELEVANCE)

def build_fields(p, t, summary):
    theme = t.get("theme") if t.get("theme") in THEMES else None
    fmt, media = detect_format_and_media(p)
    fmt, media = enrich_media(p, fmt, media)   # open the page for a heading image / embedded video
    sens = "Sensitive" if (t.get("sensitivity") == "Sensitive" or p["over_18"]) else "Safe"
    note = f"Auto-harvested from {p.get('source_label') or 'feed'}. Kind: {t.get('kind')}. {t.get('reason', '')}"[:900]
    fields = {
        "Title": clean_title(p["title"])[:250], "Status": "Candidate", "Summary": summary,
        "Source URL": story_source_url(p), "Media URL": media, "Format": fmt,
        "Relevance": int(t.get("relevance", 0)), "Sensitivity": sens,
        "Source type": p.get("provenance", "Manual"), "Editor notes": note,
        "Source name": (p.get("source_label") or "")[:250],
    }
    if theme:
        fields["Theme"] = theme
    if p["created"]:
        fields["Date"] = p["created"]
    return fields

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
             f"&fields%5B%5D={urllib.parse.quote(key_field)}&fields%5B%5D=Found%20daily")
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
                 today_s, cut_short, cut_long):
    """Compute the six yield columns + accumulated 'Found daily' for each row (keyed by
    key_field) and PATCH them. Returns rows written, or -1 if the yield fields are absent
    (422 → skip, don't raise). 'Found' is accumulated into the history string; 'Kept' is
    the exact rolling count passed in."""
    updates = []
    for r in rows:
        key = (r.get("fields", {}).get(key_field) or "").strip()
        if not key:
            continue
        hist = _parse_found_daily(r.get("fields", {}).get("Found daily", ""))
        hist[today_s] = hist.get(today_s, 0) + int(found_now.get(key, 0))
        hist = {d: c for d, c in hist.items() if d > cut_long}       # trim to long window
        updates.append({"id": r["id"], "fields": {
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

def source_concept_map():
    """{Source Name: Concept} for sources that carry a Concept (the 55 phrase-locked alerts).
    Lets per-source yield roll up to the Coverage Map. Blank-Concept sources (subreddits,
    feeds, YouTube, social) map to nothing and simply don't count toward any concept."""
    m, offset = {}, None
    while True:
        u = (f"https://api.airtable.com/v0/{BASE}/{SOURCES_TABLE}?pageSize=100"
             f"&fields%5B%5D=Name&fields%5B%5D=Concept")
        if offset:
            u += f"&offset={offset}"
        data = http_json(u, headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"})
        for r in data.get("records", []):
            f = r.get("fields", {})
            name, concept = (f.get("Name") or "").strip(), (f.get("Concept") or "").strip()
            if name and concept:
                m[name] = concept
        offset = data.get("offset")
        if not offset:
            break
    return m

def _remap(counter, name_to_concept):
    """Re-key a {Source name: n} Counter to {Concept: n} via the source→concept map."""
    out = Counter()
    for name, n in counter.items():
        concept = name_to_concept.get(name)
        if concept:
            out[concept] += n
    return out

def update_yields(found_by_src, kept_by_src):
    """Roll this run's per-source yield onto BOTH the Sources table (keyed by Name, active rows)
    and the Coverage Map (keyed by Concept, via each source's stored Concept), so the owner can
    see which sources — and which targeted concepts — are actually producing. Each target gets
    six columns + a 'Found daily' history:
      • Found/Kept last run — this run's judging-pool contribution + what it landed.
      • Found (10d)/(30d)   — rolling sums. 'Found' can't be recovered from Airtable (rejected
        items aren't stored) so it's ACCUMULATED into 'Found daily' (append today, trim, re-sum)
        → these build up over time rather than being backfillable.
      • Kept (10d)/(30d)    — exact rolling counts from the Stories table (Rejected excluded).
    Best-effort: never breaks the run; self-heals (logs) if a target's fields are absent."""
    try:
        today = datetime.date.today()
        today_s = today.isoformat()
        cut_long = (today - datetime.timedelta(days=STATS_WINDOW_LONG)).isoformat()
        cut_short = (today - datetime.timedelta(days=STATS_WINDOW_SHORT)).isoformat()
        kept10, kept30 = kept_windows(STATS_WINDOW_SHORT, STATS_WINDOW_LONG)   # per Source name

        # Sources — keyed by Name, active only
        n = _write_yield(SOURCES_TABLE, "Name", _fetch_yield_rows(SOURCES_TABLE, "Name", True),
                         found_by_src, kept_by_src, kept10, kept30, today_s, cut_short, cut_long)
        print("  ! Source-yield fields missing — skipped." if n < 0
              else f"  Per-source yield → {n} active Sources.")

        # Coverage Map — remap every counter from source→concept, key by Concept
        cmap = source_concept_map()
        n = _write_yield(CMAP_TABLE, "Concept", _fetch_yield_rows(CMAP_TABLE, "Concept", False),
                         _remap(found_by_src, cmap), _remap(kept_by_src, cmap),
                         _remap(kept10, cmap), _remap(kept30, cmap), today_s, cut_short, cut_long)
        print("  ! Coverage-Map yield fields missing — skipped." if n < 0
              else f"  Per-concept yield → {n} Coverage Map rows.")
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
    }
    # Token fields may not exist in the base yet (added via Airtable UI or metadata API later).
    # A 422 UNKNOWN_FIELD_NAME names one absent field at a time; pop the named one and retry,
    # so the run row still logs with whatever token columns DO exist.
    OPTIONAL = ("Tokens", "Tokens A (triage)", "Tokens B (judge)", "Tokens C (copy)", "Judge mode")
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
    if dry:
        n_in = sum(1 for p in posts if dedup_keys(p) & known)
        print(f"  {n_in} of these are already in Airtable (shown as [in-base]).")
    else:
        posts = [p for p in posts if not (dedup_keys(p) & known)]
        print(f"  {len(posts)} after removing ones already in Airtable.")

    # AI-relevance gate for YouTube whole-channel feeds (see ai_relevant). Other
    # sources bypass it. Applied before shuffle so filtered posts never cost a judge call.
    yt_before = sum(1 for p in posts if p.get("provenance") == "YouTube")
    if yt_before:
        gate = load_gate_matchers()
        posts = [p for p in posts if p.get("provenance") != "YouTube" or ai_relevant(p, gate)]
        yt_after = sum(1 for p in posts if p.get("provenance") == "YouTube")
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
    posts, tok_triage = pretriage(posts, deadline)

    kept, seen_titles, judged, errors, n_keep, n_rej = [], set(), 0, 0, 0, 0
    consec_err, consec_rl = 0, 0             # genuine-error streak vs rate-limit streak (tracked apart)
    # Per-model token metering: each stage runs on its OWN 200K/day bucket, so track them apart
    # (a single lumped number can't tell you which bucket is nearing its cap).
    tok_judge, tok_copy = 0, 0
    # Judge EVERY survivor, collecting all that pass the bar; SELECTION happens after, so we
    # pick the best-per-theme rather than the first-per-theme. (Copy is written only for the
    # final picks, further down — never for an item that a higher-scoring rival displaces.)
    passed = []          # [{"p":post, "t":verdict, "rel":int, "theme":str}, …]
    # Pre-dedup: drop same-title cross-posts BEFORE batching, so batch slots aren't spent on dupes
    # and the [i] indices stay clean.
    to_judge = []
    for p in posts:
        norm = norm_title(p["title"])        # outlet-stripped, so cross-outlet reprints collide
        if norm in seen_titles:
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
            passes = judge_passes(t)
            if dry:
                n_keep += passes; n_rej += (not passes)
                verd = "KEEP  " if passes else "reject"
                print(f"  {verd} [{int(t.get('relevance',0)):>3}] {t.get('kind','?'):<15}{tag} "
                      f"{p['title'][:50]} :: {t.get('reason','')[:55]}")
            if not passes:
                continue
            theme = t.get("theme") if t.get("theme") in THEMES else "(none)"
            passed.append({"p": p, "t": t, "rel": int(t.get("relevance", 0)), "theme": theme})
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
        print(f"\nDRY RUN complete — {n_keep} passed the bar, {n_rej} rejected, {judged} judged, "
              f"{len(selected)} would be kept (best {MAX_PER_THEME}/theme). Nothing written.")
        return

    # Cross-run reprint corpus: recent still-standing rows to compare new copy against.
    dedup_sigs = existing_signatures()
    n_reprint = 0
    # Stage C — write the card copy (qwen bucket) for the FINAL selected keepers only.
    for it in selected:
        summary, ctok = write_summary(it["p"])
        tok_copy += ctok
        time.sleep(COPY_SLEEP)
        # Cross-run near-dup: same event reprinted at a different URL (needs BOTH title AND
        # summary near-identical, so a developing story with new facts is kept).
        dup, match = is_reprint(it["p"]["title"], summary, dedup_sigs)
        if dup:
            n_reprint += 1
            print(f"  ⊘ reprint (skipped) — {it['p']['title'][:50]} ≈ {match[:40]}")
            continue
        fields = build_fields(it["p"], it["t"], summary)
        dedup_sigs.append((norm_title(it["p"]["title"]), (summary or "").lower().strip()))  # catch same-run reprints too
        kept.append({"fields": fields})
        print(f"  + [{it['rel']}] {it['t'].get('kind')}/{it['theme']} — {it['p']['title'][:60]}")
    if n_reprint:
        print(f"  Cross-run dedup: {n_reprint} reprint(s) of existing stories skipped.")
    _tok_line()
    total_tokens = tok_triage + tok_judge + tok_copy

    if kept:
        create_records(kept)
    # Per-source yield → Sources table. "Found" = this source's share of the judging pool it
    # supplied (snapshotted above, BEFORE pre-triage); "Kept" = how many it landed. Lets the
    # owner see which alerts/feeds/subs earn their place vs which are noise. (kept carries each
    # row's "Source name".)
    kept_by_src = Counter((k["fields"].get("Source name") or "") for k in kept)
    update_yields(found_by_src, kept_by_src)
    write_run({
        "run": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "mode": mode,
        "tried": len(sources), "read": read_ok, "failed_n": len(failed), "failed": failed,
        "fetched": fetched_total, "judged": judged, "kept": len(kept), "errors": errors,
        "tokens": total_tokens,
        "tok_triage": tok_triage, "tok_judge": tok_judge, "tok_copy": tok_copy,
        "judge_mode": f"batch-{JUDGE_BATCH}" if JUDGE_BATCH > 1 else "single",
    })
    print(f"\nDone. Mode={mode}. {read_ok}/{len(sources)} sources read, {judged} judged, "
          f"{errors} errors, {total_tokens} Groq tokens, wrote {len(kept)} new Candidate rows. Logged to Runs table.")

if __name__ == "__main__":
    main()
