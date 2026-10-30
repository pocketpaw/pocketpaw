# OSS/EE Split — Phase 5: Publish Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Cut `pocketpaw 1.0` to PyPI (public, MIT) and `pocketpaw-ee 1.0` to a private index (FSL-1.1). Update workspace clients (`paw-enterprise`) and any deployment configs that previously referenced `ee.*` paths to point at the new distribution model. Announce.

**Architecture:** Two release pipelines, two artifact destinations. Root `pyproject.toml` → PyPI via OIDC trusted publishing. `ee/pyproject.toml` → private index (recommendation: a GCS-backed simple index, or PyPI's private project if budget allows, or GitHub Releases with `--extra-index-url` for paying customers).

**Tech Stack:** Same plus `twine` or PyPI's OIDC publisher, GitHub Actions, and whatever private index you settle on.

**Reference:** Design doc Section 7 Phase 5. Depends on Phases 1–4 being merged.

---

## Pre-flight decisions

These need answers before any task runs:

1. **PyPI account ownership.** Does Anthropic/PocketPaw already own the `pocketpaw` name on PyPI? If not, claim it. Use OIDC trusted publishing — no long-lived tokens.
2. **Private index choice for `pocketpaw-ee`.** Options:
   - **PyPI private project** ($$, simplest).
   - **GCS / S3 simple index** (we control everything; cheap).
   - **GitHub Releases** with `pip install --extra-index-url` (clunky for licensed customers).
   - **JFrog / Cloudsmith / Gemfury** (commercial private indexes).
3. **License-key distribution.** How does a paying customer receive credentials for the private index? Email per-customer URL? OAuth-gated download endpoint?
4. **Versioning policy.** Both packages versioned together (lockstep `1.0` on both) or independent? Recommendation: lockstep at major.minor, independent patches.
5. **Branch policy.** Cut releases from `main` only. Tag scheme `v1.0.0` for core, `v1.0.0-ee` for EE (or both share a tag and the workflow builds both).

Capture decisions in a short ADR (e.g., `docs/adr/0001-publishing-channels.md`) before starting Task 1.

---

## Task 1: Final version bump and changelog

**Files:**
- Modify: `src/pocketpaw/__init__.py` — bump `__version__` to `1.0.0`
- Modify: `ee/pocketpaw_ee/__init__.py` — bump `__version__` to `1.0.0`
- Create: `CHANGELOG.md` (if absent) with a `1.0.0` entry summarizing Phases 1–4
- Create: `ee/CHANGELOG.md` similarly

Commit on a release branch `release/1.0.0`.

---

## Task 2: Set up PyPI OIDC trusted publishing for `pocketpaw`

**On PyPI:**
1. Log in, create project `pocketpaw` (or claim existing).
2. Settings → Publishing → Add trusted publisher → GitHub Actions.
   - Repository: `<your-org>/<your-repo>`
   - Workflow: `.github/workflows/publish-pocketpaw.yml`
   - Environment: `pypi-pocketpaw`

**On GitHub:**
1. Create environment `pypi-pocketpaw` with required reviewers.
2. Add `.github/workflows/publish-pocketpaw.yml`:

```yaml
name: Publish pocketpaw to PyPI
on:
  push:
    tags: ["v*", "!*-ee"]
jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi-pocketpaw
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv build --wheel --sdist
      - uses: pypa/gh-action-pypi-publish@release/v1
```

Test by tagging a pre-release `v1.0.0rc1` against TestPyPI first (use the action's `repository-url` input). Verify the wheel installs cleanly in a fresh venv from TestPyPI before tagging the real release.

---

## Task 3: Set up private index for `pocketpaw-ee`

Implementation depends on the choice in Pre-flight #2. Common pattern (GCS-backed simple index):

1. Provision a GCS bucket `pocketpaw-ee-index`.
2. Generate `index.html` at `pocketpaw_ee/index.html` listing all uploaded wheels (use `dumb-pypi` or roll your own — it's ~50 lines).
3. Set bucket auth: signed-URL per customer, or basic auth at a CDN edge (Cloudflare workers / Coolify route).
4. Add `.github/workflows/publish-pocketpaw-ee.yml`:

```yaml
name: Publish pocketpaw-ee to private index
on:
  push:
    tags: ["v*-ee"]
jobs:
  publish:
    runs-on: ubuntu-latest
    environment: ee-index
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: cd ee && uv build --wheel --sdist
      - uses: google-github-actions/auth@v2
        with:
          credentials_json: ${{ secrets.EE_INDEX_GCS_SA }}
      - run: |
          gsutil cp ee/dist/* gs://pocketpaw-ee-index/pocketpaw_ee/
          uv run python scripts/rebuild_simple_index.py
          gsutil cp index.html gs://pocketpaw-ee-index/pocketpaw_ee/index.html
```

**Customer install path:**
```bash
pip install --extra-index-url https://<customer-token>@ee-index.pocketpaw.com/simple/ pocketpaw-ee
```

(If GitHub Releases is chosen instead, replace with a tagged release that attaches the wheel + sdist as assets and document the install command.)

---

## Task 4: Smoke-test publishing pipeline against pre-releases

**Step 1: Cut `v1.0.0rc1` and `v1.0.0rc1-ee`**

```bash
git tag v1.0.0rc1
git tag v1.0.0rc1-ee
git push --tags
```

Watch both workflows. Fix any failures (auth, build, upload paths) — re-tag with `rc2` if needed.

**Step 2: Install from the published artifacts**

```bash
uv venv .venv-pub && . .venv-pub/Scripts/activate

# OSS path
pip install --index-url https://test.pypi.org/simple/ pocketpaw==1.0.0rc1
python -c "import pocketpaw; print(pocketpaw.__version__)"
python -c "import pocketpaw_ee" 2>&1 | grep ModuleNotFoundError

# EE path (using a test customer credential)
pip install --extra-index-url https://<test-token>@ee-index.pocketpaw.com/simple/ pocketpaw-ee==1.0.0rc1
python -c "import pocketpaw_ee; print(pocketpaw_ee.__version__)"
```

Expected: both work end-to-end.

---

## Task 5: Update workspace clients

Anywhere outside `backend/` that referenced the old `ee.*` namespace or assumed bundled installation needs updating:

**Step 1: Cross-repo grep**

```bash
cd D:/paw
grep -rn "ee\.\|pocketpaw_ee" paw-enterprise ripple side-projects soul-protocol soul-site --include="*.ts" --include="*.svelte" --include="*.py" --include="*.json" --include="*.toml" 2>/dev/null
```

**Step 2: For each finding, decide:**
- HTTP API consumers (likely most): no code change needed; the HTTP surface is unchanged.
- Build/CI configs that `pip install pocketpaw[enterprise]`: update to `pip install pocketpaw pocketpaw-ee` (with `--extra-index-url` configured).
- Dockerfiles in `backend/deploy/`: update install commands to pull both wheels for the cloud image; OSS image installs only `pocketpaw`.

**Step 3: Build and test each updated client**

- `paw-enterprise`: `bun run tauri dev` against a backend running the new install.
- `backend/deploy/`: build the OSS Docker image and the cloud Docker image; smoke-test both come up.

---

## Task 6: Update Coolify deployment

(Per memory: deploys are on Coolify.)

1. Update the cloud-side service's build command to install both `pocketpaw` and `pocketpaw-ee`. The cloud image needs the `--extra-index-url` credential injected at build time as a build arg or Coolify secret.
2. Update environment variables: any `POCKETPAW_*` that referenced the bundled `enterprise` extra is now redundant.
3. If you previously ran only one PocketPaw image, decide whether to keep one image (with EE installed) or split into two (OSS image for community demos, EE image for paying customers + own SaaS).

Test in staging before flipping production.

---

## Task 7: Update marketing site and docs

**Files:**
- `soul-site/` (or wherever the marketing site lives) — add an "Editions" page or a comparison table. OSS vs Cloud vs Self-hosted EE.
- Top-level `README.md` (workspace) and `backend/README.md` — install instructions for the OSS path (PyPI badge), pointer to EE for cloud customers.
- GitHub repo description and topics — add "open source", "self-hosted", "agentic".

---

## Task 8: Announce

**Internal first:**
- Slack / team channel — what changed, how to install, where to file issues.
- Existing users (if any) — migration note: `pip install pocketpaw[enterprise]` → `pip install pocketpaw pocketpaw-ee` with the new index URL.

**External (after internal sign-off):**
- Blog post (one).
- HN/Reddit/Twitter (one announcement, link to blog).
- Update GitHub repo to show recent activity.

Keep the announcement honest: this is the rename + clean separation, not new features. Save feature announcements for separate posts.

---

## Task 9: Promote `rc1` to `1.0.0`

After the smoke tests in Task 4 pass and there's at least one full business day of internal use without issues:

```bash
git checkout main
git tag v1.0.0
git tag v1.0.0-ee
git push --tags
```

Both publish workflows fire. Verify wheels land on PyPI / private index. Drink coffee.

---

## Rollback

If a published wheel is broken after release, do not delete from PyPI (PyPI does not allow re-uploading the same version even after delete). Instead:

```bash
# Yank the broken version
twine upload --skip-existing dist/pocketpaw-1.0.0.whl  # no-op if already there
# In a Python venv:
pip install pocketpaw==1.0.0  # confirms current state
```

Use PyPI's "yank" feature via web UI to discourage installs while you cut `1.0.1`.

For `pocketpaw-ee` on the private index, just remove the wheel from GCS and rebuild the index. Customers will see only the previous version.

---

## Definition of done

- [ ] `pip install pocketpaw==1.0.0` works from PyPI in a fresh venv
- [ ] `pip install pocketpaw-ee==1.0.0 --extra-index-url <ee-index>` works for an authorized customer
- [ ] OSS install does not contain `pocketpaw_ee` code on disk
- [ ] CI tags trigger both publish workflows correctly
- [ ] Coolify cloud deployment is on the new install path; OSS image (if cut) is on the new install path
- [ ] `paw-enterprise` runs against the new backend with no code changes
- [ ] Marketing site shows the two editions
- [ ] Announcement posted (internal + external)
- [ ] No regressions reported within 48 hours of release; otherwise cut `1.0.1`
