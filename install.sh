#!/usr/bin/env bash
# slate installer — copy slate into your repository and make your agent aware of it.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/bioneural/slate/main/install.sh) [dir]
#
# [dir] is where slate lives in your repo (default: tasks). Run from your repo root.
# Re-run any time to update slate.py, AGENTS.md, and the template in place; your
# project.md, your issues, and any existing CLAUDE.md content are left untouched.
set -euo pipefail

DIR="${1:-tasks}"
RAW="${SLATE_RAW:-https://raw.githubusercontent.com/bioneural/slate/main}"

command -v python3 >/dev/null 2>&1 || { echo "slate: python3 is required but not found" >&2; exit 1; }
command -v curl    >/dev/null 2>&1 || { echo "slate: curl is required but not found" >&2; exit 1; }

echo "slate: installing into $DIR/"
mkdir -p "$DIR/templates" "$DIR/issues"

fetch() { curl -fsSL "$RAW/$1" -o "$2"; }
fetch slate.py           "$DIR/slate.py"
fetch AGENTS.md          "$DIR/AGENTS.md"
fetch templates/issue.md "$DIR/templates/issue.md"

# Starter project.md — written only if you do not already have one.
if [ ! -f "$DIR/project.md" ]; then
  cat > "$DIR/project.md" <<EOF
---
title: $(basename "$PWD")
status: Active
updated: $(date +%F)
---

# $(basename "$PWD")

Tasks for this project. Copy templates/issue.md to issues/<ID>.md to add one.

Status values: Backlog, Todo, In Progress, In Review, Done, Canceled.
Priority values: Urgent, High, Medium, Low, No priority.
EOF
  echo "slate: wrote starter $DIR/project.md"
fi

# Tell your agent: write a managed block into your root CLAUDE.md that imports
# AGENTS.md and instructs the agent to track work in slate.
python3 "$DIR/slate.py" install

echo "slate: done. run 'python3 $DIR/slate.py' to view the board at http://localhost:8787"
