"""Stamp the shared site frame (head, header, footer) into every page under site/.

The site has no build step, so the frame is written once in scripts/frame/ and copied into each
page by this script. Each page marks where the pieces go with comment pairs:

  <!-- frame:head -->   ... <!-- /frame:head -->
  <!-- frame:header --> ... <!-- /frame:header -->
  <!-- frame:footer --> ... <!-- /frame:footer -->

Use "frame:footer-nosignup" instead on pages that carry the main sign-up themselves (one sign-up
per page). Links to the page itself get aria-current="page", and a menu group containing the
current page is marked too.

Edit the pieces in scripts/frame/, then run:   python scripts/build_frame.py
Check nothing is out of date (e.g. before a PR): python scripts/build_frame.py --check
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
FRAME = ROOT / "scripts" / "frame"

SIGNUP = re.compile(r"[ \t]*<!-- signup -->.*?<!-- /signup -->\n", re.S)
GROUP = re.compile(r'<button class="nav-group-btn"(.*?)</ul>', re.S)
SLOT = re.compile(r"(<!-- frame:(head|header|footer|footer-nosignup) -->\n)(.*?)([ \t]*<!-- /frame:\2 -->)", re.S)


def piece(name):
    return (FRAME / f"{name}.html").read_text(encoding="utf-8").replace("\r\n", "\n")


def page_path(f):
    rel = f.relative_to(SITE).parent.as_posix()
    return "/" if rel == "." else f"/{rel}/"


def mark_current(html, path):
    html = html.replace(f'href="{path}"', f'href="{path}" aria-current="page"')
    return GROUP.sub(
        lambda m: m.group(0).replace('class="nav-group-btn"', 'class="nav-group-btn is-current"', 1)
        if "aria-current" in m.group(1) else m.group(0),
        html,
    )


def stamp(text, path):
    pieces = {
        "head": piece("head"),
        "header": mark_current(piece("header"), path),
        "footer": mark_current(piece("footer"), path),
    }
    pieces["footer-nosignup"] = SIGNUP.sub("", pieces["footer"])
    return SLOT.sub(lambda m: m.group(1) + pieces[m.group(2)] + m.group(4), text)


def main():
    check = "--check" in sys.argv
    stale = []
    for f in sorted(SITE.rglob("*.html")):
        raw = f.read_bytes().decode("utf-8")
        crlf = "\r\n" in raw
        text = raw.replace("\r\n", "\n")
        if "<!-- frame:" not in text:
            continue
        new = stamp(text, page_path(f))
        if crlf:
            new = new.replace("\n", "\r\n")
        if new != raw:
            stale.append(f.relative_to(ROOT).as_posix())
            if not check:
                f.write_bytes(new.encode("utf-8"))
    verb = "Out of date" if check else "Updated"
    print(f"{verb}: {', '.join(stale)}" if stale else "All pages up to date.")
    sys.exit(1 if check and stale else 0)


if __name__ == "__main__":
    main()
