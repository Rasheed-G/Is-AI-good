# CLAUDE.md — public CODE repo (`Is-AI-good`)

**This repo holds all code + the website. It is PUBLIC and auto-deploys to https://isaigood.org.**
The owner is **non-technical** and owns the *why*; you own the code + git.

## What this repo is / isn't
- **Is:** the harvester (`scripts/`), the static site (`site/`), and the Cloudflare function
  (`functions/`). Cloudflare Pages watches `main` and deploys it live.
- **Isn't:** planning or documentation. Specs, plans, drafts and decision logs live in a separate private
  docs repo (cloned locally as the sibling folder `../AI Harm Watch`; its own `CLAUDE.md` explains where
  things go).

## Public means final only
Everything here is public: files, commit messages, branch names, PR titles, descriptions and comments.
Only put here what is live or about to go live, described as what it does.
- **PR titles and descriptions:** what changes for visitors and what changes behind the scenes. No build
  item IDs, release or step numbers, gates, plans, tests in progress, owner decisions or dates, review
  notes, or links and paths to the private docs.
- **Commit messages:** the same. Say what the change does.
- **Branches:** delete each branch once its PR is merged or closed.
- Planning and the reasons behind a change go in the private docs repo, never here.

## The golden rule: never commit to `main`
`main` is protected and every merge to it goes **live**. All changes ship this way:
1. Create a **new branch**.
2. Make the change; push the branch; open a **PR**.
3. Cloudflare posts a **preview URL** on the PR — the owner eyeballs it.
4. Owner approves → **merge the PR**. That merge is the go-live moment. Then delete the branch.

A broken change can never reach the live site, because the owner sees the preview first.

## After any code change — log it
Once a change is merged, record the decision in the private docs repo (`../AI Harm Watch`), following its
`CLAUDE.md`.

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
