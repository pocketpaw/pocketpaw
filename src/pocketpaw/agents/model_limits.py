# Resolve a model's output-token ceiling, so a run can send an explicit cap.
# Created: 2026-08-16 (feat/agent-max-output-tokens).
#
# WHY. No agent backend sent ``max_tokens`` at all. The only ModelSettings in
# the agents package is the AgentAPI shim, where the parameter is explicitly
# ignored. Sending nothing is not neutral: OpenRouter bills its PRE-FLIGHT
# credit check against ``max_tokens``, and with none supplied it substitutes the
# model's ceiling. On deepseek-v4-flash that is 65536, so a request whose reply
# would be a few hundred tokens is refused with
#
#   402 - This request requires more credits, or fewer max_tokens. You
#         requested up to 65536 tokens, but can only afford 6627.
#
# The account was not out of credits in any meaningful sense; it could not
# afford the WORST CASE we implicitly asked for. Sending the model's real
# 8192 shrinks that reservation eightfold. Other gateways price the reservation
# differently, but none of them are helped by us declining to say.
#
# WHERE THE NUMBER COMES FROM. litellm ships
# ``model_prices_and_context_window_backup.json`` — ~1.3 MB, 3000+ models —
# inside the installed package, and ``get_model_info()`` reads it. That is the
# same data as BerriAI's raw JSON on GitHub, except it is pinned with the
# dependency and reviewed when the pin moves, instead of being whatever ``main``
# says at the moment a process happens to boot.
#
# LITELLM_LOCAL_MODEL_COST_MAP. litellm's ``get_model_cost_map`` fetches that
# JSON over HTTP AT IMPORT TIME unless this variable is set, falling back to the
# bundled copy only on failure. So the network fetch already happens today,
# unpinned, in the import path of anything that touches litellm. We set the
# variable with ``setdefault`` before importing: it removes a startup network
# dependency we did not intend, keeps model data pinned to the reviewed version,
# and an operator who genuinely wants live pricing can still export it as false.
#
# FAILING OPEN IS THE POINT. Every lookup path returns None rather than raising.
# None means "send no cap", which is exactly today's behaviour — so a model
# litellm has never heard of, or a deployment without litellm installed, is no
# worse off than before this file existed.

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

#: Returned by the settings reader to mean "the operator disabled this".
_DISABLED = -1

#: The cap sent when nothing more specific is known.
#
# NOT the model's advertised ceiling, and this is the whole design decision.
# The goal is a reservation that covers a real reply, not the largest reply the
# model could theoretically emit — those are different numbers and only the
# first one is useful. Asking for the ceiling is what produced the 402 in the
# first place, because that is precisely what a gateway assumes when we send
# nothing.
#
# The metadata is also not trustworthy enough to drive the value. Measured on
# 2026-08-16, ``deepseek/deepseek-v4-flash`` read ``max_output_tokens: 8192``
# upstream in the morning and ``393216`` the same afternoon — a 48x change,
# with ``max_input_tokens`` sitting at 1000000, which reads like a context
# window leaking into the output field. Had we resolved the ceiling and sent it,
# the credit reservation would have gone from 65536 to 393216 and the 402 would
# have got SIX TIMES worse while looking like a fix.
#
# 8192 is generous for a chat turn (roughly 6000 words) and small enough that a
# pre-flight credit check passes on a modest balance. An operator who needs
# more sets ``agent_max_output_tokens``.
DEFAULT_MAX_OUTPUT_TOKENS = 8192


def _model_info(name: str) -> dict[str, Any] | None:
    """``litellm.get_model_info(name)`` or None, never raising."""
    if not name:
        return None
    try:
        # setdefault, not assignment: an operator who wants live pricing can
        # still export it false. See the module header — without this, litellm
        # HTTP-fetches the cost map at import, so merely asking a question about
        # a model reaches the network and reads whatever `main` says today.
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")
        import litellm
    except Exception:  # noqa: BLE001 — litellm is an optional extra
        logger.debug("litellm unavailable; no model metadata for %r", name)
        return None
    try:
        info = litellm.get_model_info(name)
    except Exception:  # noqa: BLE001 — an unknown model raises, and prints
        # litellm writes a "Provider List:" banner to the console before
        # raising. Unknown models are ordinary here (the pinned map trails
        # new releases), so this stays at debug and the caller falls back.
        logger.debug("No pinned model metadata for %r", name)
        return None
    return info if isinstance(info, dict) else None


@lru_cache(maxsize=256)
def model_output_ceiling(provider: str, model: str) -> int | None:
    """The model's documented ``max_output_tokens``, or None if unknown.

    Tries the bare model name first, then ``provider/model``. Both spellings
    occur in real config: ``pydantic_ai_model`` may be ``deepseek/deepseek-v4-
    flash`` (already vendor-qualified, provider resolved separately from
    ``pydantic_ai_provider``) or a bare ``gpt-4o`` under an explicit provider.

    Cached because it is consulted once per run and the answer is a property of
    the installed litellm, which cannot change inside a process.
    """
    for candidate in (model, f"{provider}/{model}" if provider and model else ""):
        info = _model_info(candidate)
        if not info:
            continue
        for key in ("max_output_tokens", "max_tokens"):
            value = info.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return None


def resolve_max_output_tokens(provider: str, model: str, settings: Any) -> int | None:
    """The output cap to send for this run, or None to send none.

    1. ``agent_max_output_tokens`` < 0 — the operator opted out. Send nothing,
       which is the pre-existing behaviour, 402s and all.
    2. Otherwise the target is the operator's positive value, or
       ``DEFAULT_MAX_OUTPUT_TOKENS``.
    3. The model's documented ceiling is applied as an UPPER BOUND only, and
       only when known. It can lower the target, never raise it.

    Step 3 is deliberately weak. Metadata answers "what is the most this model
    could emit", and the reservation we want is "enough for a real reply" —
    letting the first override the second is how a fix becomes a bigger 402.
    Its real job is the narrow one it is good at: not asking a small model for
    more than it can produce, which some providers reject outright.
    """
    configured = getattr(settings, "agent_max_output_tokens", 0)
    try:
        configured = int(configured or 0)
    except (TypeError, ValueError):
        configured = 0

    if configured <= _DISABLED:
        return None

    target = configured if configured > 0 else DEFAULT_MAX_OUTPUT_TOKENS
    ceiling = model_output_ceiling(provider or "", model or "")
    if ceiling and ceiling < target:
        return ceiling
    return target
