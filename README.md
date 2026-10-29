# Is AI Good?

**A free, public gallery of real, sourced stories about the dangers and risks of AI — how it is
affecting and harming real people, shown in their original format and always linked back to source.**

The goal is to make AI's real-world impact *legible* to a general audience: not abstract argument or
statistics, but real human stories, categorised by theme and searchable. Every story links to its
original source, and every summary is written to be factual and neutral.

## How it works

The site is fully static — visitors only ever read, and content changes at most once a day — so there
is no server or database to run. An autonomous pipeline keeps it fed:

```
Daily/weekly (GitHub Actions)
  → collect candidates   (Reddit RSS + Google-Alert RSS + news/RSS feeds + YouTube)
  → AI triage & judge    (theme · a score per criterion · verdict · sensitivity)
  → AI card copy         (our own title + a short factual summary)
  → review console       (Airtable)
  → editorial gate       (auto-publish what passes the bar; the riskiest held for review)
  → build + deploy        (static JSON → the gallery re-renders)
```

- **The rulebook** ([`rulebook.md`](rulebook.md)) is the one home for the editorial rules: the themes, what
  counts as a story, the judge's scores and the bar, sensitivity, card copy and the settings. The triage, judge
  and copy prompts are built from it, and the site's theme summaries come from it.
- **Collection** is downstream-only — it reads public RSS/feeds and official platform embeds; it never
  scrapes walled apps.
- **The AI judge** scores each story for source credibility, real consequence, wider significance, fit for
  our reader (a thoughtful, experienced professional) and theme fit. The code, not the AI, applies the bar.
- **Editorial discipline is the credibility signal:** emotional in *what* is shown, disciplined in
  *how* it is sourced and framed. Sensitive stories are hidden by default with a label; the riskiest are
  held for a person to review.

## Tech

- **Harvester:** Python (standard library only), run on a schedule by **GitHub Actions**.
- **AI:** free-tier LLMs via **Groq** (a three-stage triage → judge → copy pipeline).
- **Content store / review console:** **Airtable**.
- **Site:** static HTML/CSS/JS — theme filter + free-text search + original-format embeds
  (YouTube, image, Instagram, TikTok, X, C-SPAN).
- **Hosting:** Cloudflare Pages. **Analytics:** Cloudflare Web Analytics (cookieless).
- **Cost:** designed to run entirely on free tiers.

## Repo structure

| Path | What it is |
|---|---|
| `site/` | The static website (what visitors see) — `index.html`, `styles.css`, `app.js`, `stories.json`, fonts |
| `scripts/` | The harvester + editorial pipeline (`harvest_reddit.py`, `publish.py`, `build_data.py`, …) |
| `.github/workflows/` | The scheduled automation (daily harvest, weekly sweep, publish & deploy) |

## Running the site locally

```bash
cd site
python -m http.server 8000
# open http://localhost:8000
```

The harvester and publish scripts require API credentials supplied via environment variables
(`AIRTABLE_TOKEN`, `GROQ_API_KEY`, and optionally `YOUTUBE_API_KEY`); they are never committed.
