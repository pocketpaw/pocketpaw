# Fast lane

The fast lane merges small, low-risk crew PRs into `dev` once CI is green,
without waiting for a review. The captain reviews the merged set later (filter
by the `fast-lane-merged` label) and reverts anything wrong. Every other PR
keeps the normal review gate.

It runs as `.github/workflows/fast-lane.yml`, which calls `scripts/fast-lane.sh`.
The workflow never checks out or runs PR code. It reads PR metadata from the
GitHub API and runs the script from `dev`.

## Eligibility

All of these must hold:

1. The PR is open, not a draft, targets `dev`, and has the `fast-lane` label.
2. The author is on the allowlist (`FAST_LANE_AUTHORS` in the script).
   Community PRs are never fast-laned.
3. At most 400 changed lines (additions + deletions) and at most 15 files.
4. No changed path matches the deny list: `.github/`, `.claude/`, `CLAUDE.md`,
   `AGENTS.md`, dependency manifests and lockfiles, `Dockerfile`, `deploy/`,
   migrations, and any path containing auth, security, secret, credential,
   billing, payment, wallet, permission, policy, crypto, token, instinct,
   shield, sandbox, guardian or license. The match is case-insensitive.
5. GitHub reports the PR as mergeable (no conflicts).
6. Every check and status on the head commit passed, was neutral or was
   skipped, and at least one check from the CI, Tests or PR Quality Gate
   workflows passed. No checks at all counts as not green.

## What happens

- Breaks rule 2, 3 or 4: the bot removes the label and comments
  `Fast lane: not eligible` with the reasons. The PR goes to normal review.
- CI still running: nothing. It runs again when CI finishes.
- CI red: one comment, `Fast lane: waiting, CI is red`. The label stays, so a
  fix push tries again.
- Eligible and green: squash merge, label `fast-lane-merged`, and a
  `Fast lane: merged` comment with the size and check count.

## Opt in or out

- Opt in: add the `fast-lane` label to the PR.
- Opt out: remove the label before CI goes green.
- Re-check by hand: run the Fast Lane workflow from the Actions tab, or
  `scripts/fast-lane.sh --dry-run <PR>` locally to see the decision with no writes.

## Revert

Fast-lane PRs land as one squash commit, so reverting is one command:

```bash
git revert <merge-sha>   # on a branch off dev, then open a PR
```

Or use the Revert button on the merged PR.

## Tests

`bash tests/fast_lane/test_fast_lane.sh` runs the decision rules against fixtures.
