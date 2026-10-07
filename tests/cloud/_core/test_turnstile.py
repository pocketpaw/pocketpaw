# tests/cloud/_core/test_turnstile.py — the shared Turnstile verifier's unset-secret rule.
#
# With no POCKETPAW_TURNSTILE_SECRET the check is skipped in dev but refused in a
# production posture (POCKETPAW_ENV=production or POCKETPAW_AUTH_COOKIE_SECURE=true),
# so a deploy that forgot the secret never accepts bot submissions. The Cloudflare
# round trip itself is covered by tests/cloud/ai_visibility/test_public_check.py.

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud._core import turnstile
from pocketpaw_ee.cloud._core.errors import BadRequest


@pytest.fixture(autouse=True)
def _unset_secret(monkeypatch):
    monkeypatch.setattr(
        "pocketpaw.config.get_settings", lambda: SimpleNamespace(turnstile_secret=None)
    )
    monkeypatch.delenv("POCKETPAW_ENV", raising=False)
    monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)


@pytest.mark.asyncio
async def test_dev_skips_with_a_warning(caplog) -> None:
    with caplog.at_level("WARNING"):
        await turnstile.verify_turnstile("tok", "1.2.3.4", code="pros.turnstile_failed")
    assert "unset, skipping" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env", [{"POCKETPAW_AUTH_COOKIE_SECURE": "true"}, {"POCKETPAW_ENV": "production"}]
)
async def test_production_refuses(monkeypatch, env) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(BadRequest) as exc:
        await turnstile.verify_turnstile("tok", "1.2.3.4", code="pros.turnstile_failed")
    assert exc.value.code == "pros.turnstile_failed"


@pytest.mark.asyncio
async def test_production_is_the_shared_signal(monkeypatch) -> None:
    """The rule reuses auth.core._is_production, not a private copy of it."""
    monkeypatch.setattr(turnstile, "_is_production", lambda: True)
    with pytest.raises(BadRequest):
        await turnstile.verify_turnstile("tok", None, code="tools.ai_check.turnstile_failed")
