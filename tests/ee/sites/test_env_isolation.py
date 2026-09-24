# tests/ee/sites/test_env_isolation.py
# Created: 2026-08-11 (fix/tests-ignore-operator-env) — proves the autouse
# ``_operator_env_cleared`` fixture in this directory's conftest actually removes the
# operator's ambient Cloudflare / Daytona environment, and that its list still covers
# every ``PAW_CF_*`` variable the sites source reads.
# Updated: 2026-09-24 (feat/sites-author-dependencies) — also pins the run-wide switch in
# the ROOT conftest: no ``.env`` is read at all (dotenv disabled, Settings.env_file
# dropped). The allowlist below only covers names someone thought of; a worktree under
# ``pocketPaw/.worktrees/`` loaded the parent checkout's ``.env`` and leaked
# ``POCKETPAW_SITES_BILLING_ENFORCED`` / ``PAW_SITES_GEN_CMD`` past it.
#
# WHY THIS FILE EXISTS. The guard it covers is invisible when it works: with no ``.env``
# in the checkout there is nothing to delete, so a broken or deleted fixture leaves the
# suite green on CI and green in a worktree, and only misbehaves on the one machine that
# has real credentials on disk — the machine least likely to suspect the harness. So the
# module-scoped fixture below PLANTS the variables first (module scope instantiates
# before the function-scoped conftest fixture, so the guard sees them and has something
# to remove), which makes the assertion real everywhere instead of vacuous.
#
# The planted values are deliberately self-describing non-secrets. Do not swap them for
# realistic-looking tokens: the repo's own secret scanner has been tripped by test
# canaries before, and a placeholder that names itself is also a better error message.

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tests.ee.sites.conftest import OPERATOR_ENV_VARS

# Shapes, not secrets. Only PAW_CF_DEPLOY_MODE needs a real-looking value, because it is
# the one variable whose CONTENT selects a code path ("workers" = the live Cloudflare
# deploy); the rest only need to be present to prove they get removed.
_PLANTED: dict[str, str] = {
    name: ("workers" if name == "PAW_CF_DEPLOY_MODE" else f"planted-by-{__name__}-not-a-credential")
    for name in OPERATOR_ENV_VARS
}


@pytest.fixture(scope="module", autouse=True)
def _simulate_operator_env():
    """Stand in for the operator's gitignored ``.env`` being loaded into the process.

    Module-scoped on purpose: pytest instantiates higher-scoped fixtures first, so these
    are already in ``os.environ`` when the function-scoped ``_operator_env_cleared``
    runs. ``monkeypatch`` is function-scoped and therefore unavailable here, so this
    saves and restores ``os.environ`` by hand — including the case where the developer
    running it genuinely has one of these set."""
    saved = {name: os.environ.get(name) for name in _PLANTED}
    os.environ.update(_PLANTED)
    yield
    for name, previous in saved.items():
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def test_operator_env_is_absent_inside_a_test() -> None:
    """The planted environment does not reach the test body."""
    leaked = {name: os.environ[name] for name in OPERATOR_ENV_VARS if name in os.environ}
    assert leaked == {}, (
        f"the sites suite is reading ambient configuration: {sorted(leaked)}. "
        "The autouse _operator_env_cleared fixture in tests/ee/sites/conftest.py is "
        "meant to delete these before every test."
    )


def test_deploy_mode_default_is_not_the_live_path() -> None:
    """The specific failure this guards: an ambient ``PAW_CF_DEPLOY_MODE=workers``
    silently routing tests down the real Cloudflare deploy with a live token present."""
    assert os.environ.get("PAW_CF_DEPLOY_MODE") is None


def test_a_mid_test_dotenv_load_cannot_put_them_back(tmp_path: Path) -> None:
    """The second leg of the guard: deleting the variables is not enough on its own.

    ``pocketpaw.uploads.factory.build_adapter`` imports and calls ``load_dotenv`` INSIDE
    the function, so it re-reads the file during the test — and because the fixture has
    made the variable absent, a non-overriding load is free to set it again. That is not
    hypothetical: it is how three test_preview_refresh.py tests kept reaching the live
    Cloudflare path after the delete was in place. This calls ``load_dotenv`` the same
    lazy way, pointed at a real file on disk, and asserts nothing lands in the
    environment."""
    env_file = tmp_path / ".env"
    env_file.write_text("PAW_CF_DEPLOY_MODE=workers\n", encoding="utf-8")

    from dotenv import load_dotenv

    load_dotenv(str(env_file), override=True)

    assert os.environ.get("PAW_CF_DEPLOY_MODE") is None, (
        "a mid-test load_dotenv reached os.environ — the _operator_env_cleared fixture "
        "is meant to neuter dotenv.load_dotenv for the duration of each test."
    )


def test_cleared_list_covers_every_paw_cf_variable_in_the_sites_source() -> None:
    """Catch a NEW ``PAW_CF_*`` variable added to sites without being added to the
    cleared list — otherwise the leak returns quietly through the new name."""
    from pocketpaw_ee import sites

    source_root = Path(sites.__file__).parent
    referenced: set[str] = set()
    for path in source_root.rglob("*.py"):
        referenced.update(re.findall(r"\bPAW_CF_[A-Z0-9_]+\b", path.read_text(encoding="utf-8")))

    assert referenced, f"found no PAW_CF_* references under {source_root} — bad glob?"
    missing = referenced - set(OPERATOR_ENV_VARS)
    assert not missing, (
        f"these PAW_CF_* variables are read by the sites source but are not cleared "
        f"before tests: {sorted(missing)}. Add them to OPERATOR_ENV_VARS in "
        "tests/ee/sites/conftest.py."
    )


def test_the_run_reads_no_dotenv_file_at_all() -> None:
    """The root conftest's run-wide switch, which the allowlist above cannot replace.

    python-dotenv walks UP from the calling file, so a worktree nested in a checkout
    with an operator ``.env`` read that file at import — and every variable in it that
    is not on OPERATOR_ENV_VARS reached the tests (the custom-domain cap and the
    concierge plan gate both went live via ``POCKETPAW_SITES_BILLING_ENFORCED``). CI has
    no ``.env``, so only this assertion notices the switch going missing.
    """
    from dotenv.main import _load_dotenv_disabled

    from pocketpaw.config import Settings

    if os.environ.get("PYTHON_DOTENV_DISABLED", "1").casefold() in {"0", "false", "f", "no", "n"}:
        pytest.skip("the developer opted back in with PYTHON_DOTENV_DISABLED=0")
    assert _load_dotenv_disabled(), (
        "PYTHON_DOTENV_DISABLED is not set in the test process — tests/conftest.py is "
        "meant to set it before the first pocketpaw import."
    )
    assert Settings.model_config.get("env_file") is None, (
        "Settings still reads the CWD's .env — tests/conftest.py is meant to drop it."
    )
