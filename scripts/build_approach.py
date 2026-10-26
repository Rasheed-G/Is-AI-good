"""Build the theory of change page (site/approach/index.html) from the theory of change document.

The page uses the whole document word for word. This script only changes how it is presented:
the text diagram becomes a clickable journey map, each gate opens on click, source numbers like [15]
link to Appendix A, the deeper sections (8, 9 and the sources) open on click, and two small
pictures are added (the 70% funnel and the "how sure" strip).

The document lives in the private docs repo. When it changes, run:
  python scripts/build_approach.py "<path to THEORY-OF-CHANGE-v4.md>" --updated 2026-10-22
  python scripts/build_frame.py
Needs markdown-it-py (pip install markdown-it-py).
"""
import datetime
import html
import pathlib
import re
import sys

from markdown_it import MarkdownIt

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "site" / "approach" / "index.html"
DEFAULT_SRC = ROOT.parent / "AI Harm Watch" / "docs" / "THEORY-OF-CHANGE-v4.md"
VERSION = "4"

md = MarkdownIt("commonmark", {"html": False}).enable("table")


def inline(text):
    return md.renderInline(text)


def render(text):
    out = md.render(text)
    out = re.sub(r"<p>━+\s*(.*?)\s*━*</p>", r'<p class="work-stops">\1</p>', out, flags=re.S)
    out = out.replace("<table>", '<div class="table-wrap" tabindex="0"><table>').replace("</table>", "</table></div>")
    out = out.replace("<hr />\n", "")
    return out


def link_refs(out):
    # [15] -> link to source 15 (only in text, never inside a tag)
    parts = re.split(r"(<[^>]+>)", out)
    for i, p in enumerate(parts):
        if not p.startswith("<"):
            parts[i] = re.sub(r"\[(\d{1,3})\]", r'<a class="cite" href="#source-\1">[\1]</a>', p)
    return "".join(parts)


def private_links(out):
    # Links to our private notes can't open for readers: keep the words, drop the link
    return re.sub(r'<a href="(?!https?:)[^"]*">(.*?)</a>', r"\1", out)


def slug_section(title):
    m = re.match(r"(\d+)\.(\d+)?", title)
    if m:
        return "s" + m.group(1) + (f"-{m.group(2)}" if m.group(2) else "")
    return "appendix-a" if title.lower().startswith("appendix a") else re.sub(r"\W+", "-", title.lower()).strip("-")


def split(text, level):
    """Split markdown into (heading, body) chunks at headings of this level."""
    mark = "#" * level + " "
    chunks, head, body = [], None, []
    for line in text.split("\n"):
        if line.startswith(mark):
            chunks.append((head, "\n".join(body)))
            head, body = line[len(mark):].strip(), []
        else:
            body.append(line)
    chunks.append((head, "\n".join(body)))
    return chunks


# ---------- pictures ----------

def journey_map(mermaid):
    labels = dict(re.findall(r'(\w+)[\[{(]+"([^"]+)"', mermaid))
    groups = dict(re.findall(r'subgraph (\w+)\["([^"]+)"\]', mermaid))
    stop = re.search(r'--\s*"([^"]+)"\s*-->', mermaid).group(1)
    gates = "\n".join(
        f'          <li class="map-gate"><a class="map-gate-btn" href="#gate-{n}" data-gate="{n}" aria-expanded="false">'
        f'<span class="map-num">{n}</span><span>{html.escape(re.sub(r"^\d+\.\s*", "", labels[f"G{n}"]))}</span></a></li>'
        for n in range(1, 9)
    )
    routes = "".join(f"<li>{html.escape(r.strip())}</li>" for r in labels["R"].split("·"))
    return f"""<figure class="journey-map" id="journey-map" aria-labelledby="mapCap">
      <figcaption id="mapCap" class="map-hint">Tap a gate to open it.</figcaption>
      <div class="map-group map-ours">
        <p class="map-group-title">{html.escape(groups["OURS"])}</p>
        <ol class="map-gates">
{gates}
        </ol>
      </div>
      <p class="map-stop"><span>{html.escape(stop)}</span></p>
      <div class="map-group map-others">
        <p class="map-group-title">{html.escape(groups["OTHERS"])}</p>
        <ul class="map-routes">{routes}</ul>
        <p class="map-box">{html.escape(labels["S"])}</p>
        <p class="map-box map-goal">{html.escape(labels["GOAL"])}</p>
      </div>
    </figure>"""


def funnel():
    rows, left = [], 100.0
    rows.append(("Start", left))
    for n in range(1, 9):
        left *= 0.7
        rows.append((f"Gate {n}", left))
    items = "\n".join(
        f'        <li style="--i:{i}"><span class="funnel-label">{label}</span><span class="funnel-track">'
        f'<span class="funnel-bar" style="--w:{v:.1f}%"></span></span><span class="funnel-num">{round(v)}</span></li>'
        for i, (label, v) in enumerate(rows)
    )
    return f"""<figure class="funnel" aria-labelledby="funnelCap">
      <figcaption id="funnelCap">Out of 100 people, how many are left after each gate if every gate keeps 70%</figcaption>
      <ol class="funnel-rows">
{items}
      </ol>
    </figure>"""


LEVELS = ["low", "low–medium", "medium", "medium–high", "high"]


def confidence_strip(sure):
    rows = []
    for n, text in sure:
        level = next((l for l in sorted(LEVELS, key=len, reverse=True) if text.lower().startswith(l)), None)
        score = LEVELS.index(level) + 1 if level else 0
        pips = "".join(f'<span class="pip{" on" if i < score else ""}"></span>' for i in range(5))
        rows.append(
            f'        <li><a href="#gate-{n}" data-gate="{n}">Gate {n}</a>'
            f'<span class="pips" aria-hidden="true">{pips}</span><span class="sure-text">{inline(text)}</span></li>'
        )
    return f"""<figure class="sure-strip" aria-labelledby="sureCap">
      <figcaption id="sureCap">How sure we are, gate by gate</figcaption>
      <ol>
{chr(10).join(rows)}
      </ol>
    </figure>"""


# ---------- sections ----------

def gate_block(head, body):
    n = re.match(r"Gate (\d+)", head).group(1)
    lines = body.strip("\n").split("\n")
    sub = ""
    if lines and re.match(r"^\*[^*].*\*$", lines[0].strip()):
        sub = f'<p class="gate-sub">{inline(lines.pop(0).strip())}</p>'
    inner = render("\n".join(lines))
    sure = re.search(r"\*\*How sure:\*\* (.+)", body).group(1).strip()
    return n, sure, (
        f'<details class="gate" id="gate-{n}">\n<summary><h3>{inline(head)}</h3>{sub}</summary>\n'
        f'<div class="gate-body">\n{inner}</div>\n</details>\n'
    )


def section(head, body, toc):
    sid = slug_section(head)
    toc.append((sid, head))
    h2 = f'<h2 id="{sid}">{inline(head)}</h2>\n'
    if head.startswith("5."):
        intro, *gates = split(body, 3)
        blocks, sure = [], []
        for gh, gb in gates:
            n, s, block = gate_block(gh, gb)
            blocks.append(block)
            sure.append((n, s))
        return f'<section class="toc-sec" aria-labelledby="{sid}">\n{h2}{render(intro[1])}{confidence_strip(sure)}\n<div class="gates">\n{"".join(blocks)}</div>\n</section>\n'
    if head.startswith(("8.", "9.")):
        intro, *subs = split(body, 3)
        out = [render(intro[1])]
        for sh, sb in subs:
            ssid = slug_section(sh)
            out.append(f'<details class="more" id="{ssid}">\n<summary><h3>{inline(sh)}</h3></summary>\n{render(sb)}</details>\n')
        return f'<section class="toc-sec" aria-labelledby="{sid}">\n{h2}{"".join(out)}</section>\n'
    if head.lower().startswith("appendix a"):
        lines = [l for l in body.strip().split("\n") if l.strip()]
        lead = [l for l in lines if not re.match(r"^\d+\. ", l)]
        items = "\n".join(
            f'<li id="source-{m.group(1)}" value="{m.group(1)}">{inline(m.group(2))}</li>'
            for m in (re.match(r"^(\d+)\. (.*)", l) for l in lines) if m
        )
        count = items.count("<li ")
        return (
            f'<section class="toc-sec sources" aria-labelledby="{sid}">\n{h2}{render(chr(10).join(lead))}'
            f'<details class="more" id="sources-list">\n<summary><h3>All {count} sources</h3></summary>\n<ol>\n{items}\n</ol>\n</details>\n</section>\n'
        )
    out = render(body)
    if head.startswith("6."):
        out = re.sub(r"(<p><strong>Keep people moving\.</strong>.*?</p>\n)", lambda m: m.group(1) + funnel() + "\n", out, flags=re.S)
    return f'<section class="toc-sec" aria-labelledby="{sid}">\n{h2}{out}</section>\n'


def build(src, updated):
    text = src.read_text(encoding="utf-8").replace("\r\n", "\n")
    text = re.sub(r"^# .*\n", "", text, count=1)
    intro, *sections = split(text, 2)
    mermaid = re.search(r"```mermaid\n(.*?)```", intro[1], re.S)
    intro_md = intro[1].replace(mermaid.group(0), "@@MAP@@")
    intro_html = render(intro_md).replace("<blockquote>", '<blockquote class="toc-brief">')
    intro_html = intro_html.replace("<p>@@MAP@@</p>\n", "").replace("@@MAP@@", "")
    brief, _, notes = intro_html.partition("</blockquote>\n")
    toc = [("in-brief", "The theory in brief"), ("journey-map", "The journey, gate by gate")]
    body = "".join(section(h, b, toc) for h, b in sections)
    main = (
        f'<section class="toc-sec" id="in-brief" aria-label="The theory in brief">\n{brief}</blockquote>\n{notes}</section>\n'
        f"{journey_map(mermaid.group(1))}\n{body}"
    )
    main = private_links(link_refs(main))
    nav = "\n".join(f'          <li><a href="#{sid}">{inline(h)}</a></li>' for sid, h in toc)
    date = datetime.date.fromisoformat(updated)
    when = f"{date.day} {date:%B %Y}"
    words = len(re.findall(r"\w+", re.sub(r"<[^>]+>", " ", main)))
    return PAGE.format(nav=nav, main=main, when=when, version=VERSION, minutes=round(words / 250 / 5) * 5)


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="robots" content="index,follow" />
  <title>Our approach | Is AI Good?</title>
  <meta name="description" content="Our theory of change. How we think Is AI Good? can help experienced professionals take a first step into AI safety, how sure we are, and how we'll know." />
  <!-- frame:head -->
  <!-- /frame:head -->
  <link rel="stylesheet" href="/approach/approach.css?v=20261027a" />
  <script defer src="/approach/approach.js?v=20261027a"></script>
</head>
<body class="approach-page">
  <header class="site-header compact">
    <div class="wrap">
      <!-- frame:header -->
      <!-- /frame:header -->
      <h1 class="page-title">Our approach</h1>
    </div>
  </header>

  <div class="reading-progress" aria-hidden="true"><span></span></div>

  <div class="wrap approach-layout">
    <nav class="on-page" aria-label="On this page">
      <details class="on-page-box" open>
        <summary>On this page</summary>
        <ol>
{nav}
        </ol>
      </details>
    </nav>

    <main id="main" class="text-page approach">
      <p class="toc-version">Our theory of change, version {version}. Updated {when}.</p>
      <p class="toc-version">The whole plan takes about {minutes} minutes to read. The summary and the journey take about 5.</p>
{main}
      <p class="next-step"><a class="btn-main" href="/">Back to the main page →</a></p>
    </main>
  </div>

  <!-- frame:footer -->
  <!-- /frame:footer -->
</body>
</html>
"""


if __name__ == "__main__":
    args = sys.argv[1:]
    updated = datetime.date.today().isoformat()
    if "--updated" in args:
        i = args.index("--updated")
        updated = args[i + 1]
        del args[i:i + 2]
    src = pathlib.Path(args[0]) if args else DEFAULT_SRC
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build(src, updated), encoding="utf-8", newline="\n")
    print(f"Wrote {OUT.relative_to(ROOT).as_posix()} from {src.name}")
