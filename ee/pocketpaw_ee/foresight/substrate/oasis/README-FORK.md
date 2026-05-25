# OASIS vendored fork — placeholder

Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 first PR.

## What is here

This is a **placeholder** for the vendored fork of
[camel-ai/oasis](https://github.com/camel-ai/oasis) at commit `46cdc8d`.

For v0.1 (this PR), only `LICENSE` lives here — we are NOT yet copying
the ~3,500 LOC of OASIS source into the repo. The v0.1 scaffold runs
without any OASIS code on disk because the engine surfaces (World,
Persona, Backend) are protocol-shaped, not subclass-shaped.

The src-copy ships in a separate PR (next on the foresight roadmap)
once the team has agreed on:

1. **src-copy vs git submodule.** The audit recommends src-copy ("we own
   the fork day one"); the captain may still want a submodule for the
   first carry-cost month. Decision deferred to the substrate-vendoring
   PR — both paths leave this directory layout unchanged.
2. **Which modules to vendor.** RFC §6 lists `social_agent/`,
   `environment/`, `agents_generator/`. We do NOT vendor
   `social_platform/Platform` (replaced by `ForesightWorld`) or the
   TWHIN-BERT recommender (dropped). Keeps the carry surface under
   ~2,000 LOC instead of 3,500.
3. **Pinning policy.** Locked at `46cdc8d` (current upstream main as of
   2026-05-25 per `gh api repos/camel-ai/oasis/commits/main`). Future
   cherry-picks land via explicit PRs against `ee/pocketpaw_ee/foresight/substrate/oasis/`
   with a justification line in the PR body.

## License

OASIS is Apache-2.0. The `LICENSE` file in this directory is a verbatim
copy of `github.com/camel-ai/oasis/main/LICENSE` (Copyright 2023 @
CAMEL-AI.org). Any modifications we make to vendored OASIS files in
future PRs will carry an explicit per-file "Modified by PocketPaw, YYYY-MM-DD"
notice at the top, per Apache-2.0 §4(b).

## Why a fork (not a pip dep)

Audit-locked reasons, reproduced from RFC 08 §6.1:

- Upstream is soft-dormant (last meaningful commit Mar 13, 2026; 13 open
  PRs unmerged at audit time).
- We need 100% control over the modules we use — bug fixes and API
  extensions land in our fork on our review cadence.
- The audit cap is ~3,500 LOC of vendored code, auditable end-to-end.
- CAMEL itself stays a PyPI dep (`camel-ai==0.2.78`) — the
  `BaseModelBackend` protocol comes from there, not from OASIS.

## What changes when src-copy lands

The follow-up PR will replace this file with a real `README-FORK.md`
that enumerates every file copied, every modification made, and the
upstream-rebase recipe if `camel-ai/oasis` ever revives at velocity.
