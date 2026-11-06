#!/usr/bin/env bash
# The pipeline's working files live on the `pipeline-data` branch (see scripts/pipeline_data.py).
#   pipeline_data.sh checkout          put that branch in ./pipeline-data (a new empty branch the first time)
#   pipeline_data.sh push "<label>"    commit and push whatever changed there
# Nothing on this branch deploys: the site is built from main only.
set -euo pipefail
DIR=pipeline-data
BRANCH=pipeline-data

case "${1:-}" in
  checkout)
    if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
      git fetch --depth=1 origin "$BRANCH:refs/remotes/origin/$BRANCH"
      git worktree add -B "$BRANCH" "$DIR" "origin/$BRANCH"
    else
      git worktree add --detach "$DIR"
      (cd "$DIR" && git checkout -q --orphan "$BRANCH" && git rm -rfq . && git clean -fdq)
    fi
    ;;
  push)
    cd "$DIR"
    git config user.name "harvest-bot"
    git config user.email "actions@users.noreply.github.com"
    git add -A
    if git diff --cached --quiet; then
      echo "No working-file changes."
      exit 0
    fi
    git commit -qm "${2:-Pipeline} $(date -u +%F)"
    git push -q origin "HEAD:refs/heads/$BRANCH"
    echo "Working files pushed to $BRANCH."
    ;;
  *)
    echo "usage: $0 checkout | push <label>" >&2
    exit 2
    ;;
esac
