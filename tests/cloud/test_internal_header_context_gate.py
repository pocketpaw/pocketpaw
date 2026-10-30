"""The internal-header context is refused in multi-tenant cloud.

``loopback_or_request_context`` accepts the caller's workspace and user straight
from ``X-PocketPaw-Workspace-Id`` / ``X-PocketPaw-User-Id`` when the peer address
is loopback. It is bound on all 24 routes of ``foresight/router.py``.

Two factors, and the docstring is candid about the second:

    the loopback host check is the only boundary between the local chat agent
    and full workspace access

Behind a reverse proxy on the same host that boundary is nothing — every
external client presents as loopback. And the comment that made it look safer,
"uvicorn doesn't honor the header by default", is wrong: uvicorn's Config
defaults ``proxy_headers=True``, bounded by ``forwarded_allow_ips`` (default
``127.0.0.1``), which ``_core/rate_limit.py`` records as unset here.

WEAKER THAN THE ONE ALREADY JUDGED INSUFFICIENT

``pockets/router.py:_loopback_bypass_active`` requires a FOURTH factor — a
``compare_digest`` against a process-local secret — and PR #2126 still gated it
on ``is_multi_tenant_cloud()``. This path has no token and no compare. Same
gate, same reason.

Mutations that must fail these tests: dropping the gate call from
``loopback_or_request_context``, returning True when the signal cannot be read,
and treating any present value of the opt-in as truthy.
"""

from __future__ import annotations

import sys
import types

import pytest
from pocketpaw_ee.cloud._core import context as ctx


@pytest.fixture
def in_cloud(monkeypatch):
    module = types.ModuleType("pocketpaw_ee.cloud.shared.db")
    module.is_multi_tenant_cloud = lambda: True
    monkeypatch.setitem(sys.modules, "pocketpaw_ee.cloud.shared.db", module)
    monkeypatch.delenv(ctx._ALLOW_HEADER_BYPASS_ENV, raising=False)


def test_the_header_context_is_refused_in_multi_tenant_cloud(in_cloud):  # noqa: ARG001
    """The finding. The header trio was the whole of the tenancy check."""
    assert ctx._header_bypass_allowed_here() is False


def test_a_local_install_is_unaffected(monkeypatch):
    """No cloud DB means one tenant, and the local agent still reaches itself."""
    module = types.ModuleType("pocketpaw_ee.cloud.shared.db")
    module.is_multi_tenant_cloud = lambda: False
    monkeypatch.setitem(sys.modules, "pocketpaw_ee.cloud.shared.db", module)
    monkeypatch.delenv(ctx._ALLOW_HEADER_BYPASS_ENV, raising=False)

    assert ctx._header_bypass_allowed_here() is True


def test_an_unreadable_signal_refuses(monkeypatch):
    """If we cannot tell whether this is cloud, we do not open the bypass."""
    module = types.ModuleType("pocketpaw_ee.cloud.shared.db")

    def _explode():
        raise RuntimeError("no db")

    module.is_multi_tenant_cloud = _explode
    monkeypatch.setitem(sys.modules, "pocketpaw_ee.cloud.shared.db", module)
    monkeypatch.delenv(ctx._ALLOW_HEADER_BYPASS_ENV, raising=False)

    assert ctx._header_bypass_allowed_here() is False


def test_the_escape_hatch_re_enables_it(in_cloud, monkeypatch):  # noqa: ARG001
    monkeypatch.setenv(ctx._ALLOW_HEADER_BYPASS_ENV, "1")
    assert ctx._header_bypass_allowed_here() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_only_an_explicit_opt_in_re_enables_it(in_cloud, monkeypatch, value):  # noqa: ARG001
    """``...=0`` must not mean yes."""
    monkeypatch.setenv(ctx._ALLOW_HEADER_BYPASS_ENV, value)
    assert ctx._header_bypass_allowed_here() is False


def test_the_resolver_consults_the_gate_with_every_other_factor_satisfied(in_cloud, monkeypatch):  # noqa: ARG001
    """End to end, with the loopback check stubbed TRUE.

    Written this way on purpose: a test that fails because the peer address is
    wrong would also pass with the gate deleted.
    """
    monkeypatch.setattr(ctx, "_is_loopback_client", lambda request: True)

    class _Req:
        headers = {
            ctx.INTERNAL_HEADER: "true",
            ctx.WORKSPACE_HEADER: "ws-victim",
            ctx.USER_HEADER: "u-victim",
        }

    assert ctx._try_loopback_context(_Req()) is None


def test_the_loopback_factor_still_denies_on_its_own(monkeypatch):
    """The pre-existing factor is untouched — this change only ADDS one.

    With ``_is_loopback_client`` NOT stubbed, so a mutation deleting it cannot
    escape behind the new gate.
    """
    module = types.ModuleType("pocketpaw_ee.cloud.shared.db")
    module.is_multi_tenant_cloud = lambda: False
    monkeypatch.setitem(sys.modules, "pocketpaw_ee.cloud.shared.db", module)
    monkeypatch.delenv(ctx._ALLOW_HEADER_BYPASS_ENV, raising=False)

    class _OffBox:
        headers = {
            ctx.INTERNAL_HEADER: "true",
            ctx.WORKSPACE_HEADER: "ws",
            ctx.USER_HEADER: "u",
        }
        client = types.SimpleNamespace(host="203.0.113.7")

    assert ctx._try_loopback_context(_OffBox()) is None

    class _Loopback(_OffBox):
        client = types.SimpleNamespace(host="127.0.0.1")

    assert ctx._try_loopback_context(_Loopback()) is not None


def test_the_uvicorn_claim_is_not_restated_anywhere(monkeypatch):  # noqa: ARG001
    """The comment that made this look safe was factually wrong.

    uvicorn's Config defaults ``proxy_headers=True``. Pinned because the claim
    is the kind that gets re-added by someone reasoning from memory.
    """
    import inspect

    source = inspect.getsource(ctx)
    assert "doesn't honor the header by default" not in source, (
        "the incorrect uvicorn claim is back; uvicorn defaults proxy_headers=True"
    )
