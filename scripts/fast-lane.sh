#!/usr/bin/env bash
# scripts/fast-lane.sh — merge small, green crew PRs into dev without waiting
# for captain review. The captain batch-reviews merged fast-lane PRs later and
# reverts anything wrong. Docs: docs/fast-lane.md.
#
# Run by .github/workflows/fast-lane.yml from the trusted dev branch. It only
# reads PR metadata through the GitHub API. It never checks out or runs PR code.
#
# Eligibility (all must hold): open, not draft, base dev, label `fast-lane`,
# author in FAST_LANE_AUTHORS, size within MAX_LINES/MAX_FILES, no changed path
# matching DENY_RE, MERGEABLE, and every check green with at least one passing
# check from CI_WORKFLOWS (no checks at all fails closed).
#
# `decide` is the pure decision function (no network). It reads a PR JSON file,
# a file-list file (one path per line) and a check-rollup JSON file, logs each
# rule to stderr, and prints one line to stdout:
#   skip|<why>  not-eligible|<reasons>  pending|<why>  red|<checks>
#   eligible|<lines>|<files>|<green checks>
# scripts/fast-lane.test.sh sources this file and feeds `decide` fixtures.
#
# Usage: scripts/fast-lane.sh [--dry-run] [PR ...]   (no PR: every open fast-lane PR)
#        scripts/fast-lane.sh [--dry-run] --sha SHA  (open fast-lane PRs at head SHA)
# --dry-run prints the decision and makes no API writes. Needs gh and jq.
set -euo pipefail

FAST_LANE_AUTHORS="prakashUXtech"
MAX_LINES=400
MAX_FILES=15
BASE_BRANCH="dev"
LABEL="fast-lane"
MERGED_LABEL="fast-lane-merged"
CI_WORKFLOWS=("CI" "Tests" "PR Quality Gate")
SELF_WORKFLOW="Fast Lane"
DENY_RE='^\.github/|^\.claude/|(^|/)(CLAUDE|AGENTS)\.md$|(^|/)(pyproject\.toml|uv\.lock|package\.json|bun\.lock|package-lock\.json|requirements[^/]*\.txt)$|(^|/)Dockerfile|^deploy/|/migrations?/|auth|security|secret|credential|billing|payment|wallet|permission|policy|crypto|token|instinct|shield|sandbox|guardian|license'

REPO="${GH_REPO:-pocketpaw/pocketpaw}"
DRY_RUN=0

log() { printf '  %s\n' "$*" >&2; }

# decide <pr.json> <files.txt> <checks.json>
decide() {
  local pr="$1" files="$2" checks="$3"
  local state draft base has_label author adds dels nfiles lines mergeable mstate
  state=$(jq -r '.state' "$pr")
  draft=$(jq -r '.isDraft' "$pr")
  base=$(jq -r '.baseRefName' "$pr")
  has_label=$(jq -r --arg l "$LABEL" '[.labels[]?.name] | index($l) != null' "$pr")
  author=$(jq -r '.author.login' "$pr")
  adds=$(jq -r '.additions' "$pr")
  dels=$(jq -r '.deletions' "$pr")
  nfiles=$(jq -r '.changedFiles' "$pr")
  mergeable=$(jq -r '.mergeable' "$pr")
  mstate=$(jq -r '.mergeStateStatus' "$pr")
  lines=$((adds + dels))

  local skip="" reasons=() pending="" red="" ngreen=0

  log "state=$state draft=$draft base=$base label=$has_label"
  [[ "$state" == "OPEN" ]] || skip="PR is $state"
  [[ -n "$skip" || "$draft" != "true" ]] || skip="PR is a draft"
  [[ -n "$skip" || "$base" == "$BASE_BRANCH" ]] || skip="base is $base, not $BASE_BRANCH"
  [[ -n "$skip" || "$has_label" == "true" ]] || skip="no $LABEL label"

  if [[ " $FAST_LANE_AUTHORS " == *" $author "* ]]; then
    log "author: ok ($author)"
  else
    log "author: FAIL ($author)"; reasons+=("author $author is not on the fast-lane allowlist")
  fi
  if (( lines <= MAX_LINES && nfiles <= MAX_FILES )); then
    log "size: ok ($lines lines, $nfiles files)"
  else
    log "size: FAIL ($lines lines, $nfiles files)"
    reasons+=("too big: $lines lines, $nfiles files (max $MAX_LINES lines, $MAX_FILES files)")
  fi
  local denied
  denied=$(grep -Ei -- "$DENY_RE" "$files" | paste -sd, - || true)
  if [[ -z "$denied" ]]; then
    log "paths: ok"
  else
    log "paths: FAIL ($denied)"; reasons+=("touches protected paths: ${denied//,/, }")
  fi

  if [[ "$mergeable" == "MERGEABLE" && ! "$mstate" =~ ^(DIRTY|UNKNOWN|BLOCKED|DRAFT)$ ]]; then
    log "mergeable: ok ($mstate)"
  else
    log "mergeable: not ready ($mergeable/$mstate)"; pending="not mergeable yet ($mergeable/$mstate)"
  fi

  # Drop our own check and Copilot review checks, then classify the rest.
  local ci_json summary
  ci_json=$(printf '%s\n' "${CI_WORKFLOWS[@]}" | jq -R . | jq -s .)
  summary=$(jq -c --arg self "$SELF_WORKFLOW" --argjson ci "$ci_json" '
    [ .[]
      | select((.workflowName // "") != $self)
      | select(((.name // .context // "") + " " + (.workflowName // "")) | test("copilot"; "i") | not)
      | { name: (.name // .context),
          wf: (.workflowName // ""),
          s: (if .__typename == "StatusContext" then
                (if .state == "SUCCESS" then "ok"
                 elif (.state == "PENDING" or .state == "EXPECTED") then "pending"
                 else "red" end)
              elif .status != "COMPLETED" then "pending"
              elif (.conclusion == "SUCCESS" or .conclusion == "NEUTRAL" or .conclusion == "SKIPPED") then "ok"
              else "red" end) } ]
    | { red: [ .[] | select(.s == "red") | .name ],
        pending: [ .[] | select(.s == "pending") | .name ],
        green: ([ .[] | select(.s == "ok") ] | length),
        ci_passed: ([ .[] | select(.s == "ok" and (.wf as $w | $ci | index($w) != null)) ] | length) }' "$checks")
  red=$(jq -r '.red | join(", ")' <<<"$summary")
  ngreen=$(jq -r '.green' <<<"$summary")
  local npending nci
  npending=$(jq -r '.pending | length' <<<"$summary")
  nci=$(jq -r '.ci_passed' <<<"$summary")
  # ponytail: "required CI exists" = at least one *passing* check from CI_WORKFLOWS,
  # so a head where every CI job was skipped still fails closed.
  log "checks: green=$ngreen pending=$npending red=[${red}] ci_passed=$nci"

  if [[ -n "$skip" ]]; then echo "skip|$skip"; return; fi
  if (( ${#reasons[@]} )); then
    local IFS=';'; echo "not-eligible|${reasons[*]}"; return
  fi
  if [[ -n "$red" ]]; then echo "red|$red"; return; fi
  if [[ -n "$pending" ]]; then echo "pending|$pending"; return; fi
  if (( npending )); then echo "pending|$npending checks still running"; return; fi
  if (( nci == 0 )); then local IFS=,; echo "pending|no passing check from ${CI_WORKFLOWS[*]} yet"; return; fi
  echo "eligible|$lines|$nfiles|$ngreen"
}

write() {  # run a mutating command unless --dry-run
  if (( DRY_RUN )); then log "dry-run, would: $*"; else "$@"; fi
}

comment_once() {  # comment unless an identical comment already exists
  local n="$1" body="$2"
  local existing
  existing=$(gh api "repos/$REPO/issues/$n/comments" --paginate --jq '.[].body')
  if grep -Fxq -- "$body" <<<"$existing"; then
    log "comment already present, not repeating"
    return
  fi
  write gh pr comment "$n" -R "$REPO" --body "$body"
}

evaluate() {
  local n="$1" tmp
  [[ "$n" =~ ^[0-9]+$ ]] || { echo "fast-lane: bad PR number: $n" >&2; return 1; }
  tmp=$(mktemp -d)
  gh pr view "$n" -R "$REPO" --json number,state,isDraft,baseRefName,labels,author,additions,deletions,changedFiles,mergeable,mergeStateStatus,headRefOid,statusCheckRollup >"$tmp/pr.json"
  jq '.statusCheckRollup // []' "$tmp/pr.json" >"$tmp/checks.json"
  gh api "repos/$REPO/pulls/$n/files" --paginate --jq '.[].filename' >"$tmp/files.txt"

  echo "PR #$n:" >&2
  local decision kind rest
  decision=$(decide "$tmp/pr.json" "$tmp/files.txt" "$tmp/checks.json")
  echo "decision: $decision"
  kind="${decision%%|*}"; rest="${decision#*|}"

  case "$kind" in
    skip|pending) ;;
    not-eligible)
      write gh pr edit "$n" -R "$REPO" --remove-label "$LABEL" || log "could not remove label"
      comment_once "$n" "Fast lane: not eligible. ${rest//;/; }. This PR now needs normal captain review."
      ;;
    red)
      comment_once "$n" "Fast lane: waiting, CI is red ($rest)"
      ;;
    eligible)
      local lines files green sha
      IFS='|' read -r lines files green <<<"$rest"
      sha=$(jq -r '.headRefOid' "$tmp/pr.json")
      write gh pr merge "$n" -R "$REPO" --squash --delete-branch=false --match-head-commit "$sha"
      write gh pr edit "$n" -R "$REPO" --add-label "$MERGED_LABEL" || log "could not add $MERGED_LABEL label"
      comment_once "$n" "Fast lane: merged ($lines lines, $files files, $green checks green)."
      ;;
  esac
  rm -rf "$tmp"
}

main() {
  local sha="" prs=()
  while (( $# )); do
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      --sha) sha="${2:?--sha needs a value}"; shift ;;
      -h|--help) sed -n '2,24p' "$0"; return 0 ;;
      *) prs+=("$1") ;;
    esac
    shift
  done
  if (( ${#prs[@]} == 0 )); then
    local q='.[].number'
    [[ -n "$sha" ]] && q=".[] | select(.headRefOid == \"$sha\") | .number"
    [[ -z "$sha" || "$sha" =~ ^[0-9a-f]{7,40}$ ]] || { echo "fast-lane: bad sha" >&2; return 1; }
    while read -r n; do [[ -n "$n" ]] && prs+=("$n"); done < <(
      gh pr list -R "$REPO" --state open --base "$BASE_BRANCH" --label "$LABEL" \
        --limit 100 --json number,headRefOid --jq "$q")
  fi
  (( ${#prs[@]} )) || { echo "fast-lane: no matching open $LABEL PRs" >&2; return 0; }
  local n
  for n in "${prs[@]}"; do evaluate "$n"; done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
