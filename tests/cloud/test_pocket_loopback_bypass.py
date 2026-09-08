"""The pocket internal bypass is refused in multi-tenant cloud.

``_loopback_bypass_active`` lets a caller skip cookie/bearer auth on
``GET /pockets/{id}``, ``POST /pockets/{id}/spec/merge`` and both reconcile
routes, taking the workspace AND the user from request headers
(``X-PocketPaw-Workspace-Id``, ``X-PocketPaw-User-Id``).

Four factors are required, and three of them are attacker-supplied. The fourth,
"the request came from loopback", provides nothing behind a reverse proxy — the
code says so itself, in ``_is_localhost``:

    Security contract: we deliberately do NOT honor ``X-Forwarded-For`` here.
    If a future deployment puts a reverse proxy in front of the dashboard,
    ``request.client.host`` will resolve to the proxy's address (also loopback
    if the proxy runs on the same box), making every external client appear
    loopback. This bypass MUST be disabled in that deployment, or this contract
    revisited.

Production behind nginx or Caddy on the same host is precisely that deployment.
So the process token is the entire gate, and a leaked token is any-tenant,
any-user impersonation across pocket reads and writes. The module's own
docstring calls the arrangement "interim, dev-grade".

WHY A SIGNAL RATHER THAN AN ENV FLAG

An env flag defaults to whatever an operator remembers to set, and the audit
that found this also found ``RECALL_WEBHOOK_SECRET`` — a variable that appeared
in no template, so the warning telling operators to set it was addressed to
nobody. ``is_multi_tenant_cloud()`` is not a thing to remember: it means "there
is a cloud DB, so more than one tenant lives here", and it is the same signal
the agent jail and the storage boot guard already use.

A local desktop install has no cloud DB and is unaffected — which matters,
because that is the deployment where this bypass is how the local agent reaches
its own backend.

Mutations that must fail these tests: dropping the ``_bypass_allowed_here``
call from ``_loopback_bypass_active``, returning True when the signal cannot be
read, and treating any present value of the opt-in variable as truthy.
"""

from __future__ import annotations

import importlib

import pytest

ROUTER = "pocketpaw_ee.cloud.pockets.router"


@pytest.fixture
def router_module():
    return importlib.import_module(ROUTER)


@pytest.fixture
def in_cloud(monkeypatch):
    """Make ``is_multi_tenant_cloud()`` report a multi-tenant deployment."""
    import sys
    import types

    module = types.ModuleType("pocketpaw_ee.cloud.shared.db")
    module.is_multi_tenant_cloud = lambda: True
    monkeypatch.setitem(sys.modules, "pocketpaw_ee.cloud.shared.db", module)


def test_the_bypass_is_refused_in_multi_tenant_cloud(router_module, in_cloud, monkeypatch):
    """The finding. One leaked process token was any-tenant impersonation."""
    monkeypatch.delenv("POCKETPAW_INTERNAL_BYPASS_ENABLED", raising=False)
    assert router_module._bypass_allowed_here() is False


def test_a_local_install_is_unaffected(router_module, monkeypatch):
    """No cloud DB means one tenant, and the local agent still reaches itself."""
    import sys
    import types

    module = types.ModuleType("pocketpaw_ee.cloud.shared.db")
    module.is_multi_tenant_cloud = lambda: False
    monkeypatch.setitem(sys.modules, "pocketpaw_ee.cloud.shared.db", module)
    monkeypatch.delenv("POCKETPAW_INTERNAL_BYPASS_ENABLED", raising=False)

    assert router_module._bypass_allowed_here() is True


def test_an_unreadable_signal_refuses(router_module, monkeypatch):
    """If we cannot tell whether this is cloud, we do not open the gate."""
    import sys
    import types

    module = types.ModuleType("pocketpaw_ee.cloud.shared.db")

    def _explode():
        raise RuntimeError("no db")

    module.is_multi_tenant_cloud = _explode
    monkeypatch.setitem(sys.modules, "pocketpaw_ee.cloud.shared.db", module)
    monkeypatch.delenv("POCKETPAW_INTERNAL_BYPASS_ENABLED", raising=False)

    assert router_module._bypass_allowed_here() is False


def test_the_escape_hatch_re_enables_it(router_module, in_cloud, monkeypatch):
    monkeypatch.setenv("POCKETPAW_INTERNAL_BYPASS_ENABLED", "1")
    assert router_module._bypass_allowed_here() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_only_an_explicit_opt_in_re_enables_it(router_module, in_cloud, monkeypatch, value):
    """``...=0`` must not mean yes.

    ``"VAR" in os.environ`` is the easy wrong check and would make every value
    an opt-in, including the one an operator sets to turn it off.
    """
    monkeypatch.setenv("POCKETPAW_INTERNAL_BYPASS_ENABLED", value)
    assert router_module._bypass_allowed_here() is False


def test_the_full_bypass_check_consults_the_gate(router_module, in_cloud, monkeypatch):
    """End to end through ``_loopback_bypass_active``, with every OTHER factor
    satisfied — so the only thing that can deny is the new gate.

    Written this way on purpose: a test that fails because the token is wrong
    would pass with the gate deleted.
    """
    from types import SimpleNamespace

    monkeypatch.delenv("POCKETPAW_INTERNAL_BYPASS_ENABLED", raising=False)
    monkeypatch.setattr(router_module, "_is_localhost", lambda request: True)
    monkeypatch.setattr(router_module, "_bypass_token_matches", lambda supplied: True)

    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

    assert (
        router_module._loopback_bypass_active(
            request,
            internal_header="true",
            internal_token="whatever",
            workspace_header="ws-victim",
            user_header="u-victim",
        )
        is False
    )


def test_every_other_factor_still_denies_on_its_own(router_module, monkeypatch):
    """The pre-existing factors are untouched — this change only ADDS one."""
    import sys
    import types
    from types import SimpleNamespace

    module = types.ModuleType("pocketpaw_ee.cloud.shared.db")
    module.is_multi_tenant_cloud = lambda: False
    monkeypatch.setitem(sys.modules, "pocketpaw_ee.cloud.shared.db", module)
    monkeypatch.setattr(router_module, "_is_localhost", lambda request: True)
    monkeypatch.setattr(router_module, "_bypass_token_matches", lambda supplied: True)

    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    base = {
        "internal_header": "true",
        "internal_token": "t",
        "workspace_header": "w",
        "user_header": "u",
    }
    assert router_module._loopback_bypass_active(request, **base) is True

    for missing, value in (
        ("internal_header", "no"),
        ("workspace_header", ""),
        ("user_header", ""),
    ):
        assert (
            router_module._loopback_bypass_active(request, **{**base, missing: value}) is False
        ), f"{missing} no longer denies on its own"


def test_a_non_loopback_request_is_denied_with_the_real_check(router_module, monkeypatch):
    """The loopback factor itself, with ``_is_localhost`` NOT stubbed.

    The test above monkeypatches ``_is_localhost`` to True so the new gate is
    the only thing that can deny — which is right for that test and useless for
    this one. A mutation deleting the loopback factor escaped the whole file
    until this case existed, because every other test had stubbed it away.
    """
    import sys
    import types
    from types import SimpleNamespace

    module = types.ModuleType("pocketpaw_ee.cloud.shared.db")
    module.is_multi_tenant_cloud = lambda: False
    monkeypatch.setitem(sys.modules, "pocketpaw_ee.cloud.shared.db", module)
    monkeypatch.setattr(router_module, "_bypass_token_matches", lambda supplied: True)
    monkeypatch.delenv("POCKETPAW_INTERNAL_BYPASS_ENABLED", raising=False)

    off_box = SimpleNamespace(client=SimpleNamespace(host="203.0.113.7"))
    assert (
        router_module._loopback_bypass_active(
            off_box,
            internal_header="true",
            internal_token="t",
            workspace_header="w",
            user_header="u",
        )
        is False
    )

    loopback = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    assert (
        router_module._loopback_bypass_active(
            loopback,
            internal_header="true",
            internal_token="t",
            workspace_header="w",
            user_header="u",
        )
        is True
    )
