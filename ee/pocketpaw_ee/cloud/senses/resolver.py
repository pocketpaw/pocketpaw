# SenseResolver — binds a provider-agnostic Sense to a tenant's enabled connector.
# Created: 2026-06-08 — Sense tier chunk 2. A Sense (paw.email.v1) names a
# capability; this resolver picks the connector that fills it for a workspace,
# then executes via the EXISTING connectors_service path. READ-FIRST (v1): only
# ``trust_level == "auto"`` actions run; confirm/restricted (and unknown/missing
# trust) are BLOCKED and never sent to execute. Disambiguation: 0 candidates ->
# None (caller prompts-to-connect); 1 -> that connector; >1 -> the stored
# preference if it's among candidates, else the deterministic first with an
# ``ambiguous`` flag so the caller can ask the user to set a preference.

from __future__ import annotations

from dataclasses import dataclass, field

from pocketpaw.senses import validate_sense_id
from pocketpaw_ee.cloud.connectors import service as connectors_service
from pocketpaw_ee.cloud.connectors.dto import ExecuteActionRequest
from pocketpaw_ee.cloud.senses import preference
from pocketpaw_ee.cloud.senses.filler import ConnectorSenseFiller


@dataclass(frozen=True)
class ResolvedSense:
    """The connector chosen to fill a sense for a workspace.

    ``ambiguous`` is True when more than one enabled connector could fill the
    sense and no stored preference disambiguated it — the caller should surface
    a "set a preference" prompt. ``candidates`` is the full sorted candidate
    set so the caller can render the choices.
    """

    sense_id: str
    connector_name: str
    ambiguous: bool = False
    candidates: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SenseExecutionResult:
    """Envelope for ``execute_sense``.

    Structured (never an HTTP 500): ``ok`` is False with a stable ``error``
    code for the two refusal paths — no provider for the sense, and the
    read-first block on a non-auto action. On success ``data`` carries the
    underlying ``ExecuteActionResponse`` and ``connector_name`` records which
    provider ran.
    """

    ok: bool
    sense_id: str
    connector_name: str | None = None
    action: str | None = None
    error: str | None = None  # stable code: "sense.no_provider" | "sense.action_needs_approval"
    message: str | None = None
    data: object = None


async def resolve(
    sense_id: str,
    workspace_id: str,
    *,
    pocket_id: str | None = None,
) -> ResolvedSense | None:
    """Bind ``sense_id`` to the connector that fills it for this workspace.

    Returns ``None`` when no enabled connector can fill the sense (the caller
    decides what to do — typically prompt-to-connect). Raises
    ``SenseValidationError`` for an unknown ``paw.*`` id.
    """
    validate_sense_id(sense_id)

    registry = connectors_service._get_registry()  # noqa: SLF001 — reuse the EE singleton
    filler = ConnectorSenseFiller(registry)
    candidates = await filler.candidates(sense_id, workspace_id, pocket_id=pocket_id)

    if not candidates:
        return None

    if len(candidates) == 1:
        return ResolvedSense(
            sense_id=sense_id,
            connector_name=candidates[0],
            ambiguous=False,
            candidates=candidates,
        )

    # More than one candidate — let a stored preference decide.
    preferred = await preference.get_preference(workspace_id, sense_id, pocket_id=pocket_id)
    if preferred is not None and preferred in candidates:
        return ResolvedSense(
            sense_id=sense_id,
            connector_name=preferred,
            ambiguous=False,
            candidates=candidates,
        )

    # No usable preference — pick deterministically and flag for the caller.
    return ResolvedSense(
        sense_id=sense_id,
        connector_name=candidates[0],
        ambiguous=True,
        candidates=candidates,
    )


def _trust_for_action(registry, connector_name: str, action: str) -> str | None:
    """Read ``trust_level`` for an action from the connector's ConnectorDef.

    Returns the trust string (e.g. "auto" | "confirm" | "restricted") or
    ``None`` when the connector, the action, or the trust field is missing.
    ``None`` is treated by the gate as "not auto" -> blocked.
    """
    defn = registry.get_definition(connector_name)
    if defn is None:
        return None
    for a in getattr(defn, "actions", None) or []:
        if isinstance(a, dict) and a.get("name") == action:
            return a.get("trust_level")
    return None


async def execute_sense(
    sense_id: str,
    action: str,
    params: dict,
    workspace_id: str,
    *,
    pocket_id: str | None = None,
    user_id: str | None = None,
) -> SenseExecutionResult:
    """Resolve a sense, enforce the read-first gate, then delegate to execute.

    1. ``resolve`` — if no provider, return a structured ``sense.no_provider``
       result (never raise a 500).
    2. READ-FIRST GATE — the action's ``trust_level`` on the resolved
       connector must be exactly ``"auto"``. Anything else (confirm /
       restricted / unknown / missing) is refused with
       ``sense.action_needs_approval`` and ``connectors_service.execute`` is
       NEVER called.
    3. Delegate to the existing execute path with the resolved connector.
    """
    resolved = await resolve(sense_id, workspace_id, pocket_id=pocket_id)
    if resolved is None:
        return SenseExecutionResult(
            ok=False,
            sense_id=sense_id,
            action=action,
            error="sense.no_provider",
            message=(
                f"no enabled connector can fill {sense_id!r} for this workspace — "
                "connect a provider for this sense and retry."
            ),
        )

    connector_name = resolved.connector_name
    registry = connectors_service._get_registry()  # noqa: SLF001 — reuse the EE singleton
    trust = _trust_for_action(registry, connector_name, action)
    if trust != "auto":
        return SenseExecutionResult(
            ok=False,
            sense_id=sense_id,
            connector_name=connector_name,
            action=action,
            error="sense.action_needs_approval",
            message=(
                f"action {action!r} on {connector_name!r} needs approval "
                f"(trust_level={trust!r}) — not executed in v1 (read-first)."
            ),
        )

    response = await connectors_service.execute(
        workspace_id,
        connector_name,
        ExecuteActionRequest(action=action, params=params, pocket_id=pocket_id),
        user_id=user_id,
    )
    return SenseExecutionResult(
        ok=True,
        sense_id=sense_id,
        connector_name=connector_name,
        action=action,
        data=response,
    )


__all__ = ["ResolvedSense", "SenseExecutionResult", "execute_sense", "resolve"]
