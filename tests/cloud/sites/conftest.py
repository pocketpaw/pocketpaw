# tests/cloud/sites/conftest.py
# Created: 2026-08-12 (the custom-domain routing lane).
#
# WHY THIS EXISTS. ``tests/ee/sites/`` has cleared the operator's own Cloudflare
# environment before every test since 2026-08-11: ``config.py`` declares
# ``env_file=".env"`` and ``security/url_validators.py`` calls
# ``load_dotenv(override=False)`` at IMPORT time, searching UPWARD from the CWD — so a
# gitignored ``.env`` in the checkout root is silently test input, and the suite's
# result becomes a property of whose machine ran it.
#
# THAT FIX ONLY COVERED ONE OF THE TWO SITES TEST TREES. This one — the billing /
# tiers tests that also drive ``sites.service`` — was never isolated, and nothing
# noticed because no test here read a variable that changed behaviour. The routing lane
# gave ``add_domain`` a dependency on ``PAW_CF_DEPLOY_MODE`` (a route is only written in
# ``workers`` mode, where a per-site Worker exists to point at), and eight tier tests
# immediately went red on a developer machine with a live ``.env`` while staying green
# in CI, which has none. That difference is the whole bug: a gate that only fires on
# some machines is worse than no gate, because a red run stops meaning "the branch is
# broken".
#
# The variable list is imported rather than copied. ``tests/ee/sites/conftest.py`` owns
# it and ``test_env_isolation.py`` asserts it covers every PAW_CF_* name the sites
# source reads, so a second hand-maintained copy here would drift out from under that
# guard the first time somebody adds a variable.
from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

from tests.ee.sites.conftest import OPERATOR_ENV_VARS  # noqa: E402


@pytest.fixture(autouse=True)
def _operator_env_cleared_for_cloud_sites(monkeypatch):
    """Remove the operator's own Cloudflare environment from every test in this tree.

    Deleting, not setting: it makes each test's behaviour a property of the code under
    test rather than of the machine. Function-scoped, so a test that WANTS one of these
    sets it in its own body after this has run.

    Deleting alone is not enough, which is the part worth remembering — some call sites
    load the file LAZILY, inside the function, so they re-read it DURING the test, and a
    non-overriding ``load_dotenv`` is free to set a variable this fixture has just
    removed. So ``load_dotenv`` is neutered for the duration too, both spellings, since
    a lazy ``from dotenv import load_dotenv`` resolves the attribute at call time.
    """
    for name in OPERATOR_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    def _refuse_to_load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        """Stand in for ``dotenv.load_dotenv``, returning its "loaded nothing" value so
        a caller that checks the result sees an empty file rather than an error."""
        return False

    try:
        import dotenv
        import dotenv.main
    except Exception:  # pragma: no cover — dotenv is only an indirect dep
        pass
    else:
        monkeypatch.setattr(dotenv, "load_dotenv", _refuse_to_load_dotenv)
        monkeypatch.setattr(dotenv.main, "load_dotenv", _refuse_to_load_dotenv)
    yield
