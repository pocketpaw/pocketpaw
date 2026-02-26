<!-- Updated: 2026-02-26 — Added branch/issue warnings, tightened checklist. -->

> **Before opening this PR:**
> - Does it target `dev`? PRs against `main` are auto-closed.
> - Is there a linked issue? PRs without one will be closed.
> - Did you read [CONTRIBUTING.md](../blob/dev/CONTRIBUTING.md)?

## What does this PR do?

<!-- Describe the change in 2-3 sentences. What problem does it solve? -->

## Related Issue

<!-- Link the issue this PR addresses. PRs without a linked issue will be closed. -->

Fixes #

## Changes Made

<!-- List the specific files changed and what was modified in each. -->

- `file_path`: description of change

## How to Test

<!-- Step-by-step instructions for a reviewer to verify your changes work. -->

1.
2.
3.

## Evidence of Testing

<!-- Paste terminal output, test results, or screenshots proving you tested locally. -->

```
paste output here
```

## Checklist

- [ ] PR targets `dev` branch (not `main`)
- [ ] Linked to an existing issue
- [ ] I have run PocketPaw locally and tested my changes
- [ ] Tests pass (`uv run pytest --ignore=tests/e2e`)
- [ ] Linting passes (`uv run ruff check .`)
- [ ] I have added/updated tests if applicable
- [ ] No unrelated changes bundled in this PR
- [ ] No secrets or credentials in the diff
