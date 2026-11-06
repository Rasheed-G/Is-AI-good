"""The approved outlet list (approved_outlets.txt, owner approved): a story from one of these counts as a
trusted source. The credibility score it gets is a rulebook setting (section 8, "Approved outlet credibility")."""
import pathlib, urllib.parse
import rulebook

PATH = pathlib.Path(__file__).resolve().parent.parent / "approved_outlets.txt"


def _load():
    exact, suffixes, paywall = set(), [], set()
    for line in PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip().lower()
        if not line or line.startswith("#"):
            continue
        words = line.split()
        line = words[0]
        if line.startswith("suffix:"):
            suffixes.append(line[7:].strip())
        else:
            exact.add(line)
            if "paywall" in words[1:]:
                paywall.add(line)
    return exact, tuple(suffixes), paywall


EXACT, SUFFIXES, PAYWALL = _load()
TRUSTED_CREDIBILITY = rulebook.setting_int("Approved outlet credibility")


def host(url):
    h = urllib.parse.urlparse(url or "").netloc.lower().split(":")[0]
    return h.removeprefix("www.").removeprefix("m.")


def approved(url):
    """True when the link's web address is on the list. Sub-addresses count (edition.cnn.com -> cnn.com)."""
    h = host(url)
    if not h:
        return False
    if SUFFIXES and h.endswith(SUFFIXES):
        return True
    parts = h.split(".")
    return any(".".join(parts[i:]) in EXACT for i in range(len(parts) - 1))


def paywalled(url):
    """True when the link is at an approved outlet marked "paywall" (readers mostly can't read it free)."""
    parts = host(url).split(".")
    return any(".".join(parts[i:]) in PAYWALL for i in range(len(parts) - 1))


def preference(url):
    """Which link readers are best sent to: a free approved outlet (2), a paywalled approved one (1), other (0)."""
    return 2 if approved(url) and not paywalled(url) else 1 if approved(url) else 0


def trust(verdict, url):
    """After the judge: a story from an approved outlet gets the trusted credibility score (never lowered).
    Returns True when the score was raised, so the run can count it."""
    if not verdict or not approved(url):
        return False
    scores = verdict.setdefault("scores", {})
    try:
        cred = int(float(scores.get("credibility", 0) or 0))
    except (TypeError, ValueError):
        cred = 0
    if cred >= TRUSTED_CREDIBILITY:
        return False
    scores["credibility"] = TRUSTED_CREDIBILITY
    return True
