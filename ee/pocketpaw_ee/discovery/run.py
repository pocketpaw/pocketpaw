# pocketpaw_ee/discovery/run.py — the DiscoveryRun orchestrator.
#
# Created: 2026-06-19 (SZD-4 / feat/szd-4-discovery-run) — ties SZD-2 sampling
# and the SZD-3 digester together. ``DiscoveryRun.run(workspace_id,
# connector_ids, opts)`` enumerates a workspace's bound connectors, SAMPLES a
# bounded N records per connector, groups records by type, and runs the
# ``StructuredShapeDigester`` to produce an ``OntologyDraft``.
#
# CRITICAL read-path contract (a design review caught this):
#   * Discovery runs BEFORE any pocket exists, so we do NOT call
#     ``connector.sync(pocket_id)`` — sync requires a pocket AND returns COUNTS,
#     not records.
#   * Records are read through the workspace-scoped, pocket-less path:
#     ``registry.ensure_connected(name, "ws:<workspace_id>")`` resolves the
#     workspace adapter (the #1445 path; ``pocket_id=None`` semantically — we
#     never pass a ``pocket:<id>`` scope key), then ``adapter.execute(action,
#     params)`` returns an ``ActionResult`` whose ``.data`` carries the records.
#   * This runs on the TENANT'S LOCAL RUNTIME. The cloud LOCAL-exec seam returns
#     HTTP 503 ``connector.local_agent_unavailable`` for local-mode actions, so
#     the orchestrator drives the local ``ConnectorRegistry`` directly.
#
# Slice 1 is a DETERMINISTIC digest (no LLM refine). The optional draft-refine
# pass (cleaning the ontology with an agent) is gated behind ``opts.refine`` and,
# when enabled, MUST use the on-box / tenant model — never route tenant raw data
# to a cloud model. It is not wired in slice 1 (the deterministic digest is
# accepted); ``opts.refine=True`` raises so a caller can't silently get an
# unrefined draft while believing it was refined.
#
# Async orchestration; depends on the OSS connector registry/adapter surface
# (duck-typed for testability) + the SZD-3 digester. No DB writes — the draft is
# returned for a downstream gate to review.

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pocketpaw_ee.discovery.digester import Digester, StructuredShapeDigester
from pocketpaw_ee.discovery.models import OntologyDraft

logger = logging.getLogger(__name__)

# Default per-connector sampling cap. A discovery run only needs enough records
# to infer shapes/keys/links — not the whole dataset. Bounded so a large
# connector can't blow up memory or wall-clock on the tenant box.
DEFAULT_SAMPLE_CAP = 200

# Trust levels whose actions are safe to invoke unattended during discovery.
# "auto" actions are reads the agent may run without asking; anything that
# mutates is "confirm" and must never fire on a sampling pass.
_READ_TRUST_LEVELS = frozenset({"auto"})

# HTTP methods that read rather than mutate. Discovery only ever reads.
_READ_METHODS = frozenset({"GET", "HEAD"})


@dataclass(frozen=True)
class ReadAction:
    """A single connector read to sample during discovery.

    ``type_name`` labels the records this action returns so the digester can
    group by type (one action → one logical type). When ``None``, the connector
    name is used as the type label.
    """

    action: str
    params: dict[str, Any] = field(default_factory=dict)
    type_name: str | None = None


@dataclass(frozen=True)
class DiscoveryRunOptions:
    """Knobs for a discovery run.

    * ``sample_cap`` — max records kept per connector (the "N" in "N of M").
    * ``read_actions`` — explicit ``{connector_id: [ReadAction, ...]}`` override.
      When a connector is absent here, the orchestrator auto-selects its
      read-shaped actions (GET/HEAD + ``auto`` trust level) from the adapter's
      ``actions()`` schema. Supplying this is the precise path; auto-select is
      the convenience default.
    * ``allowed_connector_ids`` — permission filter. When set, only these
      connector ids are sampled (the rest are skipped + noted). When ``None``,
      all requested connectors are sampled — connector-level permission
      enforcement isn't merged yet, so we degrade gracefully to allow-all and
      record that in the draft meta.
    * ``refine`` — request the optional on-box LLM refine pass. Not wired in
      slice 1; setting it ``True`` raises rather than silently returning an
      unrefined draft.
    """

    sample_cap: int = DEFAULT_SAMPLE_CAP
    read_actions: dict[str, list[ReadAction]] = field(default_factory=dict)
    allowed_connector_ids: frozenset[str] | None = None
    refine: bool = False


@runtime_checkable
class _Adapter(Protocol):
    """The slice of a connector adapter the orchestrator touches.

    Deliberately narrow + duck-typed so a test can supply a mock without the
    full ``ConnectorProtocol`` surface. ``actions()`` returns ``ActionSchema``
    objects; ``execute()`` returns an ``ActionResult`` whose ``.data`` holds
    records.
    """

    async def actions(self) -> Sequence[Any]: ...

    async def execute(self, action: str, params: dict[str, Any]) -> Any: ...


@runtime_checkable
class _Registry(Protocol):
    """The slice of the connector registry the orchestrator touches.

    Only ``ensure_connected`` — the workspace-scoped resolve path. Duck-typed so
    tests inject a mock and assert the scope key is ``ws:<workspace_id>`` (never
    a ``pocket:<id>`` key, never ``sync``).
    """

    async def ensure_connected(self, connector_name: str, scope_key: str) -> Any | None: ...


def _default_registry() -> _Registry:
    """Lazily build the local-runtime ConnectorRegistry.

    Mirrors ``src/pocketpaw/api/v1/connectors.py:_get_registry`` — the same
    static registry rooted at ``connectors/``. Imported lazily so importing this
    module never drags the registry in (and so tests can inject a mock instead).
    """
    from pathlib import Path

    from pocketpaw.connectors.registry import ConnectorRegistry

    return ConnectorRegistry(Path("connectors"))


def _records_from_data(data: Any) -> list[Any]:
    """Normalize an ActionResult ``.data`` payload into a list of records.

    Adapters return reads in a few shapes: a bare ``list[dict]`` (most reads), a
    single ``dict`` (a one-record read), or a wrapped envelope like
    ``{"items": [...]}`` / ``{"results": [...]}`` / ``{"data": [...]}``. We
    unwrap the common envelopes and otherwise treat a dict as one record. Empty
    / ``None`` → no records (so an empty connector yields no rows, not a crash).
    """
    if data is None:
        return []
    if isinstance(data, Mapping):
        for key in ("items", "results", "records", "rows", "data"):
            inner = data.get(key)
            if isinstance(inner, Sequence) and not isinstance(inner, (str, bytes)):
                return list(inner)
        # A lone dict is a single record.
        return [data]
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        return list(data)
    # Scalar / unknown — no usable records.
    return []


def _is_read_action(schema: Any) -> bool:
    """True when an ``ActionSchema`` looks like a safe read.

    Read = GET/HEAD method AND ``auto`` trust level. Anything else (POST,
    ``confirm``) can mutate and must never fire on a discovery pass. Tolerant of
    enum-or-string fields so a mock can use plain strings.
    """
    method = str(getattr(schema, "method", "GET")).upper()
    trust = str(getattr(schema, "trust_level", "confirm")).lower()
    # StrEnum stringifies as "TrustLevel.auto" in some reprs; normalize to value.
    trust = trust.rsplit(".", 1)[-1]
    return method in _READ_METHODS and trust in _READ_TRUST_LEVELS


class DiscoveryRun:
    """Orchestrates one sovereign zero-setup discovery run.

    ``run(workspace_id, connector_ids, opts)`` →
      1. resolve each connector via the workspace-scoped path
         (``ensure_connected(name, "ws:<workspace_id>")`` — pocket-less);
      2. sample up to ``opts.sample_cap`` records/connector via
         ``adapter.execute()`` over its read-shaped actions;
      3. group records by type and run the ``StructuredShapeDigester``;
      4. return an ``OntologyDraft`` labelled "based on N of M".

    Degrades gracefully: an unresolvable / empty connector contributes no rows;
    an all-empty run returns an empty draft (no crash). No DB writes — a
    downstream gate reviews the draft.
    """

    def __init__(
        self,
        registry: _Registry | None = None,
        digester: Digester | None = None,
    ) -> None:
        # Injected for tests; defaults to the local-runtime registry + the SZD-3
        # structured digester. The registry is built lazily so a mock-injected
        # run never touches the filesystem.
        self._registry = registry
        self._digester: Digester = digester or StructuredShapeDigester()

    @property
    def registry(self) -> _Registry:
        if self._registry is None:
            self._registry = _default_registry()
        return self._registry

    async def run(
        self,
        workspace_id: str,
        connector_ids: Iterable[str],
        opts: DiscoveryRunOptions | None = None,
    ) -> OntologyDraft:
        opts = opts or DiscoveryRunOptions()
        if opts.refine:
            # The on-box refine pass is not wired in slice 1. Fail loud rather
            # than hand back a deterministic draft while a caller believes it
            # was LLM-refined. When it lands it MUST use the tenant/on-box model.
            raise NotImplementedError(
                "DiscoveryRun refine pass is not implemented in slice 1; the "
                "deterministic digest is the slice-1 contract. When wired, the "
                "refine pass must run on the on-box / tenant model — never a "
                "cloud model on tenant raw data."
            )

        requested = [c for c in connector_ids]
        total_connectors = len(requested)

        # Permission gate. No connector-level enforcement is merged yet, so when
        # the caller doesn't pass an allow-list we sample all requested
        # connectors and record the degraded posture in the draft meta.
        permission_enforced = opts.allowed_connector_ids is not None
        if permission_enforced:
            allowed = opts.allowed_connector_ids or frozenset()
            permitted = [c for c in requested if c in allowed]
            skipped = [c for c in requested if c not in allowed]
        else:
            permitted = list(requested)
            skipped = []

        scope_key = f"ws:{workspace_id}"
        grouped: dict[str, list[Any]] = {}
        per_connector: dict[str, dict[str, Any]] = {}
        sampled_connectors = 0

        for connector_id in permitted:
            # WORKSPACE-SCOPED, POCKET-LESS resolve — the #1445 path. NEVER
            # ensure_connected(name, "pocket:<id>") and NEVER adapter.sync().
            adapter = await self.registry.ensure_connected(connector_id, scope_key)
            if adapter is None:
                per_connector[connector_id] = {"status": "unresolved", "records": 0}
                logger.info(
                    "discovery: connector %s unresolved for %s — skipping",
                    connector_id,
                    scope_key,
                )
                continue

            read_actions = await self._read_actions_for(connector_id, adapter, opts)
            if not read_actions:
                per_connector[connector_id] = {"status": "no_read_action", "records": 0}
                logger.info(
                    "discovery: connector %s exposes no read-shaped action — skipping",
                    connector_id,
                )
                continue

            connector_records = await self._sample_connector(
                connector_id, adapter, read_actions, opts.sample_cap, grouped
            )
            sampled_connectors += 1
            per_connector[connector_id] = {
                "status": "sampled" if connector_records else "empty",
                "records": connector_records,
            }

        draft = self._digester.digest(
            grouped,
            connector_meta={
                "workspace_id": workspace_id,
                "source": "discovery_run",
            },
        )

        # Provenance — the "based on N of M" label + the per-connector roll-up.
        draft.meta["discovery"] = {
            "workspace_id": workspace_id,
            "sampled_connectors": sampled_connectors,
            "total_connectors": total_connectors,
            "label": f"based on {sampled_connectors} of {total_connectors}",
            "sample_cap": opts.sample_cap,
            "permission_enforced": permission_enforced,
            "skipped_by_permission": skipped,
            "connectors": per_connector,
        }
        return draft

    async def _read_actions_for(
        self,
        connector_id: str,
        adapter: _Adapter,
        opts: DiscoveryRunOptions,
    ) -> list[ReadAction]:
        """Resolve the read actions to sample for one connector.

        Explicit ``opts.read_actions`` wins (the precise path). Otherwise
        auto-select the adapter's read-shaped actions (GET/HEAD + ``auto`` trust)
        from its ``actions()`` schema — the safe-reads-only convenience default.
        """
        explicit = opts.read_actions.get(connector_id)
        if explicit:
            return list(explicit)

        try:
            schemas = await adapter.actions()
        except Exception as exc:  # noqa: BLE001 — a broken adapter can't block the run
            logger.warning("discovery: actions() failed for %s: %s", connector_id, exc)
            return []

        out: list[ReadAction] = []
        for schema in schemas or []:
            if not _is_read_action(schema):
                continue
            name = getattr(schema, "name", None)
            if not name:
                continue
            out.append(ReadAction(action=str(name), type_name=str(name)))
        return out

    async def _sample_connector(
        self,
        connector_id: str,
        adapter: _Adapter,
        read_actions: Sequence[ReadAction],
        sample_cap: int,
        grouped: dict[str, list[Any]],
    ) -> int:
        """Sample up to ``sample_cap`` records across a connector's read actions.

        Records land in ``grouped[type_label]`` so the digester can infer one
        type per action. The cap is enforced across ALL of the connector's
        actions combined (the "N per connector" budget). Returns the count
        sampled from this connector.
        """
        budget = max(0, sample_cap)
        taken = 0
        for read in read_actions:
            if taken >= budget:
                break
            try:
                result = await adapter.execute(read.action, dict(read.params))
            except Exception as exc:  # noqa: BLE001 — one bad action can't kill the run
                logger.warning(
                    "discovery: execute(%s) failed on %s: %s",
                    read.action,
                    connector_id,
                    exc,
                )
                continue

            if not getattr(result, "success", False):
                logger.info(
                    "discovery: execute(%s) on %s returned failure: %s",
                    read.action,
                    connector_id,
                    getattr(result, "error", None),
                )
                continue

            records = _records_from_data(getattr(result, "data", None))
            if not records:
                continue

            remaining = budget - taken
            chunk = records[:remaining]
            type_label = read.type_name or connector_id
            grouped.setdefault(type_label, []).extend(chunk)
            taken += len(chunk)

        return taken


__all__ = [
    "DEFAULT_SAMPLE_CAP",
    "DiscoveryRun",
    "DiscoveryRunOptions",
    "ReadAction",
]
