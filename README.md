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
  → AI triage & judge    (theme · plain-language summary · relevance score · sensitivity flag)
  → review queue         (Airtable)
  → editorial gate       (auto-publish safe items; sensitive items held for review)
  → build + deploy        (static JSON → the gallery re-renders)
```

- **Collection** is downstream-only — it reads public RSS/feeds and official platform embeds; it never
  scrapes walled apps.
- **The AI judge** is a typed, source-agnostic editorial filter. Its north-star test is *"does this
  help a general reader understand a real risk, danger, or harm of AI?"* — keeping grounded, sourced
  stories and rejecting hype, promos, and opinion.
- **Editorial discipline is the credibility signal:** emotional in *what* is shown, disciplined in
  *how* it is sourced and framed. Sensitive items are always held for human review before publishing.

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
