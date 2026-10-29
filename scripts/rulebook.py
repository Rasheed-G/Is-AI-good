"""Read the editorial rulebook (rulebook.md at the repo root).

The rulebook is the one home for the rules: themes, story criteria, gallery rules, card copy and settings.
Code reads it from here instead of keeping its own copy, so a rule changed there changes everywhere.

    import rulebook
    rulebook.version()            # "1"
    rulebook.theme_names()        # ["Jobs & livelihoods", ...] in rulebook order
    rulebook.themes()             # [{"name", "slug", "summary", "in", "out"}, ...]
    rulebook.section(7)           # the text of "## 7. Card copy", heading excluded
    rulebook.setting("Gallery cap")   # "20"
"""
import functools
import pathlib
import re

PATH = pathlib.Path(__file__).resolve().parent.parent / "rulebook.md"


@functools.lru_cache(maxsize=1)
def text():
    return PATH.read_text(encoding="utf-8")


def version():
    m = re.search(r"\*\*Version (\d+)\*\*", text())
    if not m:
        raise ValueError("rulebook.md has no '**Version N**' line")
    return m.group(1)


def section(n):
    """The body of '## n. Title', up to the next '## ' heading (the '---' rule is trimmed)."""
    m = re.search(rf"^## {n}\. .*?$\n(.*?)(?=^## |\Z)", text(), flags=re.M | re.S)
    if not m:
        raise ValueError(f"rulebook.md has no section {n}")
    return m.group(1).strip().removesuffix("---").strip()


def _plain(cell):
    return cell.replace("**", "").strip()


def slug(name):
    """Theme name as it appears in a site address: "Jobs & livelihoods" -> "jobs-livelihoods"."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _table_rows(body, first_header):
    """Rows (lists of cells) of the markdown table in `body` whose first header cell is `first_header`."""
    rows, inside = [], False
    for line in body.splitlines():
        if not line.startswith("|"):
            if inside:
                break
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not inside:
            inside = _plain(cells[0]) == first_header
            continue
        if set(cells[0]) <= set("-: "):
            continue
        rows.append(cells)
    return rows


@functools.lru_cache(maxsize=1)
def themes():
    out = []
    for cells in _table_rows(section(2), "#"):
        _, name, summary, inside, outside = cells[:5]
        name = _plain(name)
        out.append({"name": name, "slug": slug(name), "summary": _plain(summary),
                    "in": _plain(inside), "out": _plain(outside)})
    if not out:
        raise ValueError("rulebook.md section 2 has no themes table")
    return tuple(out)


def theme_names():
    return [t["name"] for t in themes()]


def setting(dial):
    """The Value cell of a row in the section 8 settings table, matched on the dial's name (⚙ ignored)."""
    for cells in _table_rows(section(8), "Dial"):
        if _plain(cells[0]).replace("⚙", "").strip().lower() == dial.lower():
            return _plain(cells[2])
    raise KeyError(f"rulebook.md section 8 has no setting '{dial}'")


def setting_int(dial):
    """The first whole number in a setting's value: "6 months (exemptions...)" -> 6."""
    m = re.search(r"\d[\d,]*", setting(dial))
    if not m:
        raise ValueError(f"setting '{dial}' has no number")
    return int(m.group(0).replace(",", ""))
