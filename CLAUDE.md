# CLAUDE.md — public CODE repo (`Is-AI-good`)

**This repo holds all code + the website. It is PUBLIC and auto-deploys to https://isaigood.org.**
The owner is **non-technical** and owns the *why*; you own the code + git.

## What this repo is / isn't
- **Is:** the harvester (`scripts/`), the static site (`site/`), and the Cloudflare function
  (`functions/`). Cloudflare Pages watches `main` and deploys it live.
- **Isn't:** the documentation. Specs, decision logs, and the project's live view live in the **private**
  docs repo `AI-Harm-Watch` (cloned locally as the sibling folder `../AI Harm Watch`).

## The golden rule: never commit to `main`
`main` is protected and every merge to it goes **live**. All changes ship this way:
1. Create a **new branch**.
2. Make the change; push the branch; open a **PR**.
3. Cloudflare posts a **preview URL** on the PR — the owner eyeballs it.
4. Owner approves → **merge the PR**. That merge is the go-live moment.

A broken change can never reach the live site, because the owner sees the preview first.

## After any code change — log it
Code and its rationale are kept in separate homes. Once a change is merged, **record the decision in the
private docs repo** (`../AI Harm Watch`): append to the right **surface's** `decisions.md` — a pipeline
stage (`docs/pipeline/<stage>/`), the **website** (`docs/website/`), or the **data console**
(`docs/data/`) — and refresh `docs/LIVE-VIEW.md` if the top-level picture moved. The pipeline has 6
stages (collect · gate · triage · judge · copy · publish); the full surface map is `docs/README.md`.

## Security (push protection is ON)
- **Never commit secrets.** Keys (Airtable / Groq / YouTube) live only in GitHub Actions Secrets; the
  Cloudflare function reads its token from the environment. Never hard-code a key — push protection will
  block it anyway.
- The static site must carry **no** token.
- Run `/security-review` on the pending diff before a larger release.

## Environment notes
- Use `python` (not `python3`); the harvester is standard-library only.
- Avoid bash heredocs on Windows/Git Bash — write a `.py` file and run it.
- Groq 403s from the owner's local network — validate Groq code in the cloud (Actions), not locally.
