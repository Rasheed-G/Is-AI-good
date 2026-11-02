"""Better source (v3, rulebook section 3): a strong story held back ONLY by its outlet is looked for at an
approved outlet, and swapped automatically when the same event is found there (owner, 2026-11-02: no manual
check). The old link is kept on the story so a swap can be undone.

Steps, per story: search Google News for the headline's key words (last 14 days) -> keep results whose outlet is
approved -> the cheap model checks "same event?" -> decode Google's link to the real article -> read it -> swap.
Google's links are encoded; decoding is best-effort and UNTESTED at scale (the pipeline test checks 50 links).
Any failure leaves the story as it was (still Weak, so it doesn't go live).
"""
import re, json, time, base64, html, urllib.parse
import xml.etree.ElementTree as ET
import events, outlets, rulebook

MAX_SEARCHES = 15                 # stories searched per day (bounds time and Google requests)
WINDOW_DAYS = rulebook.setting_int("Better source window")
GN_SEARCH = "https://news.google.com/rss/search?q={q}&hl=en-GB&gl=GB&ceid=GB:en"
GN_EXECUTE = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def eligible(verdict, judge_verdict):
    """True when the judge said Weak and the story would pass with a trusted outlet's credibility."""
    if not verdict or judge_verdict(verdict) != "Weak":
        return False
    trial = json.loads(json.dumps(verdict))
    trial.setdefault("scores", {})["credibility"] = outlets.TRUSTED_CREDIBILITY
    return judge_verdict(trial) == "Keep"


def query(title):
    title = re.split(r"\s+[|–—-]\s+", title or "")[0]          # drop a trailing " - Outlet"
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'’$£€.-]*", title)
             if w.lower() not in events.STOP and (len(w) >= 4 or w[0].isdigit() or w.isupper())]
    return " ".join(words[:8])


def search(title, get_text):
    """Google News results [(title, link, outlet_url, description)] for the headline's key words."""
    q = query(title)
    if not q:
        return []
    url = GN_SEARCH.format(q=urllib.parse.quote(f"{q} when:{WINDOW_DAYS}d"))
    try:
        root = ET.fromstring(get_text(url))
    except Exception:
        return []
    out = []
    for it in root.iter("item"):
        src = it.find("source")
        desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
        out.append(((it.findtext("title") or "").strip(), (it.findtext("link") or "").strip(),
                    src.get("url", "") if src is not None else "", " ".join(desc.split())))
    return out


def _article_id(link):
    m = re.search(r"news\.google\.com/(?:rss/)?articles/([^?/]+)", link or "")
    return m.group(1) if m else ""


def decode(link, get_text, post_form):
    """The real article address behind a news.google.com link, or "" when it can't be decoded.
    Old-style ids carry the address inside them; new ones need Google's own lookup (signature + timestamp)."""
    aid = _article_id(link)
    if not aid:
        return link if link.startswith("http") and "news.google.com" not in link else ""
    try:                                              # old style: the address is inside the id
        raw = base64.urlsafe_b64decode(aid + "=" * (-len(aid) % 4))
        m = re.search(rb"https?://[\x21-\x7e]+", raw)
        if m and b"news.google.com" not in m.group(0):
            return m.group(0).decode("ascii", "ignore").rstrip("\x01\xd2")
    except Exception:
        pass
    try:                                              # new style: ask Google with the page's signature
        page = get_text(f"https://news.google.com/rss/articles/{aid}")
        sg = re.search(r'data-n-a-sg="([^"]+)"', page)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page)
        if not (sg and ts):
            return ""
        inner = json.dumps(["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None,
                                            None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                            aid, int(ts.group(1)), sg.group(1)])
        body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))
        resp = post_form(GN_EXECUTE, body)
        data = json.loads(resp.split("\n\n", 1)[1])
        url = json.loads(data[0][2])[1]
        return url if isinstance(url, str) and url.startswith("http") else ""
    except Exception:
        return ""


def find(item, chat, get_text, post_form, read_page):
    """Look for one story at an approved outlet. Returns (new_url, article_text, tokens) or ("", "", tokens)."""
    p = item["p"]
    here = outlets.host(p.get("external_url") or p.get("permalink"))
    results = [r for r in search(p["title"], get_text)
               if outlets.approved(r[2]) and outlets.host(r[2]) != here][:3]
    if not results:
        return "", "", 0
    docs = [events.doc(p["title"], p.get("selftext"), p.get("external_url"), "new")]
    docs += [events.doc(t, d, link, "queue") for t, link, _src, d in results]
    pairs = [(0, k, 0) for k in range(1, len(docs))]
    same, tok = events.ask_same(pairs, docs, chat, sleep=0)
    for k in range(1, len(docs)):
        if not same.get((0, k)):
            continue
        url = decode(docs[k]["url"], get_text, post_form)
        if not url or not outlets.approved(url):
            continue
        text, _date, _lang = read_page(url)
        return url, text, tok
    return "", "", tok


def improve(weak_items, judge_verdict, chat, get_text, post_form, read_page, deadline):
    """Swap the eligible Weak stories to an approved outlet where found. Each item is {"p", "t"}; a swapped
    item's post gets the new link (the old one in p["swapped_from"]) and its verdict the trusted credibility.
    Returns (swapped_items, searched, tokens)."""
    swapped, searched, spent = [], 0, 0
    for it in weak_items:
        if searched >= MAX_SEARCHES or time.time() > deadline:
            break
        if not eligible(it["t"], judge_verdict):
            continue
        searched += 1
        url, text, tok = find(it, chat, get_text, post_form, read_page)
        spent += tok
        time.sleep(2)
        if not url:
            continue
        p = it["p"]
        p["swapped_from"] = p.get("external_url") or p.get("permalink")
        p["external_url"] = p["permalink"] = url
        if len(text) >= 200:
            p["selftext"] = text
        outlets.trust(it["t"], url)
        it["t"]["reason"] = f"{it['t'].get('reason', '')} Better source found: {outlets.host(url)}.".strip()
        swapped.append(it)
        print(f"  ↻ better source: {p['title'][:50]} -> {outlets.host(url)}")
    return swapped, searched, spent
