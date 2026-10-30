#!/usr/bin/env bash
# scripts/check_header_dates.sh — refuse new dated change entries in file headers.
#
# File headers describe the file's current state; history lives in git
# (CLAUDE.md § File header comments). An appended "Updated: 2026-..." line is a
# second changelog, and parallel PRs that each append one conflict on whichever
# merges second (pocketpaw #2343/#2345). This diffs <base>..<head>, skipping
# Markdown and JSON, and fails on any ADDED line of that shape. Existing entries
# are untouched: the check only fires when a change adds more.
#
# Usage: scripts/check_header_dates.sh <base-ref> <head-ref>
#   CI:    scripts/check_header_dates.sh HEAD^1 HEAD      (PR merge commit)
#   local: scripts/check_header_dates.sh origin/dev HEAD
set -euo pipefail

base="${1:?base ref}"
head="${2:?head ref}"
pattern='^\+.*\b(Updated|Changes?|Changed|Modified):? 20[0-9]{2}-[0-9]{2}'

hits="$(git diff -U0 "$base" "$head" -- . ':!*.md' ':!*.json' \
  | grep -E -n "$pattern" || true)"

if [ -n "$hits" ]; then
  echo "check_header_dates: dated change entries added in this diff:" >&2
  echo "$hits" >&2
  echo "Fix: rewrite the header to describe the file's current state; drop the date." >&2
  echo "History belongs in git log, not in a header comment (CLAUDE.md § File header comments)." >&2
  exit 1
fi
echo "check_header_dates: no dated header entries added."
