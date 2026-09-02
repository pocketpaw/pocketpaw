"""Tag a proxy request with the workspace that should pay for it.

Created 2026-09-02 (feat/proxy-spend-by-workspace).

WHY THIS EXISTS
---------------
Cloud chat was unbillable, and the failure was silent.

The LiteLLM billing cutover reads a tenant's spend as
``GET /spend/logs?api_key=<the tenant's virtual key>``. That works for the two
paths that actually send the tenant's key — Studio and the media MCP server —
and for nothing else. Both agent backends send
``settings.litellm_api_key``, which is the DEPLOYMENT's key, not any tenant's.
So a chat run's spend row is stamped with the deployment key, the per-tenant read
never matches it, and in ``live`` mode (where per-run metering is gated off so
exactly one meter charges) every chat run bills zero. Observed in production:
``ingested spend for 3/3 tenants -> 0 credits`` while the proxy's own log showed
real dollars for the same runs.

Handing each backend a per-tenant key would fix it and would keep the same shape
of hole: attribution would depend on a provisioning step, and a workspace that
missed that step would be free rather than loud. So the id rides on the REQUEST
instead. LiteLLM calls this an end-user (or customer); it needs no key, nothing
to provision, and a request that somehow carries no id shows up as unattributed
spend an operator can see rather than spend nobody is charged for.

THE CHAIN, END TO END
---------------------
Every hop below was read in the pinned ``litellm`` and ``pydantic-ai`` sources
rather than assumed, because a break at any one of them is invisible — it does
not error, it bills nothing:

1. The backend puts the workspace id in the request body's ``user`` field
   (pydantic-ai's ``openai_user`` model setting; ChatLiteLLM's ``model_kwargs``).
2. The proxy's ``get_end_user_id_from_request_body`` reads ``user`` (its check 3)
   and binds ``UserAPIKeyAuth.end_user_id``.
3. That becomes ``litellm_params["user_api_key_end_user_id"]``, which
   ``get_end_user_id_for_cost_tracking`` turns into the spend row's ``end_user``
   column.
4. The cutover ingest reads it back with
   ``GET /spend/logs/v2?end_user=<workspace>``.

TWO DEPLOYMENT PRECONDITIONS, both on the proxy and neither visible from here:

* ``litellm.disable_end_user_cost_tracking`` must stay off. It is the one switch
  that drops the id at step 3, and it drops it silently.
* A default customer budget (``litellm_settings.max_end_user_budget`` and its
  kin) now applies to these ids, because they are customers as far as the proxy
  is concerned. Unset by default; set it and it becomes a second, quieter budget
  next to the per-key one.

WHAT THE ID IS
--------------
The workspace, and nothing finer. The credit ledger's grain is the workspace —
``credits.debit`` takes one — so a session- or user-scoped id would attribute
spend the ledger cannot spend against, and a composite would have to be parsed
back apart at ingest. The proxy's ``end_user`` is one opaque string; this keeps
it the same string the wallet is keyed on.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The provider names whose requests reach our own LiteLLM proxy. Attribution is
# only added for these.
#
# Deliberately NOT ``openai_compatible``, even though an operator can point that
# provider at the proxy too. The id is a tenant identifier, and the difference
# between the two providers is whether we know where the request lands: on
# ``litellm`` it lands on infrastructure we run, and on ``openai_compatible`` it
# lands wherever the base URL says. Sending workspace ids to an arbitrary
# third-party endpoint to save an operator one config line is the wrong trade.
# An operator who routes the proxy through ``openai_compatible`` gets no
# attribution and, per the ingest's coverage check, gets told so.
_PROXY_PROVIDERS = frozenset({"litellm"})


def is_proxy_provider(provider: str | None) -> bool:
    """Whether ``provider`` routes through our LiteLLM proxy."""
    return (provider or "").strip().lower() in _PROXY_PROVIDERS


def current_workspace_id() -> str | None:
    """The workspace this run belongs to, or None when there isn't one.

    Reads the agent-identity ContextVar the cloud run loop binds
    (``attach_agent_identity``, set before the agent stream is driven, so it is
    visible from inside the backend's generator). None on a community install
    with no EE package, and None for any run outside a cloud chat dispatch — a
    CLI turn, a background job, a direct backend test. Those have no workspace to
    bill, so an untagged request is correct there, not a miss.

    Never raises. An attribution tag is not worth failing a run over, and the
    ingest's coverage check is what makes a silent None visible.
    """
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_workspace_id as _current

        workspace = _current()
    except Exception:  # noqa: BLE001 — see the docstring; this must never raise
        return None
    workspace = (workspace or "").strip()
    return workspace or None


def end_user_id_for(provider: str | None) -> str | None:
    """The ``user`` value to put on this run's request, or None to send none.

    None whenever the request is not going to our proxy, or the run has no
    workspace. Callers should omit the field entirely on None rather than send an
    empty string: the proxy treats ``""`` as an id, which would pool every
    untagged run under one blank customer and make the coverage check read as
    fully attributed.
    """
    if not is_proxy_provider(provider):
        return None
    return current_workspace_id()


__all__ = ["current_workspace_id", "end_user_id_for", "is_proxy_provider"]
