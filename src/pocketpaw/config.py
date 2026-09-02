"""Configuration management for PocketPaw.

Changes:
  - 2026-09-01 (feat/scale-concurrency-knobs): Added ``agent_pool_max_instances``
    (default 20), ``session_warm_max_per_tenant`` (default 8) and
    ``session_warm_max_global`` (default 64) — the three agent-tier ceilings that
    were previously HARDCODED constructor defaults reachable only by editing
    ``pool.py`` / ``session_supervisor.py``, because both singletons are built with
    no arguments (``AgentPool()``, ``SessionSupervisor()``). Every default is the
    literal that was already in force, so no existing deploy changes behaviour;
    they exist so a multi-user deploy can raise them from env. Note these are
    PER-PROCESS ceilings — they bound one web or worker process, and replicas
    multiply them. Distinct from ``max_concurrent_conversations`` below, which
    gates only the OSS channel-adapter loop, and from ``POCKETPAW_ARQ_MAX_JOBS``,
    which is the cluster-wide arq ceiling.
  - 2026-08-03 (PA-9): Re-measured ``prompt_pocket_summary_only``'s payoff against
    the live layer and corrected its description. The flag saves ~1,631 chars/turn
    (3,240 -> 1,609 on a 300-widget pocket), not the ~39.6k the old note implied —
    PA-8a's ``_WIDGET_SUMMARY_MAX_CHARS`` had already bounded the block, so the
    dramatic figure described behaviour that no longer exists. Default is
    unchanged; only the docstring was wrong.
  - 2026-08-03 (PA-8a): Added ``prompt_pocket_summary_only`` (default False, env
    POCKETPAW_PROMPT_POCKET_SUMMARY_ONLY) — takes the bulk widget dump out of
    the channel prompt's ``<current-pocket>`` block, leaving the pocket id, the
    name, the widget COUNT, a snapshot stamp and the standing order to call
    ``get_pocket``. Default False is byte-for-byte today's block, so no deploy
    is changed by shipping it; flipping it is a config/env change, not a code
    change. Read by ``pocketpaw.prompt.channel.request.ChannelCurrentPocketLayer``.
  - 2026-07-11 (self-serve-analysis S1): Added ``fabric_analyst`` (default False,
    env POCKETPAW_FABRIC_ANALYST) — gates the Fabric transparent-analysis read
    engine (SQL GROUP BY aggregation + reasoning steps on FabricStore.query /
    POST /fabric/query). Off (default): an aggregation query is rejected
    fail-loud with FabricAnalystDisabledError -> HTTP 422
    fabric.analyst_disabled; plain queries are unaffected either way.
  - 2026-08-08 (feat/sites-js-by-default): Added
    ``sites_keep_client_bundle_default`` (default True) — a published Paw Site
    now ships its own client JavaScript UNLESS it declares otherwise. The
    per-site ``keepsClientBundle`` declaration became tri-state (``None`` =
    undeclared → this default; ``True``/``False`` = the author's explicit
    choice, honoured in both directions), resolved at exactly one place,
    ``sites/service.py:publish_pocket``. Note the default silences the
    build-time resting-visibility smoke gate for undeclared sites — see the
    Field description for the full tradeoff.
  - 2026-07-11 (feat/external-alerting-c2c3): Added ``automation_evaluator_autostart``
    (default True, env POCKETPAW_AUTOMATION_EVALUATOR_AUTOSTART) — the OSS
    always-on automation switch. When on (default), the background
    AutomationEvaluator starts at dashboard boot so threshold rules fire without
    a manual POST /automations/evaluator/start. A new default-ON flag is safe: a
    fresh install with no enabled rules just sleeps.
  - 2026-07-11 (FST-8 — divergence report + docs): Refreshed the
    ``fabric_source_truth_mode`` Field description — shadow/enforce semantics
    SHIPPED in FST-3..7 (the FST-1 "RESERVED / INERT" wording was stale):
    shadow records statements + divergence lines while the cache stays LWW,
    enforce hands the cache to the trust-ladder resolver, and the flip to
    enforce is gated by ``python -m pocketpaw.fabric.divergence_report``.
    Description text only — no behavioral change.
  - 2026-07-10 (FST-1 — Fabric source-truth schema): Added
    ``fabric_source_truth_mode`` (Literal off|shadow|enforce, default 'off';
    POCKETPAW_FABRIC_SOURCE_TRUTH_MODE) — the three-position rollout switch for
    the Fabric source-truth chain, mirroring the ``litellm_spend_mode``
    pattern. INERT in FST-1: no code path reads it yet; only the
    fabric_statements/fabric_sources schema + append-only store CRUD exist.
    'off' is byte-for-byte today's behavior (the flat properties dict is the
    only read path); shadow/enforce semantics land in later FST slices.
  - 2026-07-10 (feat/verify-mode-shadow): Added the three-position verify
    rollout modes — ``deep_work_verify_mode`` and ``cloud_plan_verify_mode``
    (Literal off|shadow|enforce, default 'off'; env
    POCKETPAW_DEEP_WORK_VERIFY_MODE / POCKETPAW_CLOUD_PLAN_VERIFY_MODE) —
    superseding the boolean kill-switches so a real tenant can run a SAFE
    observe-only SHADOW phase (verdict + judge stamps + a
    ``would_have=<done|requeued|escalated>`` telemetry line; task status
    NEVER touched) before anyone risks ENFORCE. Resolved by
    ``effective_deep_work_verify_mode()`` /
    ``effective_cloud_plan_verify_mode()`` (mirrors
    ``effective_spend_mode()``): a non-'off' mode wins outright; mode 'off'
    + legacy bool True resolves to 'enforce' — NOT shadow — because the
    bools' SHIPPED meaning is the full acting loop, and mapping them to
    shadow would silently strip requeue/escalate from any deployment that
    already set them. The legacy bools stay for back-compat.
  - 2026-07-02 (feat/judge-shadow-1168): Added the LLM-as-judge SHADOW settings
    (J-1, issue #1168) — ``deep_work_verify_judge_shadow_enabled`` (default
    False; when True AND deep_work_verify_loop_enabled is on, every completing
    deep_work task is ALSO scored by the LlmJudgeVerdictProvider and the
    verdict stamped observe-only on ``metadata["verify_judge_verdict"]`` — it
    NEVER drives requeue/escalate; the deterministic verdict alone acts),
    ``deep_work_verify_judge_model`` (default "haiku" — the ``claude`` CLI
    ``--model`` alias for the cheap judge tier),
    ``deep_work_verify_judge_timeout_seconds`` (default 60 — the CLI
    subprocess timeout) and ``deep_work_verify_judge_confidence_floor``
    (default 0.75 — below it the judge abstains to UNKNOWN). Env:
    POCKETPAW_DEEP_WORK_VERIFY_JUDGE_* .
  - 2026-07-02 (feat/svl-5-cloud-verify): Added the CLOUD planner-terminal
    Self-Verifying Loop flags (SVL-5) — ``cloud_plan_verify_loop_enabled``
    (default False; kill-switch — when False cloud plan tasks auto-complete
    exactly as before) and ``cloud_plan_verify_max_requeues`` (default 2; the
    verify-requeue bound, SEPARATE from any error-retry budget). Mirrors the
    deep_work_verify_* pair at the ee/cloud planner terminal
    (``_execute_ready_plan_tasks`` → ``_run_one``). Env:
    POCKETPAW_CLOUD_PLAN_VERIFY_* .
  - 2026-06-30 (feat/billing-quota-enforcement, chunk 4): Expanded the
    ``billing_enforced`` field docstring — when the flag is on the run-start 402
    hard-block now covers TWO conditions (was: balance <= 0 only): balance <= 0
    (credits.insufficient) AND month-to-date spend >= the per-plan monthly credit
    ceiling (credits.quota_exceeded), enforced at run start across both the chat
    HTTP path and the worker/executor. No logic change — the gate was wired in
    chunk 3; this only documents it. Kept the "default False -> OSS/self-host
    unaffected" note.
  - 2026-06-28 (AW-7 template gate deny-on-no-match): Added
    ``instinct_template_default_deny`` (default False, env
    POCKETPAW_INSTINCT_TEMPLATE_DEFAULT_DENY) — the host-wide default for the
    TEMPLATE-level deny-by-default. When a template is BOUND to a pocket but
    declares NO rule matching a MUTATING action, the template gate previously
    returned EXECUTE (proceed). With this flag ON, that no-rule-match case
    parks the write for human approval (PENDING_APPROVAL) instead; READS
    (read_only / GET / HEAD actions) still proceed ungated. OFF by default so
    day-one behavior is unchanged. A per-workspace override field
    (``instinct_template_default_deny`` on the workspace document; null = use
    this global default) is resolved exactly like
    ``instinct_approval_level``.
  - 2026-06-28 (AW-1 connector egress guard): Added
    ``connector_egress_guard`` (default False; env
    POCKETPAW_CONNECTOR_EGRESS_GUARD) — the kill-switch for routing
    DirectREST connector HTTP through the SSRF egress guard
    (``assert_egress_allowed`` + the pinned-IP transport). OFF by default so
    flipping it on per-deployment closes the connector SSRF bypass without
    risking live connectors in the same change. The existing
    ``POCKETPAW_ALLOW_INTERNAL_URLS`` flag stays the dev escape that permits
    internal/loopback hosts when the guard is on.
  - 2026-06-28 (fix/billing-checkout-sessions): Added ``dodo_checkout_return_base``
    (default "", env POCKETPAW_DODO_CHECKOUT_RETURN_BASE) — the fallback base URL a
    Dodo subscription checkout session returns the buyer to after pay / cancel when
    the /billing/subscribe request carries no Origin (or usable Referer) header.
    Return urls become ``{base}/settings/billing?checkout=success|cancel``; empty
    default omits the redirect when no origin is available.
  - 2026-06-26 (WU-F billing cutover): Added ``litellm_spend_mode``
    (Literal off|shadow|live, default 'off'; POCKETPAW_LITELLM_SPEND_MODE) — the
    three-position billing-cutover switch that supersedes the
    ``litellm_spend_ingest_enabled`` bool. 'off' keeps BC-3 per-run metering as
    today; 'shadow' runs a read-only per-tenant compare (litellm spend vs BC-3
    ledger, ZERO debits) that records a reconciliation row; 'live' makes LiteLLM
    the sole meter (proxy-spend sweep debits + BC-3 sweep gated off). The legacy
    bool is kept for back-compat and resolved by ``effective_spend_mode()`` — an
    existing ``POCKETPAW_LITELLM_SPEND_INGEST_ENABLED=true`` maps to 'shadow' (NOT
    'live') while the new mode is left at 'off', so deploying WU-F can never
    auto-flip an old bool-setter into live billing; 'live' requires an explicit
    POCKETPAW_LITELLM_SPEND_MODE=live.
  - 2026-06-26: Added the L2 cross-backend harness-failover settings (MCG-10) —
    ``backend_failover_enabled`` (default False; kill-switch — when False the
    new ``AgentRouter.run_with_failover`` behaves exactly like ``run`` and no
    harness switch ever happens) and ``backend_failover_chain`` (default
    ["claude_agent_sdk", "codex_cli", "opencode"]; the ordered list of agent
    HARNESSES to try when the whole primary lane is down — distinct from L1's
    LiteLLM model/account failover, which cannot escape a provider-wide
    outage). Only a lane-level failure (overload/unavailable/auth that
    persists after the backend's own retries) before any token is streamed
    triggers a switch; each harness is tried at most once. Env:
    POCKETPAW_BACKEND_FAILOVER_ENABLED / POCKETPAW_BACKEND_FAILOVER_CHAIN
    (JSON list). The EE cloud run path wiring is a follow-up — this ships the
    mechanism + the OSS hook only.
  - 2026-06-24: Added ``dodo_plan_products`` (default {}, env
    POCKETPAW_DODO_PLAN_PRODUCTS as a JSON object) — the BC-7 mapping of plan
    tier key -> Dodo recurring product id. ``subscribe`` reads it to open a
    recurring checkout; the subscription webhook reverses it (product_id ->
    plan key) to know which tier renewed. A before-validator degrades a
    malformed env string to {} so a typo can't crash settings load.
  - 2026-08-21: Added ``dodo_site_products`` (default {}, env
    POCKETPAW_DODO_SITE_PRODUCTS as a JSON object) — the PER-SITE analogue of
    ``dodo_plan_products``. ``site_plans._dodo_product_for`` has read
    ``getattr(get_settings(), "dodo_site_products", None)`` since BC-9 and the
    field it reads was never declared, so the getattr always returned None, every
    site tier's ``dodo_product_id`` was None, and no per-site plan could be
    purchased on any deployment however it was configured. Declaring it makes the
    env var mean something for the first time.
  - 2026-08-26: Added ``dodo_site_addons`` (default {}, env
    POCKETPAW_DODO_SITE_ADDONS as a JSON object) — the tier -> Dodo ADD-ON id map
    that bills a paid site as a LINE on the workspace subscription instead of a
    separate per-site subscription. ``dodo_site_products`` is deliberately kept
    alongside it: per-site subscriptions are live in production and their renewal
    and cancel webhooks still route through the product map.
  - 2026-09-02: Added ``billing_dunning_grace_days`` (default 7, env
    ``POCKETPAW_BILLING_DUNNING_GRACE_DAYS``) — how long a workspace keeps its
    paid plan after a renewal payment fails. A ``subscription.on_hold`` webhook
    stamps the deadline; the grace sweep revokes the plan once it passes, and a
    successful retry clears it. Configurable because the right number depends on
    the gateway's own retry schedule, and suspending a customer while the charge
    is still being recovered is worse than a few extra days of service.
  - 2026-08-21: Added ``sites_billing_enforced`` (default False, env
    ``POCKETPAW_SITES_BILLING_ENFORCED``) — the PER-SITE paywall switch, so the
    Paw Sites seams (custom-domain capability + count caps, concierge
    entitlement) can be turned on without also 402ing chat runs, seats, pockets,
    connectors, calls or uploads. Every sites seam reads ``billing_enforced OR
    sites_billing_enforced``, so the global flag keeps working exactly as
    documented and this is additive for existing tenants.
  - 2026-06-24: Added ``billing_enforced`` (default False, env
    POCKETPAW_BILLING_ENFORCED) — the BC-4 run-start hard-block flag. When
    True the cloud rejects STARTING a new chat run on a zero-or-negative
    credit balance with HTTP 402; in-flight runs are untouched. Off by
    default so OSS / self-host stay unaffected.
  - 2026-06-23: Added the deep_work Self-Verifying Loop flags (SVL-1) —
    ``deep_work_verify_loop_enabled`` (default False; kill-switch — when
    False deep_work tasks complete exactly as before) and
    ``deep_work_verify_max_requeues`` (default 2; the requeue bound read by
    SVL-2, landed here so later slices don't touch config). SVL-1 only reads
    the enable flag to stamp an observe-only OutcomeVerdict on a completing
    task. Env: POCKETPAW_DEEP_WORK_VERIFY_* .
  - 2026-06-22: Added ``discovery_sovereign_model`` (default True) — the model-lane
    sovereignty posture for discovery's categorize (F2) / refine (F3) passes.
    True (default, unchanged behavior): hard-pin the model to the on-box Ollama
    so tenant data never leaves the box. False: use the workspace's configured
    provider via ``resolve_llm_client`` — a CLOUD model is allowed (explicit
    tenant opt-in). The kb ingest/build tripwire holds regardless. Env:
    POCKETPAW_DISCOVERY_SOVEREIGN_MODEL.
  - 2026-06-21: Added ``instinct_enforce_discovered_rules`` (default False, F6) —
    when true, approved workspace-discovered Instinct rules are merged with
    template rules at the live gate and govern actions. Off by default; the
    discovered branch is dead code on the default path. A separate, narrower
    flag than ``instinct_approval_level``. Env:
    POCKETPAW_INSTINCT_ENFORCE_DISCOVERED_RULES.
  - 2026-06-18: Added the four layered/learning Instinct gate defaults —
    ``instinct_approval_level`` (default "ASK", dormant),
    ``instinct_auto_approve_threshold`` (0.9), ``instinct_dry_run_mode``
    (False), ``instinct_optimistic_ttl_seconds`` (300). Global host-wide
    defaults for the 4-lane triage router; per-workspace overrides land
    with the gate integration layer. Dormant on ship (ASK escalates
    everything). Env: POCKETPAW_INSTINCT_* .
  - 2026-06-10: Added ``belt_repo_allowlist`` — the security boundary for the
    Belt & Pulley code-change gate (BS-3). A ``belt_propose_change`` proposal's
    repo path must resolve inside one of these roots; empty defaults to the
    cwd's parent. Env: POCKETPAW_BELT_REPO_ALLOWLIST (JSON list).
  - 2026-07-01: Added ``shield_api_socket`` + ``shield_api_token`` (SEC-5) —
    the same-box shield daemon's control-API UNIX socket + Bearer token. The
    cloud ``/api/v1/security/*`` proxy reads these to reach shield; the token
    is never logged. Env: POCKETPAW_SHIELD_API_SOCKET / POCKETPAW_SHIELD_API_TOKEN.
  - 2026-06-10: Added ``loom_bin`` + ``loom_model_path`` — the codebase
    orientation (loom) MCP server settings. ``loom_model_path`` defaults
    to None, which disables the loom MCP server; set it to a built
    world-model JSON to enable orient / locate / why / what_depends_on /
    boundaries for the cloud chat agent (BS-1, Belt & Pulley stations).
  - 2026-05-26: Added ``foresight_use_skill`` — env gate for the
    ``foresight-create-sim`` bundled skill (default OFF). The SKILL.md
    still auto-installs; this flag toggles the chat-surface affordance
    only. Read by the agent prompt assembler and the paw-enterprise
    feature-flag echo. RFC 08 v1.0 wave 4.
  - 2026-05-22: Added ``source_refresh_min_interval_seconds`` (interval
    floor) and ``source_refresh_max_per_hour`` (per-pocket auto-refresh
    budget) — cost controls for pocket data-source interval / webhook
    refresh (RFC 04 M3).
  - 2026-05-22: Added ``ripple_embed_allowed_hosts`` — host allow-list
    for the Ripple ``embed`` widget's ``mode:"url"`` form (Increment 5,
    escape-hatch node + embed URL policy).
  - 2026-05-22: Added ``pocket_router_enabled`` (kill-switch) and
    ``pocket_router_min_confidence`` (cheap-tier confidence floor) for
    the pocket execution router (Increment 3).
  - 2026-07-06: Added ``sites_crew_enabled`` — when true, the /sites
    CREATE surface runs the guided authoring-crew flow (clarity gate →
    interview → design-system + assets → build) instead of the single-shot
    create preamble; default off (feat/sites-crew-create-flow, SC-crew).
  - 2026-07-18: Added ``herdr_runtime_enabled`` (kill-switch, default off),
    ``herdr_cli_path`` and ``herdr_cli_timeout_ms`` for the flagged,
    fail-open HerdrRuntime adapter over the external ``herdr`` terminal
    multiplexer (feat/herdr-runtime-adapter, HR-1).
  - 2026-05-22: Added ``auto_install_bundled_templates`` — toggles the
    boot-time mirror of built-in pocket templates into
    ``~/.pocketpaw/templates/`` (feat/bundled-templates, Increment 2a).
  - 2026-07-12: Removed ``auto_install_bundled_kb_scopes`` along with the
    bundled ``ripple-recipes`` scope — the hand-authored pattern recipes
    biased the agent's design toward fixed layouts.
  - 2026-05-21: Added ``auto_install_bundled_skills`` — toggles the
    boot-time mirror of bundled SKILL.md files.
  - 2026-04-30: Added pluggable embedding adapter settings — ``kb_vectors_enabled``,
    ``embedding_adapter``, ``embedding_dim``, ``embedding_monthly_cap_usd``,
    ``vertex_project_id``, ``vertex_location``. Stage 2.D of "Files as Knowledge".
  - 2026-04-30: Added ``kb_scopes`` (list[str]) for multi-scope KB queries.
    ``kb_scope`` (single string) is now a deprecation shim — when set and
    ``kb_scopes`` is empty, it copies forward and emits DeprecationWarning.
    Stage 1.B of "Files as Knowledge".
  - 2026-04-16: SSRF guard on URL config fields — opencode_base_url,
    litellm_api_base, openai_compatible_base_url, mem0_ollama_base_url,
    embedding_base_url, signal_api_url, mcp_client_metadata_url are now
    validated by security.url_validators.validate_external_url. Closes #703.
  - 2026-04-10: Removed old pocketclaw migration warning — fully shifted to pocketpaw.
  - 2026-04-04: Added soul_cognitive_model setting for cheaper cognitive processing.
  - 2026-03-16: Use Literal types for whatsapp_mode, tts_provider, stt_provider (#638).
  - 2026-02-17: Added health_check_on_startup field for Health Engine.
  - 2026-02-06: Secrets stored encrypted via CredentialStore; auto-migrate plaintext keys.
  - 2026-02-06: Harden file/directory permissions (700 dir, 600 files).
  - 2026-02-02: Added claude_agent_sdk to agent_backend options.
  - 2026-02-02: Simplified backends - removed 2-layer mode.
  - 2026-02-02: claude_agent_sdk is now RECOMMENDED (uses official SDK).
"""

from __future__ import annotations

import json
import logging
import os
import re
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from pocketpaw.security.url_validators import validate_external_url

# Shorthand for Settings URL fields that must be safe from SSRF (#703).
# Applies scheme + loopback/RFC1918 guards from security.url_validators.
ExternalUrl = Annotated[str, AfterValidator(validate_external_url)]

logger = logging.getLogger(__name__)


# API key validation patterns
_API_KEY_PATTERNS = {
    "anthropic_api_key": {
        "pattern": re.compile(r"^sk-ant-"),
        "example": "sk-ant-...",
        "name": "Anthropic API key",
    },
    "openai_api_key": {
        "pattern": re.compile(r"^sk-"),
        "example": "sk-...",
        "name": "OpenAI API key",
    },
    "openrouter_api_key": {
        "pattern": re.compile(r"^sk-or-v1-"),
        "example": "sk-or-v1-...",
        "name": "OpenRouter API key",
    },
    "telegram_bot_token": {
        "pattern": re.compile(r"^\d+:AA[A-Za-z0-9_-]{30,}$"),
        "example": "123456789:AAH...",
        "name": "Telegram bot token",
    },
}


def validate_api_key(field_name: str, value: str) -> tuple[bool, str]:
    """Validate a **single** API key against strict regex patterns.

    Used by the REST ``PUT /settings`` endpoint and the WS ``save_api_key``
    handler to check format *before* saving.  Returns a per-key verdict so
    the caller can surface a targeted warning.

    See also :func:`validate_api_keys` which validates *all* keys on a
    :class:`Settings` instance using looser prefix checks.

    Args:
        field_name: Settings field name (e.g., ``"anthropic_api_key"``).
        value: The raw API key string to validate.

    Returns:
        ``(True, "")`` when the format is acceptable, or
        ``(False, "<human-readable warning>")`` when it is not.
    """
    if not value or not value.strip():
        return True, ""  # Empty values are allowed (user may want to unset)

    value = value.strip()

    validator = _API_KEY_PATTERNS.get(field_name)
    if not validator:
        return True, ""  # No validation rule for this field

    if not validator["pattern"].match(value):
        return False, (
            f"{validator['name']} doesn't match expected format "
            f"(expected format: {validator['example']}). "
            f"Double-check for typos or truncation."
        )

    return True, ""


def _chmod_safe(path: Path, mode: int) -> None:
    """Set file permissions, ignoring errors on Windows."""
    try:
        path.chmod(mode)
    except OSError:
        pass


def get_config_dir() -> Path:
    """Get the config directory, creating if needed."""
    config_dir = Path.home() / ".pocketpaw"
    config_dir.mkdir(exist_ok=True)
    _chmod_safe(config_dir, 0o700)
    return config_dir


def get_config_path() -> Path:
    """Get the config file path."""
    return get_config_dir() / "config.json"


def get_token_path() -> Path:
    """Get the access token file path."""
    return get_config_dir() / "access_token"


# Telegram bot token format: numeric id + colon + alphanumeric secret
_TELEGRAM_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")


def validate_api_keys(settings: Settings) -> list[str]:
    """Validate **all** API keys on a :class:`Settings` instance (batch, loose).

    Uses simple prefix checks (not the strict regexes in :func:`validate_api_key`)
    and returns a list of human-readable warnings.  Designed for advisory use
    (e.g. ``Settings.save()`` logs warnings) — callers must **never** block a
    save based on these results.
    """
    warnings: list[str] = []
    if settings.anthropic_api_key and not settings.anthropic_api_key.startswith("sk-ant-"):
        warnings.append("Anthropic API key may be invalid: expected to start with sk-ant-")
    if settings.openai_api_key and not settings.openai_api_key.startswith("sk-"):
        warnings.append("OpenAI API key may be invalid: expected to start with sk-")
    if settings.telegram_bot_token and not _TELEGRAM_BOT_TOKEN_RE.fullmatch(
        settings.telegram_bot_token.strip()
    ):
        warnings.append(
            "Telegram bot token may be invalid: expected format is numeric_id:alphanumeric_secret"
        )
    return warnings


class Settings(BaseSettings):
    """PocketPaw settings with env and file support."""

    model_config = SettingsConfigDict(
        env_prefix="POCKETPAW_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,  # allow field-name assignment alongside aliases
    )

    # Telegram
    telegram_bot_token: str | None = Field(
        default=None, description="Telegram Bot Token from @BotFather"
    )
    allowed_user_id: int | None = Field(
        default=None, description="Telegram User ID allowed to control the bot"
    )

    # Agent Backend
    agent_backend: str = Field(
        default="claude_agent_sdk",
        description=(
            "Agent backend: 'claude_agent_sdk', 'openai_agents', 'google_adk', "
            "'codex_cli', 'opencode', 'copilot_sdk', 'deep_agents', or "
            "'langchain_react'. All backends support 'litellm' as a provider "
            "for open-source model access."
        ),
    )
    # backend fallback chain
    fallback_backends: list[str] = Field(
        default_factory=list,
        description=("Ordered list of fallback backends to try if the primary backend fails"),
    )

    # L2 cross-backend harness failover (MCG-10) — distinct from the generic
    # ``fallback_backends`` above. This is the harness-level escape hatch for a
    # provider-wide outage: when the whole Claude Code lane is down (an
    # Anthropic-wide overload that Claude Code's own ``--fallback-model`` cannot
    # escape, because that stays in Anthropic's capacity pool), switch to a
    # DIFFERENT backend harness. Only fires on a lane-level failure (overload /
    # unavailable / auth that survives the backend's own retries) AND only if
    # nothing was streamed to the user yet (a half-streamed turn can't be
    # replayed). Off by default so behavior is unchanged unless opted in.
    backend_failover_enabled: bool = Field(
        default=False,
        description=(
            "Enable L2 cross-backend (harness) failover. When True, "
            "AgentRouter.run_with_failover switches to the next harness in "
            "backend_failover_chain if the primary lane is down before any "
            "token is streamed. Off by default."
        ),
    )
    backend_failover_chain: list[str] = Field(
        default_factory=lambda: ["claude_agent_sdk", "codex_cli", "opencode"],
        description=(
            "Ordered list of agent HARNESSES to try on a lane-level failure "
            "(Claude Code -> Codex -> opencode). Each harness is tried at most "
            "once; the chain is consulted only when backend_failover_enabled."
        ),
    )

    # Claude Agent SDK Settings
    claude_sdk_provider: str = Field(
        default="anthropic",
        description=(
            "Provider for Claude SDK: 'anthropic', 'ollama', 'openai_compatible', or 'litellm'"
        ),
    )
    claude_sdk_model: str = Field(
        default="",
        description="Model for Claude SDK backend (empty = let Claude Code auto-select)",
    )
    claude_sdk_max_turns: int = Field(
        default=100,
        description="Max tool-use turns per query in Claude SDK (0 = unlimited)",
    )

    # OpenAI Agents SDK Settings
    openai_agents_provider: str = Field(
        default="openai",
        description=(
            "Provider for OpenAI Agents: 'openai', 'ollama', 'openai_compatible', or 'litellm'"
        ),
    )
    openai_agents_model: str = Field(
        default="", description="Model for OpenAI Agents backend (empty = gpt-5.2)"
    )
    openai_agents_max_turns: int = Field(
        default=100, description="Max turns per query in OpenAI Agents backend (0 = unlimited)"
    )

    # Gemini CLI Settings (legacy, kept for config compat)
    gemini_cli_model: str = Field(
        default="gemini-3-pro-preview", description="Model for Gemini CLI backend (legacy)"
    )
    gemini_cli_max_turns: int = Field(
        default=100, description="Max turns per query in Gemini CLI backend (legacy, 0 = unlimited)"
    )

    # Google ADK Settings
    google_adk_provider: str = Field(
        default="google",
        description="Provider for Google ADK: 'google' or 'litellm'",
    )
    google_adk_model: str = Field(
        default="gemini-3-pro-preview", description="Model for Google ADK backend"
    )
    google_adk_max_turns: int = Field(
        default=100, description="Max turns per query in Google ADK backend (0 = unlimited)"
    )

    # Codex CLI Settings
    codex_cli_model: str = Field(default="gpt-5.3-codex", description="Model for Codex CLI backend")
    codex_cli_max_turns: int = Field(
        default=100, description="Max turns per query in Codex CLI backend (0 = unlimited)"
    )
    codex_cli_api_key: str | None = Field(
        default=None,
        description=(
            "Optional API key for the Codex CLI backend. Falls back to "
            "openai_api_key when unset; useful when the user wants Codex "
            "talking to a different account than the rest of OpenAI tooling."
        ),
    )
    codex_cli_base_url: str | None = Field(
        default=None,
        description=(
            "Optional base URL for the Codex CLI backend (sets OPENAI_BASE_URL "
            "for the codex subprocess). Lets you point Codex at an "
            "OpenAI-compatible proxy (LiteLLM, Azure, etc.) without changing "
            "the global OpenAI base URL."
        ),
    )
    codex_cli_sandbox_mode: str = Field(
        default="danger-full-access",
        description=(
            "Codex CLI sandbox_mode. Values: read-only, workspace-write, "
            "danger-full-access. Default danger-full-access because Codex's "
            "tighter sandboxes (workspace-write, read-only) rely on Linux "
            "seccomp/landlock — on Windows the sandbox can't be created, so "
            "every exec call is auto-declined with status='declined'. "
            "PocketPaw runs Codex in an ephemeral temp dir as a trusted "
            "agent that the operator already authorized; the tighter modes "
            "are only useful on Linux operator deployments that want to "
            "constrain a less-trusted agent."
        ),
    )
    codex_cli_approval_policy: str = Field(
        default="never",
        description=(
            "Codex CLI approval_policy. Values: never, on-request, "
            "on-failure, untrusted. 'never' is required for headless cloud "
            "use (no human to approve). Pair with codex_cli_sandbox_mode="
            "'danger-full-access' on Windows or anywhere the agent can't "
            "be interactively supervised."
        ),
    )

    # Herdr Runtime Settings (HR-1) — the flagged, fail-open adapter that lets
    # PocketPaw spawn and drive coding-agent terminals through the external
    # ``herdr`` binary (a terminal multiplexer for coding agents). Off by
    # default: when disabled or when herdr is absent the adapter reports itself
    # unavailable and PocketPaw keeps today's behaviour.
    herdr_runtime_enabled: bool = Field(
        default=False,
        description=(
            "Kill-switch for the HerdrRuntime adapter "
            "(``pocketpaw.agents.herdr_runtime``). When False (default) the "
            "adapter reports itself unavailable and every method raises "
            "``HerdrUnavailable`` — PocketPaw runs exactly as it does today "
            "with no herdr dependency. Flip to True (and install the ``herdr`` "
            "binary + run a headless herdr server, see the HR-2 runbook) to let "
            "consumers spawn and drive coding-agent panes through herdr. herdr "
            "is used ONLY as a separate process over its CLI — never imported "
            "or linked (it is AGPL-3.0; process-boundary use only). "
            "DEDICATED-BOX ONLY: herdr has no tenant model (it mints a flat "
            "workspace namespace with no link to a paw workspace), so on a "
            "shared box one tenant's admin could observe another's panes. This "
            "flag is therefore honoured only on a single-operator deployment — "
            "a per-tenant dedicated box, a dev machine, or a self-hosted stack. "
            "If ``POCKETPAW_REQUIRE_WORKSPACE_SCOPE`` is set (the shared "
            "multi-tenant cloud marker) the adapter REFUSES to enable "
            "regardless of this flag, logs an error, and degrades through the "
            "ordinary herdr-unavailable path."
        ),
    )
    herdr_cli_path: str | None = Field(
        default=None,
        description=(
            "Explicit path to the ``herdr`` executable. When unset (default) "
            "the adapter resolves it from PATH via ``shutil.which('herdr')``. "
            "Set this to pin a specific herdr install (e.g. the version the "
            "HR-2 runbook installs) so a stray PATH entry can't shadow it. If "
            "the path is set but not an executable file, the adapter treats "
            "herdr as unavailable (fail-safe) rather than falling back to PATH."
        ),
    )
    herdr_cli_timeout_ms: int = Field(
        default=15000,
        description=(
            "Default per-command timeout (milliseconds) for non-blocking herdr "
            "CLI calls (list/get/read/send/spawn/worktree). A command that "
            "exceeds it raises ``HerdrUnavailable`` so a wedged herdr socket "
            "can never hang PocketPaw. Blocking ``wait`` calls use their own "
            "``--timeout`` plus a small buffer, not this value."
        ),
    )

    # Copilot SDK Settings
    copilot_sdk_provider: str = Field(
        default="copilot",
        description=(
            "Provider for Copilot SDK: 'copilot', 'openai', 'azure', 'anthropic', or 'litellm'"
        ),
    )
    copilot_sdk_model: str = Field(
        default="", description="Model for Copilot SDK backend (empty = gpt-5.2)"
    )
    copilot_sdk_max_turns: int = Field(
        default=100, description="Max turns per query in Copilot SDK backend (0 = unlimited)"
    )

    # Deep Agents (LangChain/LangGraph) Settings
    deep_agents_model: str = Field(
        default="anthropic:claude-sonnet-4-6",
        description="Model for Deep Agents backend in ``provider:model`` format.",
    )
    deep_agents_max_turns: int = Field(
        default=100,
        description="Max turns per query in Deep Agents backend (0 = unlimited)",
    )
    deep_agents_disable_thinking: bool = Field(
        default=False,
        description=(
            "Ask the Deep Agents backend's chat model to skip extended "
            "thinking. Sent as a provider-shaped kwarg; providers that "
            "don't recognize the shape ignore it."
        ),
    )
    # AgentAPI Settings — drive a terminal coding agent via coder/agentapi.
    agentapi_base_url: ExternalUrl = Field(
        default="http://localhost:3284",
        description=(
            "Base URL of a running AgentAPI server (`agentapi server -- claude`). "
            "The backend borrows the wrapped CLI's OWN authentication, so it needs "
            "no provider key — which is why it is useful for development when no "
            "API key or working proxy is available. One server is ONE conversation: "
            "turns are serialized, so this is a single-user tool, not a serving path."
        ),
    )
    agentapi_timeout: int = Field(
        default=3600,
        description=(
            "Seconds to wait on an AgentAPI turn. Generous by default because the "
            "wrapped agent runs its own tool chains and can work for a long time; "
            "the previous 600s cut turns off mid-task."
        ),
    )
    # Pydantic AI Settings — in-process, dispatch-only agent backend.
    # See docs/design/drafts/2026-07-29-pydantic-ai-agent-backend-prd.md.
    pydantic_ai_model: str = Field(
        default="litellm:claude-sonnet-4-6",
        description=(
            "Model for the Pydantic AI backend in ``provider:model`` format. "
            "Defaults to the ``litellm`` provider so model access goes through "
            "the self-hosted LiteLLM proxy (RFC 11), which already owns spend "
            "logs, virtual keys and per-customer budgets — a second model layer "
            "would fork metering. A bare model name with no ``provider:`` prefix "
            "falls back to ``pydantic_ai_provider``."
        ),
    )
    pydantic_ai_provider: str = Field(
        default="auto",
        description=(
            "Provider for the Pydantic AI backend when ``pydantic_ai_model`` "
            "carries no ``provider:`` prefix. ``auto`` defers to ``llm_provider``, "
            "then to ``litellm``. One of: litellm, anthropic, openai, "
            "openai_compatible, openrouter, ollama."
        ),
    )
    pydantic_ai_timeout: int = Field(
        default=3600,
        description=(
            "Seconds the Pydantic AI backend waits on the model before giving "
            "up (0 = wait indefinitely). Replaces the OpenAI client's 600s "
            "default, which is not a sensible bound on an agent turn — a long "
            "tool chain or a reasoning model thinking between tokens trips it "
            "and the run dies mid-generation. The connect timeout stays short "
            "regardless, so a dead host still fails fast. A gateway in front of "
            "the model (LiteLLM / OpenRouter) enforces its own idle window that "
            "this cannot raise."
        ),
    )
    agent_max_output_tokens: int = Field(
        default=0,
        description=(
            "Max output tokens an agent run may request. 0 (default) sends "
            "8192, lowered to the model's documented ceiling when the pinned "
            "litellm metadata knows it; a positive value replaces the 8192; a "
            "negative value sends no cap at all. Sending nothing is not "
            "neutral — OpenRouter prices its pre-flight credit check against "
            "max_tokens and substitutes the model's own ceiling when none is "
            "given, so a short reply is refused with a 402 over a reservation "
            "nobody asked for. The metadata is a clamp rather than the source: "
            "deepseek-v4-flash advertised 8192 and then 393216 on the same day, "
            "and sending the larger number would have made that 402 six times "
            "worse. Distinct from litellm_max_tokens, which only reaches the "
            "plain-completion provider (chat titles and the like), never an "
            "agent backend."
        ),
    )
    pydantic_ai_max_turns: int = Field(
        default=100,
        description=(
            "Max model requests per run in the Pydantic AI backend (0 = "
            "unlimited). Maps to the agent's request limit, which bounds a "
            "runaway tool loop."
        ),
    )
    pydantic_ai_mcp_enabled: bool = Field(
        default=True,
        description=(
            "Attach configured MCP servers to the Pydantic AI backend. The "
            "backend holds each server open for the lifetime of the backend "
            "instance, so pydantic-ai's refcount never returns to zero and a "
            "server is started exactly once rather than respawning whenever "
            "concurrent runs briefly reach zero. Set false to drop MCP from the "
            "tool surface entirely."
        ),
    )
    pydantic_ai_instrumentation: bool = Field(
        default=False,
        description=(
            "Emit OpenTelemetry spans for Pydantic AI agent runs (model "
            "requests, tool calls, token usage, time to first chunk). Calls "
            "``logfire.configure`` once per process with "
            "``send_to_logfire='if-token-present'``, so without a LOGFIRE_TOKEN "
            "the spans stay local and reach whatever OTel exporter is already "
            "configured. Off by default. Cheap since pydantic-ai 2.17.0, which "
            "caches per-message span serialization — before that it was O(n^2) "
            "over a run's history and a long tool loop paid for it."
        ),
    )
    pydantic_ai_native_web_tools: bool = Field(
        default=False,
        description=(
            "Let the model run web search and page fetch PROVIDER-SIDE on "
            "backends that support it, instead of PocketPaw's own tools making "
            "those HTTP calls from inside the agent process. On an in-process "
            "backend serving every tenant, a bridged web fetch means our event "
            "loop does the waiting. PocketPaw's ``web_search`` / ``url_extract`` "
            "stay wired as the LOCAL fallback, so a provider without native "
            "support behaves exactly as before and the model never sees two "
            "tools for one job. Off by default: the profile that advertises "
            "native support describes the OpenAI API, and whether a LiteLLM "
            "proxy forwards the native tool to its upstream is a per-deployment "
            "question."
        ),
    )
    pydantic_ai_thinking: str = Field(
        default="default",
        description=(
            "Reasoning effort for the Pydantic AI backend: 'default' (leave the "
            "provider's own setting alone), 'off', or one of 'minimal', 'low', "
            "'medium', 'high', 'xhigh'. Maps to pydantic-ai's portable "
            "``thinking`` model setting, so it works across providers rather "
            "than needing the Anthropic or OpenAI spelling. This is the "
            "largest latency dial on a reasoning model and it is currently "
            "whatever the provider picked; 'default' keeps that, and any other "
            "value makes the choice ours."
        ),
    )
    pydantic_ai_fast_model: str = Field(
        default="",
        description=(
            "Optional cheaper/faster model in ``provider:model`` form that the "
            "Pydantic AI backend downshifts to part-way through a long run. "
            "Empty disables model selection entirely, which is the default: a "
            "run stays on ``pydantic_ai_model`` from first step to last. "
            "Requires one of the two thresholds below to be non-zero."
        ),
    )
    pydantic_ai_fast_model_after_step: int = Field(
        default=0,
        description=(
            "Downshift to ``pydantic_ai_fast_model`` once a run reaches this "
            "request step (0 = never). A long tool chain front-loads its hard "
            "judgement and spends its later steps digesting tool results, "
            "which is also where the context — and so the cost — is largest. "
            "Whether that trade is worth making is an empirical question per "
            "model, which is what the evals harness is for; this ships the "
            "mechanism, off."
        ),
    )
    pydantic_ai_fast_model_after_tokens: int = Field(
        default=0,
        description=(
            "Downshift to ``pydantic_ai_fast_model`` once a run's accumulated "
            "input tokens exceed this (0 = never). A cost ceiling rather than a "
            "step count: it turns a runaway loop into a cheap one instead of a "
            "larger bill. Applied with ``pydantic_ai_fast_model_after_step`` — "
            "whichever trips first wins."
        ),
    )
    pydantic_ai_defer_mcp_tools: bool = Field(
        default=True,
        description=(
            "Hide the Pydantic AI backend's MCP tools behind tool search "
            "instead of advertising every one of them on every model request. "
            "An ungated surface carries 134 tools whose schemas are ~30,500 "
            "tokens per request; deferring the 97 bridged ones leaves 38 on "
            "the wire for ~5,900, and the model calls ``search_tools`` to pull "
            "what it needs. Costs one extra model request per discovery, and "
            "buys little on a surface that already gates hard. "
            "On by default since 2026-08-01. It shipped off because the saving "
            "was measured but 'does this model search rather than give up' was "
            "not, and that had to be answered per model. Answered: on "
            "deepseek-v4-pro and deepseek-v4-flash, three prompts needing "
            "deferred tools (tasks / icons / create-pocket) each called "
            "``search_tools``, got the right tool back and called it. What "
            "decided the default is the shape of the cost rather than the size "
            "of it — tool schemas do NOT prompt-cache on the proxy (the "
            "caching table in ``agents/pydantic_ai.py`` covers the text "
            "prefix), so the full block is re-read on every turn including one "
            "that uses no tools at all. Measured end to end against the proxy: "
            "'hey' cost 133 tools / 32,225 schema tokens / 15.7s with this off "
            "and 37 tools / 6,594 / 7.6s with it on. Set false to go back — a "
            "model that will not search loses the deferred tools entirely, so "
            "that is the escape hatch for one that turns out not to."
        ),
    )
    pydantic_ai_defer_skills: bool = Field(
        default=True,
        description=(
            "Hide the Pydantic AI backend's skills catalog behind the agent's "
            "``load_capability`` tool instead of listing every skill's name and "
            "description in the system prompt. Measured against the proxy, the "
            "19 bundled skills are 18,717 chars (~4,679 tokens) of the system "
            "prompt; deferring drops that to 751 chars (~187), and a trivial "
            "turn from 6.3s to 2.9s. "
            "This depends on the ``reasoning_content`` echo fix in "
            "``agents/pydantic_ai.py`` (``_reasoning_echo_model_class``) and "
            "must not be enabled without it. Deferral is what reliably produces "
            "an assistant turn with no thinking part — the continuation after "
            "``load_capability`` — and DeepSeek 400s the whole request when one "
            "comes back without ``reasoning_content``. Before that fix this "
            "setting failed 4 of 4 landing-page runs on both v4-flash and "
            "v4-pro, dying after exactly one tool call; after it, 4 of 4 pass "
            "(7 to 28 tool calls each). "
            "Worth knowing the saving is smaller than "
            "``pydantic_ai_defer_mcp_tools``: tool schemas never prompt-cache "
            "on the proxy, while this catalog rides in the system prompt, which "
            "does — so mainly a cold turn or a cache miss pays for it. The "
            "downside is also sharper. A model that never calls "
            "``load_capability`` loses every skill, and skills are how pocket "
            "creation, Paw Sites and Foresight know their own procedures. Set "
            "false if a model turns out not to load them."
        ),
    )
    pydantic_ai_harness_enabled: bool = Field(
        default=True,
        description=(
            "Attach the pydantic-ai-harness capabilities (compaction, planning, "
            "tool-output limits, step persistence) to the Pydantic AI backend. "
            "Set false to run the bare agent loop — the escape hatch if a "
            "harness release regresses, since the dependency is pre-1.0 in "
            "cadence and pinned exactly."
        ),
    )
    pydantic_ai_skills_enabled: bool = Field(
        default=True,
        description=(
            "Expose PocketPaw's skills to the Pydantic AI backend via "
            "pydantic-ai-skills, using progressive disclosure — the model sees "
            "names and descriptions and pulls a skill's body only when it uses "
            "one, instead of the whole set riding in the system prompt every "
            "turn. Skills are passed programmatically from PocketPaw's own "
            "loader; directory / git / S3 discovery is not used, and the "
            "script-execution tool is excluded (dispatch-only)."
        ),
    )
    pydantic_ai_compaction_max_messages: int = Field(
        default=200,
        description=(
            "Message count above which the Pydantic AI backend compacts a run's "
            "history (sliding window + clearing old tool results). A long tool "
            "loop is what blows the context window on a dispatch-only agent."
        ),
    )
    pydantic_ai_max_tool_output_chars: int = Field(
        default=200_000,
        description=(
            "Truncate any single bridged tool result above this many characters "
            "before it re-enters the model context (0 = no limit). Guards the "
            "context against one oversized tool return. NOT a read cap on file "
            "content — a cap that a tool contract cannot satisfy is how the "
            "/code fabrication bug happened (2026-07-28); bridged tools here are "
            "dispatch-only and return summaries, not whole files."
        ),
    )
    # Pocket Specialist Settings — see docs/superpowers/specs/2026-05-09-pocket-specialist-design.md
    pocket_specialist_backend: str = Field(
        default="deep_agents",
        description=(
            "Which agent backend runs the pocket specialist's LLM work. Must be a "
            "registered backend name (deep_agents, langchain_react, claude_agent_sdk, "
            "openai_agents, google_adk, codex_cli, opencode, copilot_sdk, "
            "pydantic_ai). Default deep_agents avoids subprocess cold-start. The "
            "backend must implement ``attach_specialist_tools`` — one that raises "
            "is excluded from the eligible set (``agents/backend.py``)."
        ),
    )
    pocket_specialist_model: str = Field(
        default="anthropic:claude-haiku-4-5-20251001",
        description=(
            "Model the specialist uses for spec generation. Defaults to Haiku — "
            "the specialist's job is emitting structured rippleSpec JSON from a "
            "stable ~12k-token design-rules prompt, which Haiku handles at ~2-4x "
            "Sonnet speed with no measurable quality loss. Override with "
            "provider:model when you need creative liberty (Sonnet) or cheap "
            "self-hosted inference ('openai_compatible:deepseek-v4-pro'). Set to "
            "an empty string to fall back to the chosen backend's default "
            "*_model setting."
        ),
    )
    pocket_specialist_max_validation_retries: int = Field(
        default=3,
        description=(
            "Max draft -> validate -> revise iterations before persisting with "
            "remaining warnings. Specialist always persists; this only bounds revision."
        ),
    )
    pocket_specialist_mode: Literal["subagent", "agent"] = Field(
        default="subagent",
        description=(
            "Which adapter handles ``pocket_specialist__create`` calls. "
            "``subagent`` (default) spawns an isolated backend running the "
            "specialist's own model — the historical flow. ``agent`` uses a "
            "two-call protocol: the first call returns a draft kit (design "
            "rules digest + structural plan + widget list); the chat agent "
            "drafts the rippleSpec inline using its own model and calls back "
            "with ``spec=<draft>`` for validate-and-persist. ``agent`` mode "
            "ignores ``pocket_specialist_backend`` and ``pocket_specialist_model`` "
            "entirely — the chat agent's runtime is the LLM."
        ),
    )
    pocket_router_enabled: bool = Field(
        default=True,
        description=(
            "Kill-switch for the pocket execution router (Increment 3). When "
            "True (default) ``pocket_specialist__edit`` first runs a pure, "
            "rule-based classifier that routes a request to the cheapest "
            "capable tier — Tier 0 declarative (fire a declared source/action), "
            "Tier 1 deterministic op (apply one granular op), or Tier 2 "
            "specialist (the existing LLM flow). When False the router always "
            "escalates to Tier 2, restoring pre-router behaviour exactly — "
            "every edit invokes the specialist. Flip to False to disable the "
            "router instantly without a deploy if a Tier-0/1 verdict ever "
            "misfires."
        ),
    )
    pocket_router_min_confidence: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence floor for a cheap-tier (Tier 0 / Tier 1) routing "
            "verdict. The classifier escalates to the specialist (Tier 2) "
            "whenever its confidence in the cheap tier falls below this "
            "threshold. High by default — a wrong skip produces a broken "
            "pocket, so the router is deliberately conservative."
        ),
    )
    deep_work_verify_loop_enabled: bool = Field(
        default=False,
        description=(
            "LEGACY back-compat bool for the deep_work Self-Verifying Loop — "
            "superseded by the three-position "
            "POCKETPAW_DEEP_WORK_VERIFY_MODE switch. Kept so an existing "
            "deployment that set this bool keeps the behaviour it shipped "
            "with: when the new mode is left at its 'off' default and this "
            "bool is True, ``effective_deep_work_verify_mode()`` resolves to "
            "'enforce' (the FULL acting loop — verify, requeue, escalate), "
            "NOT 'shadow' — the bool's shipped meaning IS enforce, and "
            "mapping it to shadow would silently weaken a deploy that "
            "already relies on requeue/escalate. Ignored once the mode is "
            "set to any non-'off' value. Set via "
            "POCKETPAW_DEEP_WORK_VERIFY_LOOP_ENABLED."
        ),
    )
    deep_work_verify_mode: Literal["off", "shadow", "enforce"] = Field(
        default="off",
        description=(
            "Three-position rollout switch for the deep_work Self-Verifying "
            "Loop (OSS Mission Control executor terminal). Supersedes the "
            "deep_work_verify_loop_enabled bool so a tenant can run a SAFE "
            "observe-only phase before verification is allowed to touch "
            "task status:\n"
            "  * 'off'     (default) — no verification; tasks complete "
            "byte-for-byte as today.\n"
            "  * 'shadow'  — the safe rollout rung. The deterministic "
            "verdict is computed and stamped (``verify_verdict``, plus a "
            "``verify_mode='shadow'`` marker and "
            "``verify_would_have=<done|requeued|escalated>`` — what enforce "
            "WOULD have decided), and the judge shadow still runs when its "
            "flag is on; but the task ALWAYS completes DONE — no requeue, "
            "no escalation, no ``verify_requeue_count``, no "
            "``verify_feedback`` growth. Pure telemetry.\n"
            "  * 'enforce' — the full loop: PARTIAL / NOT_SOLVED requeues "
            "with feedback (bounded by deep_work_verify_max_requeues) then "
            "escalates to BLOCKED — exactly the legacy bool's behaviour.\n"
            "Precedence (``effective_deep_work_verify_mode()``): a non-'off' "
            "mode wins outright; mode 'off' + legacy bool True resolves to "
            "'enforce'; otherwise 'off'. Set via "
            "POCKETPAW_DEEP_WORK_VERIFY_MODE."
        ),
    )
    deep_work_verify_max_requeues: int = Field(
        default=2,
        description=(
            "Max verify-driven requeues a single deep_work task may take "
            "before the loop stops requeuing and escalates instead. Used by "
            "the requeue slice (SVL-2); landed here so later slices don't have "
            "to touch config. SVL-1 only READS deep_work_verify_loop_enabled "
            "and ignores this bound."
        ),
    )
    deep_work_verify_judge_shadow_enabled: bool = Field(
        default=False,
        description=(
            "SHADOW switch for the LLM-as-judge verdict tier (J-1, #1168). "
            "When True AND deep_work_verify_loop_enabled is on, every "
            "completing deep_work task is ALSO scored by the "
            "LlmJudgeVerdictProvider on the same (output, success_criteria) "
            "and the judge's OutcomeVerdict is stamped observe-only on "
            "``metadata['verify_judge_verdict']`` plus one structured "
            "deterministic-vs-judge agreement log line. The judge verdict "
            "NEVER feeds the requeue/escalate decision — the deterministic "
            "verdict alone drives behaviour, exactly as with this flag off. "
            "Requires the loop flag: judge-on + loop-off runs nothing."
        ),
    )
    deep_work_verify_judge_model: str = Field(
        default="haiku",
        description=(
            "Model passed to the ``claude`` CLI via ``--model`` for the "
            "LLM-as-judge verdict call. A plain string so ops can point the "
            "judge at any alias/id the installed CLI accepts (e.g. 'haiku', "
            "'sonnet', or a full model id). Cheap tier by default — the "
            "judge is one extra model call per completed task."
        ),
    )
    deep_work_verify_judge_timeout_seconds: int = Field(
        default=60,
        description=(
            "Timeout (seconds) for the LLM-as-judge ``claude`` CLI "
            "subprocess. A hung CLI must never wedge task completion — on "
            "timeout the judge abstains with an UNKNOWN verdict (fail-safe)."
        ),
    )
    deep_work_verify_judge_confidence_floor: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence floor for an LLM-as-judge verdict. A judge decision "
            "whose self-reported confidence falls below this floor is "
            "discarded and the judge abstains with an UNKNOWN verdict — an "
            "uncertain judgment must never be recorded as a real "
            "pass/fail signal (mirrors the auto-triage _MIN_CONFIDENCE "
            "pattern)."
        ),
    )
    cloud_plan_verify_loop_enabled: bool = Field(
        default=False,
        description=(
            "LEGACY back-compat bool for the Self-Verifying Loop at the "
            "CLOUD planner terminal — superseded by the three-position "
            "POCKETPAW_CLOUD_PLAN_VERIFY_MODE switch. Kept so an existing "
            "deployment that set this bool keeps the behaviour it shipped "
            "with: when the new mode is left at its 'off' default and this "
            "bool is True, ``effective_cloud_plan_verify_mode()`` resolves "
            "to 'enforce' (the FULL acting loop — verify, requeue, "
            "escalate), NOT 'shadow' — the bool's shipped meaning IS "
            "enforce, and mapping it to shadow would silently weaken a "
            "deploy that already relies on requeue/escalate. Ignored once "
            "the mode is set to any non-'off' value. Set via "
            "POCKETPAW_CLOUD_PLAN_VERIFY_LOOP_ENABLED."
        ),
    )
    cloud_plan_verify_mode: Literal["off", "shadow", "enforce"] = Field(
        default="off",
        description=(
            "Three-position rollout switch for the Self-Verifying Loop at "
            "the CLOUD planner terminal (ee/cloud plan-task execution — the "
            "paw-enterprise product path). Supersedes the "
            "cloud_plan_verify_loop_enabled bool so a real tenant can run a "
            "SAFE observe-only phase before verification is allowed to "
            "touch task status:\n"
            "  * 'off'     (default) — no verification; plan tasks "
            "auto-complete byte-for-byte as today.\n"
            "  * 'shadow'  — the safe rollout rung. The deterministic "
            "verdict is computed and stamped on the task's ``verify`` dict "
            "(``verify.verdict``, plus ``verify.mode='shadow'`` and "
            "``verify.would_have=<done|requeued|escalated>`` — what enforce "
            "WOULD have decided); the task ALWAYS completes done with every "
            "DONE side-effect intact — no requeue, no escalation, no "
            "``verify.requeue_count``, no ``verify.feedback`` growth. Pure "
            "telemetry.\n"
            "  * 'enforce' — the full loop: PARTIAL / NOT_SOLVED requeues "
            "with feedback (bounded by cloud_plan_verify_max_requeues) then "
            "fails with ``verify.escalation_reason`` — exactly the legacy "
            "bool's behaviour.\n"
            "Precedence (``effective_cloud_plan_verify_mode()``): a "
            "non-'off' mode wins outright; mode 'off' + legacy bool True "
            "resolves to 'enforce'; otherwise 'off'. Set via "
            "POCKETPAW_CLOUD_PLAN_VERIFY_MODE."
        ),
    )
    cloud_plan_verify_max_requeues: int = Field(
        default=2,
        description=(
            "Max verify-driven requeues a single CLOUD plan task may take "
            "before the loop stops requeuing and fails the task with "
            "``verify.escalation_reason='budget_exhausted'``. SEPARATE from "
            "any error-retry budget — verify requeues and error retries "
            "never share a counter. Mirrors deep_work_verify_max_requeues "
            "at the cloud planner terminal (SVL-5)."
        ),
    )
    auto_install_bundled_skills: bool = Field(
        default=True,
        description=(
            "On dashboard startup, mirror bundled AgentSkills-format "
            "SKILL.md files from ``pocketpaw/bundled_skills/_bundled/skills/`` "
            "into ``~/.claude/skills/<name>/SKILL.md``. That destination is "
            "covered by PocketPaw's ``SkillLoader.SKILL_PATHS`` — so the "
            "skill works on the non-SDK backends (codex_cli / openai_agents / "
            "deep_agents) via the ``/<skill-name>`` slash command, and on the "
            "desktop dashboard. NOTE: this mirror is INVISIBLE to the default "
            "claude_agent_sdk backend (it runs ``setting_sources=[]`` which "
            "disables filesystem skill discovery) — that backend loads the "
            "bundled skills via ``sdk_load_bundled_skills`` instead. "
            "Idempotent — SHA-256 hash compare per file. Set ``false`` to "
            "freeze a manually-customized copy. Best-effort: pocket creation "
            "still works via the MCP tool surface even when no skill loads."
        ),
    )
    sites_crew_enabled: bool = Field(
        default=False,
        description=(
            "DEPRECATED / NO-OP (2026-07-14). The /sites CREATE surface now ALWAYS "
            "runs the guided two-phase authoring flow (clarity gate + `ask_user` "
            "chips → design system + real assets → build), regardless of this "
            "flag: the three create preambles + this gate collapsed into one "
            "always-on `_create_preamble` in "
            "`handlers/sites.py`, so nothing reads this setting anymore. Kept only "
            "so an existing ``POCKETPAW_SITES_CREW_ENABLED`` env var / config entry "
            "doesn't fail validation. Safe to remove once no deploy sets it."
        ),
    )
    sites_keep_client_bundle_default: bool = Field(
        default=True,
        description=(
            "Default for a published Paw Site's ``keepsClientBundle`` (MT-1) — "
            "does the site ship and run its OWN client JavaScript. Applies ONLY "
            "when the site made no declaration either way; a site that declares "
            "the flag explicitly wins in BOTH directions, so an author can still "
            "opt a pure-static page out by declaring ``false``. This is NOT the "
            "site's ``mode`` — a site with client JS is still ``mode='static'`` "
            "unless it binds live data, and prerendering stays on either way, so "
            "pages still serve finished HTML before any JS runs (additive "
            "hydration, not client rendering). "
            "TRADEOFF of the ``true`` default: every site ships a client bundle, "
            "including pure-static marketing pages that have no use for one, and "
            "the build-time resting-visibility smoke gate — which refuses a page "
            "whose content is invisible until JS reveals it — stops firing by "
            "default, because a site that ships JS is no longer presumed unable "
            "to reveal itself."
        ),
    )
    sdk_load_bundled_skills: bool = Field(
        default=True,
        description=(
            "Load PocketPaw's bundled skills into the claude_agent_sdk "
            "backend as a Claude Code local plugin (SDK ``plugins=`` option). "
            "This is the ONLY mechanism that reaches that backend: it runs "
            "``setting_sources=[]`` for persona isolation, which disables the "
            "SDK's ``~/.claude/skills`` discovery, so the "
            "``auto_install_bundled_skills`` mirror above does not help it. "
            "A local plugin loads regardless of setting_sources, so the "
            "bundled skills (pocket/site creation, editing, planning, "
            "foresight) become invokable via both slash command and "
            "natural-language intent without leaking the rest of ``~/.claude`` "
            "(CLAUDE.md, output styles) into the agent. Set ``false`` to keep "
            "the backend skill-free and drive everything via the per-surface "
            "MCP-tool preambles only."
        ),
    )
    auto_install_bundled_templates: bool = Field(
        default=True,
        description=(
            "On dashboard startup, mirror PocketPaw's built-in pocket "
            "templates from ``pocketpaw/bundled_templates/_bundled/<slug>/`` "
            "into ``~/.pocketpaw/templates/<slug>/``. Each template ships a "
            "``template.pocket.yaml`` (RFC 03 schema metadata) and a "
            "hand-authored ``ripple_spec.json`` skeleton. The create "
            "specialist instantiates-and-customizes a matching template "
            "instead of cold-generating a pocket — the fix for the 2-3 "
            "iteration authoring pain. Idempotent — SHA-256 hash compare "
            "per file. Set ``false`` to freeze a hand-customised template "
            "or disable the template library entirely. Template install is "
            "best-effort: pocket creation still works (the specialist "
            "cold-generates) even when no template is installed."
        ),
    )
    foresight_use_skill: bool = Field(
        default=True,
        description=(
            "Activate the ``foresight-create-sim`` bundled skill in chat. "
            "Default ON as of 2026-05-27 — the SKILL.md auto-installs to "
            "``~/.claude/skills/`` (idempotent) and the chat agent's "
            "prompt context builder + the cloud's foresight surface "
            "handler include the skill-activation hint in the preamble. "
            "Read by the agent prompt assembler "
            "(``pocketpaw.bootstrap.context_builder``) and the cloud's "
            "paw-enterprise feature-flag echo endpoint at "
            "``/api/v1/config/features``); the foresight CRUD endpoints "
            "themselves stay reachable regardless of this flag — the gate "
            "is purely a chat-surface affordance toggle. The flag is dev-"
            "grade today and tightens to a per-workspace database setting "
            "in a follow-up RFC."
        ),
    )
    deep_agents_skills: list[str] = Field(
        default_factory=list,
        description=(
            "Paths passed to deepagents `skills=` — directories or files loaded "
            "progressively by SkillsMiddleware (AGENTS.md-style). Empty disables."
        ),
    )
    deep_agents_memory: list[str] = Field(
        default_factory=list,
        description=(
            "Paths passed to deepagents `memory=` — files loaded by "
            "MemoryMiddleware for cross-thread recall. Empty disables."
        ),
    )

    # OpenCode Settings
    opencode_base_url: ExternalUrl = Field(
        default="http://localhost:4096",
        description="OpenCode server URL",
    )
    opencode_model: str = Field(
        default="",
        description="Model for OpenCode (provider/model format, e.g. anthropic/claude-sonnet-4-6)",
    )
    opencode_max_turns: int = Field(
        default=100, description="Max turns per query in OpenCode backend (0 = unlimited)"
    )

    # LiteLLM Proxy / SDK Configuration
    litellm_api_base: ExternalUrl = Field(
        default="http://localhost:4000",
        description="LiteLLM proxy server URL (used when any backend provider is set to 'litellm')",
    )
    litellm_api_key: str | None = Field(
        default=None,
        description="API key for LiteLLM proxy (the master key configured on the proxy)",
    )
    litellm_model: str = Field(
        default="",
        description=(
            "Default model for LiteLLM. Use provider/model format for direct mode "
            "(e.g. 'anthropic/claude-sonnet-4-6', 'huggingface/meta-llama/Llama-3-70b') "
            "or a model alias defined in LiteLLM proxy config.yaml"
        ),
    )
    litellm_max_tokens: int = Field(
        default=0,
        description="Max output tokens for LiteLLM models (0 = provider default)",
    )

    # LLM Configuration
    llm_provider: str = Field(
        default="auto",
        description=(
            "LLM provider: 'auto', 'ollama', 'openai', 'anthropic', "
            "'openai_compatible', 'gemini', 'litellm'"
        ),
    )
    ollama_host: str = Field(default="http://localhost:11434", description="Ollama API host")
    ollama_model: str = Field(default="llama3.2", description="Ollama model to use")
    openai_compatible_base_url: ExternalUrl = Field(
        default="",
        description="Base URL for OpenAI-compatible endpoint (LiteLLM, OpenRouter, vLLM, etc.)",
    )
    openai_compatible_api_key: str | None = Field(
        default=None, description="API key for OpenAI-compatible endpoint"
    )
    openai_compatible_model: str = Field(
        default="", description="Model name for OpenAI-compatible endpoint"
    )
    openai_compatible_max_tokens: int = Field(
        default=0,
        description="Max output tokens for OpenAI-compatible endpoint (0 = no limit)",
    )
    openrouter_api_key: str | None = Field(
        default=None, description="API key for OpenRouter (sk-or-v1-...)"
    )
    openrouter_model: str = Field(
        default="", description="Model slug for OpenRouter (e.g. anthropic/claude-sonnet-4-6)"
    )
    gemini_model: str = Field(default="gemini-3-pro-preview", description="Gemini model to use")
    openai_api_key: str | None = Field(default=None, description="OpenAI API key")
    openai_model: str = Field(default="gpt-5.2", description="OpenAI model to use")
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key")
    claude_code_oauth_token: str | None = Field(
        default=None,
        description=(
            "Claude Code OAuth token JSON (from `claude setup-token`). "
            "Allows Docker/headless use of Max/Pro subscription without an API key."
        ),
    )
    anthropic_model: str = Field(default="claude-sonnet-4-6", description="Anthropic model to use")

    # Memory Backend
    memory_backend: str = Field(
        default="file",
        description=(
            "Memory backend: 'file' (markdown + optional vector retrieval) or "
            "'mem0' (semantic with LLM)"
        ),
    )
    vectordb_path: str = Field(
        default="~/.pocketpaw/chroma_db", description="Storage path for the vector database"
    )
    vectordb_embedding_provider: str = Field(
        default="default",
        description=(
            "Embedding provider: 'default' (sentence-transformers), 'openai', 'huggingface'"
        ),
    )
    vectordb_embedding_model: str = Field(
        default="all-MiniLM-L6-v2",
        description=(
            "Embedding model name. For HuggingFace: any model ID"
            " (e.g. 'BAAI/bge-small-en-v1.5')."
            " For OpenAI: 'text-embedding-3-small'"
        ),
    )
    memory_use_inference: bool = Field(
        default=True, description="Use LLM to extract facts from memories (only for mem0 backend)"
    )

    # Mem0 Configuration
    mem0_llm_provider: str = Field(
        default="anthropic",
        description="LLM provider for mem0 fact extraction: 'anthropic', 'openai', or 'ollama'",
    )
    mem0_llm_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="LLM model for mem0 fact extraction",
    )
    mem0_embedder_provider: str = Field(
        default="openai",
        description="Embedder provider for mem0 vectors: 'openai', 'ollama', or 'huggingface'",
    )
    mem0_embedder_model: str = Field(
        default="text-embedding-3-small",
        description="Embedding model for mem0 vector search",
    )
    mem0_vector_store: str = Field(
        default="qdrant",
        description="Vector store for mem0: 'qdrant' or 'chroma'",
    )
    mem0_ollama_base_url: ExternalUrl = Field(
        default="http://localhost:11434",
        description="Ollama base URL for mem0 (when using ollama provider)",
    )
    mem0_auto_learn: bool = Field(
        default=True,
        description="Automatically extract facts from conversations into long-term memory",
    )
    file_auto_learn: bool = Field(
        default=False,
        description="Auto-extract facts from conversations for file memory backend (uses Haiku)",
    )
    file_vector_enabled: bool = Field(
        default=False,
        description=(
            "Enable vector indexing and semantic retrieval for file memory backend "
            "(opt-in). Also enables knowledge graph extraction with conservative "
            "regex patterns and heuristic filtering."
        ),
    )
    vector_store: str = Field(
        default="sqlite-vec",
        description="Vector store for file memory backend: 'sqlite-vec', 'chromadb', or 'qdrant'",
    )
    embedding_provider: str = Field(
        default="ollama",
        description="Embedding provider for file memory backend (default: ollama)",
    )
    embedding_model: str = Field(
        default="nomic-embed-text",
        description="Embedding model for file memory semantic retrieval",
    )
    embedding_base_url: ExternalUrl = Field(
        default="http://localhost:11434",
        description="Embedding provider base URL (for ollama)",
    )

    # Session History Compaction
    compaction_recent_window: int = Field(
        default=10, gt=0, description="Number of recent messages to keep verbatim"
    )
    compaction_char_budget: int = Field(
        default=16000, gt=0, description="Max total chars for compacted history"
    )
    compaction_summary_chars: int = Field(
        default=300, gt=0, description="Max chars per older message one-liner extract"
    )
    compaction_llm_summarize: bool = Field(
        default=True,
        description="Use Haiku to summarize older messages for better context",
    )

    # Tool Policy
    tool_profile: str = Field(
        default="full", description="Tool profile: 'minimal', 'coding', or 'full'"
    )
    tools_allow: list[str] = Field(
        default_factory=list, description="Explicit tool allow list (merged with profile)"
    )
    tools_deny: list[str] = Field(
        default_factory=list, description="Explicit tool deny list (highest priority)"
    )
    tool_output_char_cap: int = Field(
        default=12000,
        gt=0,
        description=(
            "Max characters a single tool result may add to agent context. "
            "Oversized results are truncated (head+tail, or a salient-lines "
            "extract for test/lint output) before reaching the LLM."
        ),
    )

    # Discord
    discord_bot_token: str | None = Field(default=None, description="Discord bot token")
    discord_allowed_guild_ids: list[int] = Field(
        default_factory=list, description="Discord guild IDs allowed to use the bot"
    )
    discord_allowed_user_ids: list[int] = Field(
        default_factory=list, description="Discord user IDs allowed to use the bot"
    )
    discord_allowed_channel_ids: list[int] = Field(
        default_factory=list, description="Discord channel IDs the bot is restricted to"
    )
    discord_conversation_channel_ids: list[int] = Field(
        default_factory=list,
        description="Discord channels where the bot participates in group conversation",
    )
    discord_conversation_all_channels: bool = Field(
        default=False,
        description="Enable conversation mode in all server channels (overrides channel list)",
    )
    discord_conversation_exclude_channel_ids: list[int] = Field(
        default_factory=list,
        description="Channel IDs excluded from conversation mode (e.g. announcements)",
    )
    discord_bot_name: str = Field(
        default="Paw", description="Display name used by the bot in conversation"
    )
    discord_status_type: str = Field(
        default="online", description="Discord bot status: online, idle, dnd, invisible"
    )
    discord_activity_type: str = Field(
        default="", description="Discord bot activity: playing, watching, listening, competing"
    )
    discord_activity_text: str = Field(default="", description="Discord bot activity text")

    # Slack
    slack_bot_token: str | None = Field(
        default=None, description="Slack Bot OAuth token (xoxb-...)"
    )
    slack_app_token: str | None = Field(
        default=None, description="Slack App-Level token for Socket Mode (xapp-...)"
    )
    slack_allowed_channel_ids: list[str] = Field(
        default_factory=list, description="Slack channel IDs allowed to use the bot"
    )

    # WhatsApp
    whatsapp_mode: Literal["", "personal", "business"] = Field(
        default="",
        description="WhatsApp mode: 'personal' (QR scan via neonize) or 'business' (Cloud API)",
    )
    whatsapp_neonize_db: str = Field(
        default="",
        description="Path to neonize SQLite credential store",
    )
    whatsapp_access_token: str | None = Field(
        default=None, description="WhatsApp Business Cloud API access token"
    )
    whatsapp_phone_number_id: str | None = Field(
        default=None, description="WhatsApp Business phone number ID"
    )
    whatsapp_verify_token: str | None = Field(
        default=None, description="WhatsApp webhook verification token"
    )
    whatsapp_allowed_phone_numbers: list[str] = Field(
        default_factory=list, description="WhatsApp phone numbers allowed to use the bot"
    )

    # Web Search
    web_search_provider: str = Field(
        default="tavily",
        description=(
            "Web search provider: 'tavily', 'brave', 'parallel', or 'litellm'. "
            "'litellm' routes through the LiteLLM proxy's Search API "
            "(``POST {litellm_api_base}/v1/search``) instead of calling a "
            "vendor directly, so it reuses the proxy credentials already "
            "configured and inherits whatever search tools the operator "
            "registered there — no second key to distribute, and the proxy "
            "keeps the usage accounting. Pick which registered tool with "
            "``litellm_search_tool_name``; list them with "
            "``GET {litellm_api_base}/v1/search/tools``."
        ),
    )
    litellm_search_api_base: str | None = Field(
        default=None,
        description=(
            "Base URL for the search API when "
            "``web_search_provider='litellm'``. Defaults to "
            "``litellm_api_base``, which is right until something is chained in "
            "front of the gateway. A compression or observability proxy "
            "(Headroom, for one) intercepts ``/v1/chat/completions``, "
            "``/v1/messages`` and ``/v1/responses`` and knows nothing about "
            "``/v1/search`` — so pointing ``litellm_api_base`` at it moves the "
            "model traffic and 404s every web search. Set this to the real "
            "gateway to send search straight there while completions take the "
            "detour."
        ),
    )
    litellm_search_tool_name: str = Field(
        default="web_search",
        description=(
            "Which search tool to call when ``web_search_provider='litellm'``. "
            "These names are defined by whoever configured the proxy, not by a "
            "convention — on the reference gateway they are 'web_search' "
            "(provider parallel_ai) and 'tinyfish_web_Search' (provider "
            "tinyfish). ``GET {litellm_api_base}/v1/search/tools`` lists what a "
            "given proxy actually has; a name that is not registered fails with "
            "``Search tool '<name>' not found in router.search_tools``."
        ),
    )
    tavily_api_key: str | None = Field(default=None, description="Tavily search API key")
    brave_search_api_key: str | None = Field(default=None, description="Brave Search API key")
    parallel_api_key: str | None = Field(default=None, description="Parallel AI API key")
    # Stock photography (Paw Sites imagery — search_stock_images). Both optional;
    # with neither set, stock search returns [] and sites ship text-only.
    pexels_api_key: str | None = Field(default=None, description="Pexels stock-photo API key")
    unsplash_access_key: str | None = Field(
        default=None, description="Unsplash Access Key (stock-photo API)"
    )
    url_extract_provider: str = Field(
        default="auto", description="URL extract provider: 'auto', 'parallel', or 'local'"
    )

    # Image Generation
    google_api_key: str | None = Field(default=None, description="Google API key (for Gemini)")
    image_model: str = Field(
        default="gemini-2.5-flash-image",
        description=(
            "Google image generation model. Gemini image models "
            "(gemini-*-image) run via generateContent and work on free-tier "
            "keys; imagen-* models run via the predict endpoint, which "
            "Google restricts to paid-tier keys."
        ),
    )
    # Video Generation (Replicate HTTP API — used by the /studio surface's
    # media MCP server). Env auto-derives POCKETPAW_REPLICATE_API_TOKEN /
    # POCKETPAW_FAL_API_KEY / POCKETPAW_VIDEO_MODEL.
    replicate_api_token: str | None = Field(
        default=None, description="Replicate API token (for video generation via the HTTP API)"
    )
    fal_api_key: str | None = Field(
        default=None, description="fal.ai API key (alternate media-generation provider)"
    )
    video_model: str = Field(
        default="kwaivgi/kling-v2.0",
        description="Replicate video-generation model (owner/name slug)",
    )

    # Codebase orientation (loom) — the loom binary serves an MCP server over
    # stdio that orients the cloud chat agent to a codebase (orient / locate /
    # why / what_depends_on / boundaries). Wired into the claude_agent_sdk
    # backend via CloudLoomMcpProvider. Env auto-derives POCKETPAW_LOOM_BIN /
    # POCKETPAW_LOOM_MODEL_PATH.
    loom_bin: str = Field(
        default="loom",
        description=(
            "Path to the loom binary. Resolved as: this explicit setting → "
            "PATH lookup → ~/go/bin/loom fallback. The default 'loom' relies on "
            "PATH; set an absolute path to pin a specific build."
        ),
    )
    loom_model_path: str | None = Field(
        default=None,
        description=(
            "Path to a loom world-model JSON (built via `loom build`). When "
            "unset, the loom MCP server is not registered — orientation is "
            "disabled. The binary is served as `loom mcp -model <this path>`."
        ),
    )

    # Belt & Pulley — the develop station's code-change gate. The
    # ``belt_propose_change`` MCP tool proposes a unified diff through Instinct
    # (the human approve/reject layer); on approval the executor applies it in a
    # fresh worktree and opens a PR. ``belt_repo_allowlist`` is the security
    # boundary: a proposed ``repo`` path must resolve INSIDE one of these roots,
    # so the agent can never move a diff into an arbitrary filesystem location.
    # When empty, the allowlist defaults to the current working directory's
    # parent (the workspace root that holds the project checkouts). Env auto-
    # derives POCKETPAW_BELT_REPO_ALLOWLIST (JSON list).
    belt_repo_allowlist: list[str] = Field(
        default_factory=list,
        description=(
            "Allowlisted root directories a Belt code-change proposal's repo "
            "must live under. A repo path resolving outside every root is "
            "refused. Empty → defaults to the cwd's parent (the workspace root)."
        ),
    )

    # Shield — the same-box Go security daemon (deny-by-default connector
    # egress + agent-decision control plane). shield serves a control API on a
    # UNIX socket; the cloud ``/api/v1/security/*`` router proxies it,
    # OWNER-gated, and degrades cleanly when shield is absent (a socket that is
    # unset / missing / unreachable → a typed available:false read or a 409
    # write, never a 500). The Bearer token authenticates the backend → shield
    # hop over the socket; it is NEVER logged. Env auto-derives
    # POCKETPAW_SHIELD_API_SOCKET / POCKETPAW_SHIELD_API_TOKEN.
    shield_api_socket: str = Field(
        default="/run/shield/api.sock",
        description=(
            "Filesystem path to shield's control-API UNIX socket. The cloud "
            "security proxy connects here via an httpx UDS transport. When the "
            "socket is missing or unreachable the proxy degrades to a typed "
            "'shield_not_deployed' / 'unreachable' response."
        ),
    )
    shield_api_token: str = Field(
        default="",
        description=(
            "Bearer token the cloud security proxy presents to shield over the "
            "socket. Sent as 'Authorization: Bearer <token>'. Never logged."
        ),
    )

    # Security
    bypass_permissions: bool = Field(
        default=False, description="Skip permission prompts for agent actions (use with caution)"
    )
    localhost_auth_bypass: bool = Field(
        default=True,
        description="Allow unauthenticated localhost access (disable for non-CF proxies)",
    )
    session_token_ttl_hours: int = Field(
        default=24,
        gt=0,
        description="TTL in hours for HMAC session tokens issued via /api/auth/session",
    )
    api_cors_allowed_origins: list[str] = Field(
        default_factory=list,
        description="Additional CORS origins for external clients (e.g. tauri://localhost)",
    )
    a2a_trusted_agents: list[str] = Field(
        default_factory=list,
        description="Explicitly allowed A2A agent base URLs for task delegation (prevents SSRF)",
    )
    connector_egress_guard: bool = Field(
        default=False,
        description=(
            "Route DirectREST connector HTTP through the SSRF egress guard "
            "(host allow-list, DNS pre-resolve + internal-range reject, pinned-IP "
            "transport). OFF by default — a safe-rollout kill-switch; flip on "
            "per-deployment to close the connector SSRF bypass. "
            "POCKETPAW_ALLOW_INTERNAL_URLS permits internal hosts when set."
        ),
    )
    api_rate_limit_per_key: int = Field(
        default=60,
        gt=0,
        description="Max requests per minute per API key (token-bucket capacity)",
    )
    file_jail_path: Path = Field(
        default_factory=Path.home, description="Root path for file operations"
    )
    injection_scan_enabled: bool = Field(
        default=True, description="Enable prompt injection scanning on inbound messages"
    )
    injection_scan_llm: bool = Field(
        default=False, description="Use LLM deep scan for suspicious content (requires API key)"
    )
    injection_scan_llm_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Model for LLM-based injection deep scan",
    )

    # PII Protection
    pii_scan_enabled: bool = Field(
        default=False, description="Enable PII detection and masking (opt-in)"
    )
    pii_default_action: str = Field(
        default="mask", description="Default PII action: 'log', 'mask', or 'hash'"
    )
    pii_type_actions: dict[str, str] = Field(
        default_factory=dict,
        description="Per-type PII actions, e.g. {'email': 'mask', 'ssn': 'hash'}",
    )
    pii_scan_memory: bool = Field(
        default=True,
        description="Apply PII masking before writing to memory (when pii_scan_enabled)",
    )
    pii_scan_audit: bool = Field(
        default=True, description="Apply PII masking to audit log entries (when pii_scan_enabled)"
    )
    pii_scan_logs: bool = Field(
        default=True, description="Extend log scrubber with PII patterns (when pii_scan_enabled)"
    )

    # Chat Title Generation (Haiku-backed, first-message naming)
    chat_title_generation_enabled: bool = Field(
        default=True,
        description=(
            "Auto-generate a short title for a chat from its first user message."
            " Uses a Haiku model when an Anthropic API key is configured, and"
            " falls back to a trimmed excerpt of the first message otherwise."
            " Fires a session_titled SystemEvent on completion."
        ),
    )
    chat_title_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Model used by the chat title generator (Anthropic).",
    )

    # Smart Model Routing
    smart_routing_enabled: bool = Field(
        default=False,
        description=(
            "Enable automatic model selection based on task complexity"
            " (may conflict with Claude Code's own routing)"
        ),
    )
    model_tier_simple: str = Field(
        default="claude-haiku-4-5-20251001", description="Model for simple tasks (greetings, facts)"
    )
    model_tier_moderate: str = Field(
        default="claude-sonnet-4-6",
        description="Model for moderate tasks (coding, analysis)",
    )
    model_tier_complex: str = Field(
        default="claude-opus-4-6", description="Model for complex tasks (planning, debugging)"
    )

    # Plan Mode
    plan_mode: bool = Field(default=False, description="Require approval before executing tools")
    plan_mode_tools: list[str] = Field(
        default_factory=lambda: ["shell", "write_file", "edit_file"],
        description="Tools that require approval in plan mode",
    )

    # Budget Controls
    budget_monthly_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="Monthly budget cap in USD. 0 = unlimited",
    )
    budget_warning_threshold: float = Field(
        default=0.8,
        gt=0.0,
        le=1.0,
        description="Warn when spend crosses this fraction of budget (0.8 = 80%)",
    )
    budget_auto_pause: bool = Field(
        default=True,
        description="Auto-pause agent processing when budget is exhausted",
    )
    budget_reset_day: int = Field(
        default=1,
        ge=1,
        le=28,
        description="Day of month when the budget window resets (1-28)",
    )
    per_agent_caps: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Per-agent monthly budget caps in USD. Keys are agent backend names "
            "(e.g. 'claude_agent_sdk', 'openai_agents'). "
            "0 or missing = inherit global cap. Example: {'claude_agent_sdk': 5.0}"
        ),
    )
    budget_paused: bool = Field(
        default=False,
        exclude=True,  # excluded from JSON serialization
        # validation_alias points to an unreachable key so pydantic-settings
        # never populates this field from the environment
        # (POCKETPAW_BUDGET_PAUSED is ignored at load time).
        validation_alias=AliasChoices("__budget_paused_internal__"),
        description="Internal runtime flag — set programmatically, never from env",
    )
    budget_override_usd: float | None = Field(
        default=None,
        ge=0.0,
        description="Temporary budget override cap in USD (None = no override)",
    )
    budget_override_reason: str = Field(
        default="",
        description="Reason for the active budget override",
    )
    budget_override_expires_at: str | None = Field(
        default=None,
        description="ISO timestamp when the temporary budget override expires",
    )

    # Trace retention
    trace_retention_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="How many days of trace files to keep",
    )

    # Self-Audit Daemon
    self_audit_enabled: bool = Field(default=True, description="Enable daily self-audit daemon")
    self_audit_schedule: str = Field(
        default="0 3 * * *", description="Cron schedule for self-audit (default: 3 AM daily)"
    )

    # Health Engine
    health_check_on_startup: bool = Field(
        default=True, description="Run health checks when PocketPaw starts"
    )

    # User Preferences (set during onboarding)
    user_display_name: str = Field(default="", description="User's display name")
    user_avatar_emoji: str = Field(default="🐾", description="User's chosen avatar emoji")
    theme_preference: str = Field(
        default="system", description="Theme: 'light', 'dark', or 'system'"
    )
    notifications_enabled: bool = Field(default=True, description="Enable desktop notifications")
    sound_enabled: bool = Field(default=True, description="Enable notification sounds")
    tool_notifications_enabled: bool = Field(
        default=True, description="Show notifications for tool executions"
    )
    default_workspace_dir: str = Field(
        default="", description="Default working directory for the agent"
    )

    # OAuth
    google_oauth_client_id: str | None = Field(
        default=None, description="Google OAuth 2.0 client ID"
    )
    google_oauth_client_secret: str | None = Field(
        default=None, description="Google OAuth 2.0 client secret"
    )

    # Voice/TTS
    tts_provider: Literal["openai", "elevenlabs", "sarvam"] = Field(
        default="openai", description="TTS provider: 'openai', 'elevenlabs', or 'sarvam'"
    )
    elevenlabs_api_key: str | None = Field(default=None, description="ElevenLabs API key for TTS")
    tts_voice: str = Field(
        default="alloy", description="TTS voice name (OpenAI: alloy/echo/fable/onyx/nova/shimmer)"
    )
    tts_default_voice_elevenlabs: str = Field(
        default="pNInz6obpgDQGcFmaJgB", description="ElevenLabs default voice"
    )
    voice_reply_enabled: bool = Field(
        default=True,
        description="Auto-synthesize TTS voice reply when the inbound message was a voice note",
    )
    stt_provider: Literal["openai", "sarvam", "elevenlabs"] = Field(
        default="openai", description="STT provider: 'openai', 'elevenlabs', or 'sarvam'"
    )
    stt_model: str = Field(
        default="whisper-1",
        description=(
            "STT model (whisper-1 for OpenAI, scribe_v1 for ElevenLabs, saaras:v3 for Sarvam)"
        ),
    )

    # OCR
    ocr_provider: str = Field(
        default="openai", description="OCR provider: 'openai', 'sarvam', or 'tesseract'"
    )

    # Sarvam AI
    sarvam_api_key: str | None = Field(default=None, description="Sarvam AI API subscription key")
    sarvam_tts_model: str = Field(default="bulbul:v3", description="Sarvam TTS model")
    sarvam_tts_speaker: str = Field(default="shubh", description="Sarvam TTS speaker voice")
    sarvam_tts_language: str = Field(
        default="hi-IN", description="Sarvam TTS target language (BCP-47 code)"
    )
    sarvam_stt_model: str = Field(default="saaras:v3", description="Sarvam STT model")

    # Spotify
    spotify_client_id: str | None = Field(default=None, description="Spotify OAuth client ID")
    spotify_client_secret: str | None = Field(
        default=None, description="Spotify OAuth client secret"
    )

    # Signal
    signal_api_url: ExternalUrl = Field(
        default="http://localhost:8080", description="Signal-cli REST API URL"
    )
    signal_phone_number: str | None = Field(
        default=None, description="Signal phone number (e.g. +1234567890)"
    )
    signal_allowed_phone_numbers: list[str] = Field(
        default_factory=list, description="Signal phone numbers allowed to use the bot"
    )

    # Matrix
    matrix_homeserver: str | None = Field(
        default=None, description="Matrix homeserver URL (e.g. https://matrix.org)"
    )
    matrix_user_id: str | None = Field(
        default=None, description="Matrix user ID (e.g. @bot:matrix.org)"
    )
    matrix_access_token: str | None = Field(default=None, description="Matrix access token")
    matrix_password: str | None = Field(
        default=None, description="Matrix password (alternative to access token)"
    )
    matrix_allowed_room_ids: list[str] = Field(
        default_factory=list, description="Matrix room IDs allowed to use the bot"
    )
    matrix_device_id: str = Field(default="POCKETPAW", description="Matrix device ID")

    # Microsoft Teams
    teams_app_id: str | None = Field(default=None, description="Microsoft Teams App ID")
    teams_app_password: str | None = Field(default=None, description="Microsoft Teams App Password")
    teams_allowed_tenant_ids: list[str] = Field(
        default_factory=list, description="Allowed Azure AD tenant IDs"
    )
    teams_webhook_port: int = Field(default=3978, description="Teams webhook listener port")

    # Google Chat
    gchat_mode: str = Field(
        default="webhook", description="Google Chat mode: 'webhook' or 'pubsub'"
    )
    gchat_service_account_key: str | None = Field(
        default=None, description="Path to Google service account JSON key file"
    )
    gchat_project_id: str | None = Field(
        default=None, description="Google Cloud project ID for Pub/Sub mode"
    )
    gchat_subscription_id: str | None = Field(default=None, description="Pub/Sub subscription ID")
    gchat_allowed_space_ids: list[str] = Field(
        default_factory=list, description="Google Chat space IDs allowed to use the bot"
    )

    # Generic Inbound Webhooks
    webhook_configs: list[dict] = Field(
        default_factory=list,
        description="Configured webhook slots [{name, secret, description, sync_timeout}]",
    )
    webhook_sync_timeout: int = Field(
        default=30, description="Default timeout (seconds) for sync webhook responses"
    )

    # Web Server
    web_host: str = Field(default="127.0.0.1", description="Web server host")
    web_port: int = Field(default=8888, description="Web server port")

    # A2A Protocol
    a2a_enabled: bool = Field(
        default=False,
        description="Enable the A2A Protocol remote endpoints (allow external delegates)",
    )
    a2a_agent_name: str = Field(
        default="PocketPaw",
        description="Agent name advertised in the A2A Agent Card",
    )
    a2a_agent_description: str = Field(
        default="",
        description="Agent description for A2A Agent Card (empty = default)",
    )
    a2a_agent_version: str = Field(
        default="",
        description="Agent version for A2A Agent Card (empty = auto-detect from package)",
    )
    a2a_task_timeout: int = Field(
        default=120,
        description="Timeout in seconds for A2A task processing",
    )

    # MCP OAuth
    mcp_client_metadata_url: ExternalUrl = Field(
        default="",
        description="CIMD URL for MCP OAuth (optional, for servers without dynamic registration)",
    )

    # Identity / Multi-user
    owner_id: str = Field(
        default="",
        description="Global owner identifier (e.g. Telegram user ID). Empty = single-user mode.",
    )

    # Soul Protocol
    soul_enabled: bool = Field(
        default=True,
        description="Enable soul-protocol for persistent AI identity, memory, and emotion",
    )
    soul_name: str = Field(
        default="Paw",
        description="Name for the soul identity",
    )
    soul_archetype: str = Field(
        default="The Helpful Assistant",
        description="Soul archetype (e.g. 'The Coding Expert', 'The Compassionate Creator')",
    )
    soul_persona: str = Field(
        default="",
        description="Custom persona description for the soul (empty = auto-generated)",
    )
    # TODO: soul_values and soul_ocean are not yet exposed in the dashboard UI.
    #  Add controls in a Soul settings tab when the UI is built out.
    soul_values: list[str] = Field(
        default_factory=lambda: ["helpfulness", "precision", "privacy"],
        description="Core values for the soul identity",
    )
    soul_ocean: dict[str, float] = Field(
        default_factory=lambda: {
            "openness": 0.7,
            "conscientiousness": 0.85,
            "extraversion": 0.5,
            "agreeableness": 0.8,
            "neuroticism": 0.2,
        },
        description="OCEAN Big Five personality traits (0.0-1.0)",
    )
    soul_communication: dict[str, str] = Field(
        default_factory=lambda: {"warmth": "medium", "verbosity": "low"},
        description="Communication style settings for the soul",
    )
    soul_path: str = Field(
        default="",
        description="Path to .soul file (empty = ~/.pocketpaw/soul/)",
    )
    soul_auto_save_interval: int = Field(
        default=300,
        description="Auto-save soul state interval in seconds (0 = disabled)",
    )
    soul_biorhythm: dict[str, float] = Field(
        default_factory=lambda: {
            "energy_drain_rate": 0.02,
            "mood_inertia": 0.8,
            "tired_threshold": 0.3,
            "auto_regen": 0.01,
        },
        description=(
            "Biorhythm configuration for soul energy/mood dynamics (v0.2.4+). "
            "energy_drain_rate: how fast energy depletes per interaction. "
            "mood_inertia: resistance to mood change (0-1). "
            "tired_threshold: energy level that triggers fatigue. "
            "auto_regen: passive energy recovery rate."
        ),
    )
    kb_scope: str = Field(
        default="",
        description=(
            "DEPRECATED: single-scope back-compat shim. Prefer ``kb_scopes`` "
            "(list). When ``kb_scopes`` is empty and ``kb_scope`` is set, "
            "the value is copied into ``kb_scopes`` and a DeprecationWarning "
            "is emitted. Set via POCKETPAW_KB_SCOPE."
        ),
    )
    kb_scopes: list[str] = Field(
        default_factory=list,
        description=(
            "Ordered list of kb-go scopes to query when building the agent "
            "system prompt. Each scope receives a slice of the total limit; "
            "results are concatenated under per-scope headers. Set via "
            "POCKETPAW_KB_SCOPES as a JSON array (e.g. "
            '["workspace:w1","agent:a1"]).'
        ),
    )
    kb_binary: str = Field(
        default="kb",
        description="Path to the kb binary (default: `kb` on PATH)",
    )
    kb_limit: int = Field(
        default=3,
        description="Number of top articles to inject from kb search (default: 3)",
    )
    prompt_pocket_summary_only: bool = Field(
        default=False,
        description=(
            "Keep bulk pocket widget detail OUT of the agent's system prompt. "
            "False (default) is byte-for-byte the block shipped today: the "
            "``<current-pocket>`` block carries a JSON dump of the widget "
            "summary the client posted. True renders the CHEAP half only — "
            "pocket id, name, widget count, a snapshot stamp — plus the same "
            "standing order to call ``mcp__pocketpaw_pocket__get_pocket`` for "
            "the detail, which is the tool-result path the detail belongs on. "
            "RE-MEASURED 2026-08-03 (PA-9) against the live layer: on a "
            "300-widget pocket the block is 3,240 chars / 1,092 tokens OFF and "
            "1,609 chars / 444 tokens ON, so the flag saves 648 tokens (59%) per "
            "turn — NOT the ~39.6k chars the old '~41k chars to ~1.4k' note "
            "implied. That figure described the "
            "pre-PA-8a block; _WIDGET_SUMMARY_MAX_CHARS now bounds the dump at "
            "2,000 chars before serialisation, so the block plateaus around "
            "3,240 chars from ~50 widgets upward and does not grow with pocket "
            "size. The block is per-turn (it varies, so it never sits inside a "
            "cached prefix), but the saving is now modest rather than dramatic. "
            "Read per-render by "
            "``pocketpaw.prompt.channel.request.ChannelCurrentPocketLayer``, so "
            "flipping it is a config or env change and takes effect on the next "
            "settings load — no code deploy. Set via "
            "POCKETPAW_PROMPT_POCKET_SUMMARY_ONLY."
        ),
    )
    ripple_manifest_url: str = Field(
        default="http://localhost:5174/manifest.json",
        description=(
            "URL to the Ripple UI manifest (widget specs). Defaults to the "
            "local ripple dev server while @ripple-ui/svelte is unreleased; "
            "swap to "
            "https://cdn.jsdelivr.net/npm/@ripple-ui/svelte@latest/dist/manifest.json "
            "(or any pinned version) once published."
        ),
    )
    ripple_manifest_ttl_seconds: int = Field(
        default=86400,
        description="TTL in seconds for cached Ripple manifest (default: 24h)",
    )
    ripple_catalog_gate_require_manifest: bool = Field(
        default=False,
        description=(
            "When True, the strict (agent-generation) catalog gate FAILS CLOSED "
            "if the Ripple widget manifest can't be fetched — an unverifiable "
            "spec is rejected instead of persisted. Defaults False (best-effort "
            "skip, preserving current behavior) because ripple_manifest_url "
            "defaults to the local ripple dev server, which is often down in "
            "dev; enable it on cloud/prod where the manifest URL points at a "
            "reliable CDN. Set via "
            "POCKETPAW_RIPPLE_CATALOG_GATE_REQUIRE_MANIFEST."
        ),
    )
    ripple_embed_allowed_hosts: list[str] = Field(
        default_factory=lambda: [
            "youtube-nocookie.com",
            "player.vimeo.com",
            "codepen.io",
            "codesandbox.io",
            "observablehq.com",
            "www.figma.com",
        ],
        description=(
            'Host allow-list for the Ripple `embed` widget\'s `mode:"url"` form. '
            "An `embed` URL must be https and its host must match an entry here "
            "(exact or sub-domain). Set via POCKETPAW_RIPPLE_EMBED_ALLOWED_HOSTS "
            'as a JSON array. A literal `["*"]` widens it to every host; even '
            "then loopback / private / link-local / cloud-metadata hosts stay "
            "hard-blocked. Defaults to a curated set of sandbox-friendly "
            "embed providers."
        ),
    )

    # Layered/learning Instinct gate — GLOBAL DEFAULTS (2026-06-18 design).
    # These are the host-wide defaults for the 4-lane triage router that
    # turns the binary escalate/execute Instinct gate into a learning gate.
    # They are DORMANT by default: `instinct_approval_level="ASK"` makes the
    # lane classifier always escalate, so shipping these changes zero
    # behavior. A per-workspace override (a field on the workspace document)
    # lands with the integration layer; until an admin opts a workspace into
    # "TRIAGE", the global default governs and every escalate goes to a
    # human. A support engineer setting the env var changes the default for
    # NEW workspaces only — it cannot silently upgrade existing tenants.
    instinct_approval_level: str = Field(
        default="ASK",
        description=(
            "Global default triager activation level for the layered Instinct "
            "gate: 'ASK' (dormant — every escalate goes to a human), 'TRIAGE' "
            "(triager active — auto/optimistic/batch lanes live), or 'TRUSTED' "
            "(reserved; treated as TRIAGE today). Per-workspace overrides live "
            "on the workspace document. Set via POCKETPAW_INSTINCT_APPROVAL_LEVEL."
        ),
    )
    instinct_auto_approve_threshold: float = Field(
        default=0.9,
        description=(
            "Trust-score bar (0.0-1.0) a (pocket, action) pair must reach for "
            "the AUTO/OPTIMISTIC lanes. A score below this escalates. Money- "
            "moving and DELETE actions never AUTO regardless of score (a hard "
            "blast-radius floor). Set via POCKETPAW_INSTINCT_AUTO_APPROVE_THRESHOLD."
        ),
    )
    instinct_dry_run_mode: bool = Field(
        default=False,
        description=(
            "When true, the Instinct gate routes escalating writes to the "
            "DRY_RUN lane: the write is resolved and audited but never sent to "
            "the backend (a governance rehearsal). BLOCK verdicts still block. "
            "Set via POCKETPAW_INSTINCT_DRY_RUN_MODE."
        ),
    )
    instinct_optimistic_ttl_seconds: int = Field(
        default=300,
        description=(
            "Seconds an OPTIMISTIC-lane compensation handle stays live before "
            "hard expiry. On expiry the registry fires an ALERT audit event and "
            "persists the expired handle (no heartbeat extension). Set via "
            "POCKETPAW_INSTINCT_OPTIMISTIC_TTL_SECONDS."
        ),
    )
    # AW-7 — TEMPLATE-level deny-by-default. Binding-level deny-by-default
    # (``ActionBinding.requires_instinct`` defaults True) is already live; this
    # closes the remaining hole: a template BOUND to a pocket that declares NO
    # rule matching a MUTATING action used to fall through to the template
    # gate's EXECUTE default and fire. When this flag is ON the no-rule-match
    # case parks the write for a human (PENDING_APPROVAL) instead. READS
    # (read_only / GET / HEAD actions) still proceed ungated — a read has
    # nothing to govern. DORMANT by default (False): shipping it changes zero
    # behavior. A per-workspace override field of the same name on the
    # workspace document (null = use this global default) is resolved exactly
    # like ``instinct_approval_level`` via
    # ``resolve_workspace_template_default_deny``.
    instinct_template_default_deny: bool = Field(
        default=False,
        description=(
            "When true, a template BOUND to a pocket that declares no rule "
            "matching a MUTATING action (POST/PUT/PATCH/DELETE and not "
            "read_only) parks the write for human approval instead of firing "
            "(template-level deny-by-default). Reads (read_only / GET / HEAD) "
            "still proceed ungated. Off by default (zero behavior change). "
            "Per-workspace overrides live on the workspace document. Set via "
            "POCKETPAW_INSTINCT_TEMPLATE_DEFAULT_DENY."
        ),
    )
    # Sovereign Zero-Setup Discovery — F6 live enforcement (2026-06-21).
    # When true, approved workspace-discovered Instinct rules
    # (rules.service.get_active_rules) are merged with template rules at the
    # live gate (instinct_dispatch.gate_action) and govern actions. OFF by
    # default — the template-rule path is unchanged and the discovered branch
    # is dead code on the default path (get_active_rules is never called).
    # This is a SEPARATE, NARROWER flag than instinct_approval_level: enforcing
    # WHICH discovered CEL conditions fire and activating WHETHER escalations can
    # auto-resolve are independent risk axes and must toggle independently.
    instinct_enforce_discovered_rules: bool = Field(
        default=False,
        description=(
            "When true, approved workspace-discovered Instinct rules "
            "(rules.service.get_active_rules) are merged with template rules at "
            "the live gate. Off by default — the template-rule path is unchanged "
            "and the whole discovered branch is dead code on the default path. "
            "Set via POCKETPAW_INSTINCT_ENFORCE_DISCOVERED_RULES."
        ),
    )
    automation_evaluator_autostart: bool = Field(
        default=True,
        description=(
            "When true (the default), the background AutomationEvaluator starts at "
            "dashboard boot so threshold/data-change rules fire without a manual "
            "POST /automations/evaluator/start. This is the OSS always-on automation "
            "switch — a new flag defaulting ON is safe because a fresh install with no "
            "enabled rules does nothing but sleep. Set POCKETPAW_AUTOMATION_EVALUATOR_"
            "AUTOSTART=false to keep the evaluator dormant until started via the router."
        ),
    )

    # Billing — Dodo Payments gateway (BC-2, the Gateway primitive).
    # The only payment gateway in v1; a provider abstraction
    # (``ee.cloud.billing.providers``) keeps Razorpay et al. a later swap.
    # All three are optional so a non-billing deployment boots fine; the Dodo
    # provider raises a clear ValidationError when a billing call is made
    # without the key configured.
    dodo_payments_api_key: str | None = Field(
        default=None,
        description=(
            "Dodo Payments API bearer token, used to authenticate the server-side "
            "DodoPayments client when creating a one-time top-up checkout. Set via "
            "POCKETPAW_DODO_PAYMENTS_API_KEY. None disables Dodo top-ups."
        ),
    )
    dodo_environment: str = Field(
        default="test_mode",
        description=(
            "Dodo Payments environment — 'test_mode' (default) or 'live_mode'. "
            "Selects the API base URL the DodoPayments client targets. Set via "
            "POCKETPAW_DODO_ENVIRONMENT."
        ),
    )
    dodo_billing_country: str = Field(
        default="US",
        description=(
            "Default ISO-3166 alpha-2 country prefilled on the Dodo hosted "
            "checkout's billing address. Drives which payment methods Dodo "
            "surfaces — e.g. 'IN' (with INR products) is required for UPI to "
            "appear; 'US' shows cards. The buyer can still change it on the "
            "hosted page. Set via POCKETPAW_DODO_BILLING_COUNTRY."
        ),
    )
    dodo_webhook_secret: str | None = Field(
        default=None,
        description=(
            "Dodo Payments webhook signing secret (Standard Webhooks / 'whsec_…'). "
            "Used to VERIFY the signature on every inbound /billing/webhooks/dodo "
            "POST before the payload is trusted. Set via "
            "POCKETPAW_DODO_WEBHOOK_SECRET. NEVER logged."
        ),
    )
    dodo_credit_product_id: str | None = Field(
        default=None,
        description=(
            "Dodo product id for the credits SKU. A one-time top-up adds this "
            "product to the cart with a pay-what-you-want amount equal to the "
            "purchased credits (1 credit == $0.01 == 1 cent, the currency's lowest "
            "denomination). Set via POCKETPAW_DODO_CREDIT_PRODUCT_ID."
        ),
    )
    dodo_plan_products: Annotated[dict[str, str], NoDecode] = Field(
        default_factory=dict,
        description=(
            "Mapping of plan tier key -> Dodo RECURRING product id (BC-7 "
            "subscriptions). ``subscribe(plan_key)`` looks the product up here to "
            "open a recurring checkout; the inbound subscription webhook reverses "
            "the lookup (product_id -> plan key) to know which tier renewed (and "
            "thus the monthly credit allotment from the plan catalog). Set via "
            "POCKETPAW_DODO_PLAN_PRODUCTS as a JSON object, e.g. "
            '{"team":"prod_team","business":"prod_biz"}. Default empty disables '
            "subscriptions (subscribe raises a clear ValidationError)."
        ),
    )
    dodo_site_products: Annotated[dict[str, str], NoDecode] = Field(
        default_factory=dict,
        description=(
            "Mapping of PER-SITE plan tier key -> Dodo RECURRING product id (BC-9 "
            "per-site subscriptions), the per-site analogue of "
            "dodo_plan_products. ``sites.service.publish_pocket`` looks the "
            "product up here to decide whether a paid site tier can open a "
            "checkout at all: with a product it goes charge-first (the site is "
            "created PENDING and deployed by the subscription.active webhook), "
            "without one it publishes live and records the tier with NO charge. "
            "Set via POCKETPAW_DODO_SITE_PRODUCTS as a JSON object keyed by tier, "
            'e.g. {"site":"pdt_...","staff":"pdt_..."}. Only the SITE-SCOPED rungs '
            "belong here — the org flats (studio, agency) are bought through an "
            "org subscription that does not exist yet, and site_plans refuses "
            "them regardless of what this map says. The pre-2026-08-22 keys "
            '("pro", "business") are still honoured, so an existing deployment '
            "keeps charging while the env var is re-keyed. Default empty "
            "means no per-site tier is purchasable, which is what every "
            "deployment has been until now — this field was READ by "
            "site_plans._dodo_product_for from the day per-site plans shipped and "
            "never DECLARED, so the read always found nothing and setting the env "
            "var did nothing at all."
        ),
    )
    dodo_site_addons: Annotated[dict[str, str], NoDecode] = Field(
        default_factory=dict,
        description=(
            "Mapping of PER-SITE plan tier key -> Dodo ADD-ON id, the rails a paid "
            "site bills on now that it is an add-on LINE on the workspace "
            "subscription rather than a subscription of its own. An add-on is its "
            "own Dodo entity with its own id and is NOT a product id, so this "
            "cannot reuse dodo_site_products (which stays, for the per-site "
            "subscriptions already live in production). "
            "``billing.service.sync_site_addons`` reads it to build the workspace's "
            "full add-on cart and pushes that cart with subscriptions.change_plan. "
            "Set via POCKETPAW_DODO_SITE_ADDONS as a JSON object keyed by tier, "
            'e.g. {"site":"adn_...","staff":"adn_..."}. Only the SITE-SCOPED rungs '
            "belong here — the org flats (studio, agency) are refused by site_plans "
            'regardless of what this map says. The pre-2026-08-22 keys ("pro", '
            '"business") are honoured through the same alias lookup the product map '
            "uses. Default empty means no site tier is purchasable as an add-on, and "
            "a paid publish records the tier without a charge exactly as it does "
            "with an unconfigured product."
        ),
    )
    dodo_checkout_return_base: str = Field(
        default="",
        description=(
            "Fallback base URL the Dodo subscription CHECKOUT SESSION returns the "
            "buyer to after pay / cancel, used ONLY when the /billing/subscribe "
            "request carries no Origin (and no usable Referer) header. The return "
            "urls become ``{base}/settings/billing?checkout=success|cancel``. Set "
            "via POCKETPAW_DODO_CHECKOUT_RETURN_BASE (e.g. https://app.example.com). "
            "Default empty: when both this and the request Origin are absent the "
            "redirect is omitted (the checkout still works, the buyer just isn't "
            "auto-returned)."
        ),
    )

    # Billing — compute-cost metering rate card (BC-3, the Meter + Price
    # primitives). A completed chat run is billed by its real compute cost times
    # a flat markup, converted from USD into integer credits. These two settings
    # ARE the rate card: keeping them declarative (a flat multiplier + the credit
    # denomination) leaves room for a tiered card later without changing the
    # debit path. ``credits = round(cost_usd * markup / credit_usd)``.
    billing_markup: float = Field(
        default=2.5,
        description=(
            "Flat markup applied to a chat run's real compute cost before it is "
            "billed to the workspace wallet (covers infra + margin). The metered "
            "credit charge is round(cost_usd * billing_markup / credit_usd). Set "
            "via POCKETPAW_BILLING_MARKUP."
        ),
    )
    credit_usd: float = Field(
        default=0.01,
        description=(
            "USD value of one credit (1 credit == $0.01 == 1 cent, the lowest "
            "currency denomination — the same denomination Dodo top-ups use). "
            "Divides the marked-up compute cost to yield integer credits. Set via "
            "POCKETPAW_CREDIT_USD."
        ),
    )
    billing_enforced: bool = Field(
        default=False,
        description=(
            "Run-start hard-block (BC-4). When True, STARTING a new chat run is "
            "rejected with HTTP 402 BEFORE any run row is written, on two "
            "conditions, both gated by this flag and both enforced at run start "
            "across the synchronous chat HTTP path (chat/agent_router) AND the "
            "worker/executor path (chat/runs/run_core.execute_run): (1) the "
            "workspace credit balance is <= 0 -> 402 credits.insufficient; (2) the "
            "workspace has hit its monthly credit CEILING (per-plan cap from the "
            "catalog plus any top-ups bought this period; month-to-date spend >= "
            "that ceiling) -> 402 credits.quota_exceeded. In-flight runs are never "
            "killed. Default False so OSS / self-host deployments (which run no "
            "credit ledger) are unaffected; the cloud / subscription (PEE) "
            "deployments turn it on via POCKETPAW_BILLING_ENFORCED. Does NOT "
            "gate the PER-SITE seams on its own any more — those read "
            "billing_enforced OR sites_billing_enforced, so this flag still turns "
            "them on and nothing changes for a deployment already setting it."
        ),
    )
    sites_billing_enforced: bool = Field(
        default=False,
        description=(
            "Per-SITE billing enforcement, independent of billing_enforced. When "
            "True, the Paw Sites seams enforce: the custom-domain capability gate, "
            "the custom-domain count cap (how many SITES in a workspace may carry "
            "one, and how many hostnames a free site may carry), and the visitor "
            "concierge entitlement. Every one of those reads billing_enforced OR "
            "this flag, so setting either turns them on. It exists because the two "
            "decisions are unrelated: charging for custom domains should not also "
            "start rejecting chat runs with 402, and needing one switch for both is "
            "why the sites paywall could not be turned on at all. Explicitly OUTSIDE "
            "its scope, all still governed by billing_enforced alone: chat-run "
            "credit blocks, the seat cap, the pocket cap, the connector cap, the "
            "daily call budget and the storage cap. Default False, so OSS / "
            "self-host sees no paywall and the seams do no extra database read. Set "
            "via POCKETPAW_SITES_BILLING_ENFORCED."
        ),
    )
    billing_dunning_grace_days: int = Field(
        default=7,
        description=(
            "Days a workspace keeps its paid plan after a renewal payment fails "
            "(M5 dunning). A verified subscription.on_hold stamps now + this "
            "many days onto the subscription; the grace sweep revokes the plan "
            "once that deadline passes and a successful retry clears it. Set it "
            "long enough to outlast the gateway's own retry schedule — cutting a "
            "customer off while Dodo is still recovering the charge is worse "
            "than a few extra days of service — and short enough that a card "
            "that will never clear stops costing us. 0 suspends on the first "
            "failed charge, which is deliberate but aggressive. Set via "
            "POCKETPAW_BILLING_DUNNING_GRACE_DAYS."
        ),
    )

    # Self-serve analysis (S1 — transparent-analysis read engine). Gates the
    # Fabric aggregation surface (FabricQuery.group_by/aggregate) end-to-end at
    # the store, so neither the EE /fabric/query route nor agent-tool callers
    # can aggregate while the feature is dark.
    fabric_analyst: bool = Field(
        default=False,
        description=(
            "Enable the Fabric self-serve-analysis read engine (S1): SQL "
            "GROUP BY aggregation + human-readable reasoning steps on "
            "FabricStore.query / POST /fabric/query. When False (the default) a "
            "query carrying group_by/aggregate is rejected with a clear "
            "FabricAnalystDisabledError (HTTP 422, code fabric.analyst_disabled) "
            "— fail-loud, never silent degrade; plain queries are unaffected "
            "either way. Set via POCKETPAW_FABRIC_ANALYST."
        ),
    )

    # Per-tenant LiteLLM virtual-key provisioning (MCG-8). The cloud mints a
    # budgeted, rate-limited virtual key per workspace on the LiteLLM proxy so the
    # proxy enforces a spend ceiling + rate caps per tenant and attributes spend
    # to the workspace. These knobs are the key's provisioning defaults — NOT
    # secrets; the proxy master key stays in POCKETPAW_LITELLM_API_KEY.
    tenant_max_budget_usd: float = Field(
        default=0.0,
        description=(
            "USD budget ceiling minted onto each tenant's LiteLLM virtual key, "
            "enforced by the proxy over POCKETPAW_TENANT_BUDGET_DURATION. 0 == no "
            "budget cap (the proxy applies no ceiling). Set via "
            "POCKETPAW_TENANT_MAX_BUDGET_USD."
        ),
    )
    tenant_budget_duration: str = Field(
        default="30d",
        description=(
            "Reset window for a tenant key's budget — a LiteLLM duration string "
            "(e.g. '30d', '1mo'). Empty == the budget never resets. Set via "
            "POCKETPAW_TENANT_BUDGET_DURATION."
        ),
    )
    tenant_rpm_limit: int = Field(
        default=0,
        description=(
            "Requests-per-minute cap minted onto each tenant's LiteLLM virtual key. "
            "0 == no RPM cap. Set via POCKETPAW_TENANT_RPM_LIMIT."
        ),
    )
    tenant_tpm_limit: int = Field(
        default=0,
        description=(
            "Tokens-per-minute cap minted onto each tenant's LiteLLM virtual key. "
            "0 == no TPM cap. Set via POCKETPAW_TENANT_TPM_LIMIT."
        ),
    )
    litellm_spend_ingest_enabled: bool = Field(
        default=False,
        description=(
            "DEPRECATED back-compat flag for the MCG-8 spend sweep — superseded by "
            "POCKETPAW_LITELLM_SPEND_MODE (WU-F). Left in place so an existing "
            "deployment that set POCKETPAW_LITELLM_SPEND_INGEST_ENABLED=true is not "
            "silently flipped into live billing by deploying WU-F: when the new mode "
            "is left at its 'off' default and this bool is True, "
            "``effective_spend_mode()`` resolves to 'shadow' (read-only "
            "reconciliation, ZERO debits) — NOT 'live'. Making LiteLLM the sole meter "
            "requires an EXPLICIT POCKETPAW_LITELLM_SPEND_MODE=live (a money-meter "
            "flip must be a conscious operator choice). A one-time deprecation notice "
            "is logged when this bool is seen. The flag is ignored once the mode is "
            "set to any non-'off' value. Set via POCKETPAW_LITELLM_SPEND_INGEST_ENABLED."
        ),
    )
    litellm_spend_mode: Literal["off", "shadow", "live"] = Field(
        default="off",
        description=(
            "The billing-cutover mode for LiteLLM proxy spend (WU-F). Replaces the "
            "POCKETPAW_LITELLM_SPEND_INGEST bool with a three-position switch so the "
            "cutover to LiteLLM as the single meter happens through a SAFE "
            "shadow-compare phase:\n"
            "  * 'off'    (default) — nothing changes; BC-3 per-run metering bills "
            "as today, no proxy-spend sweep runs.\n"
            "  * 'shadow' — the safe compare. A per-tenant sweep reads /spend/logs, "
            "converts cost->credits, sums the BC-3 compute_spend ledger debits over "
            "the same window, and records a reconciliation row (litellm vs bc3 + "
            "delta + coverage_gap). It performs ZERO debits — BC-3 keeps billing — "
            "so an operator can confirm the two meters agree BEFORE cutting over.\n"
            "  * 'live'   — LiteLLM is the sole meter: the proxy-spend sweep debits "
            "litellm_spend AND BC-3's per-run metering sweep is gated OFF, so "
            "exactly one meter charges each unit of usage (no double-bill window).\n"
            "Provisioning the per-tenant key is unaffected by this mode (always on). "
            "Set via POCKETPAW_LITELLM_SPEND_MODE."
        ),
    )
    litellm_reconcile_gap_threshold_credits: int = Field(
        default=10,
        description=(
            "Shadow-compare coverage-gap threshold in CREDITS (WU-F). During "
            "POCKETPAW_LITELLM_SPEND_MODE=shadow, a reconciliation row is flagged "
            "``coverage_gap=true`` when |litellm_credits - bc3_credits| exceeds this "
            "many credits — a discrepancy big enough to mean traffic is bypassing "
            "the proxy OR the USD->credits conversion disagrees, which must be "
            "resolved before flipping to 'live'. 1 credit == $0.01, so the default "
            "10 ≈ $0.10 of tolerated per-tenant-per-window drift (rounding noise). "
            "Set via POCKETPAW_LITELLM_RECONCILE_GAP_THRESHOLD_CREDITS."
        ),
    )
    fabric_source_truth_mode: Literal["off", "shadow", "enforce"] = Field(
        default="off",
        description=(
            "Rollout mode for the Fabric source-truth chain (FST). Three-position "
            "switch, mirroring litellm_spend_mode:\n"
            "  * 'off'     (default) — byte-for-byte pre-FST behavior: pure "
            "last-writer-wins; nothing reads or writes the fabric_statements / "
            "fabric_sources provenance tables; the flat FabricObject.properties "
            "dict is the only read path.\n"
            "  * 'shadow'  — provenance statements are recorded at every merge "
            "site and one grep-stable divergence line is logged per "
            "statement-producing property; the cache still takes the LWW value "
            "(the trust-ladder resolver runs advisory-only) and keeps serving "
            "every read.\n"
            "  * 'enforce' — the resolver's winner owns the cache: a lower-trust "
            "write no longer lands in the flat properties dict; the losing claim "
            "is recorded as a statement, not dropped.\n"
            "Flipping back to 'off' is always safe: reads serve the last-resolved "
            "cache, LWW resumes, statement history stays on disk. Gate the flip "
            "to 'enforce' with the FST-8 divergence report (python -m "
            "pocketpaw.fabric.divergence_report <logfile>) — target ZERO "
            "unexplained divergences. Set via "
            "POCKETPAW_FABRIC_SOURCE_TRUTH_MODE."
        ),
    )
    site_pending_alert_hours: float = Field(
        default=24.0,
        description=(
            "Charge-first reconciliation threshold. A PAID Paw Site is created "
            "PENDING and deployed only when its subscription.active webhook "
            "confirms payment; the pending-site sweeper logs at WARNING any site "
            "still pending (not deployed) longer than this many hours, so an "
            "operator can investigate a lost / delayed webhook. Visibility only — "
            "the sweep never auto-deploys or auto-cancels. Tuned above Dodo's "
            "webhook retry window so a transient delay is not flagged. Set via "
            "POCKETPAW_SITE_PENDING_ALERT_HOURS."
        ),
    )

    # Sovereign Zero-Setup Discovery — model-lane sovereignty posture (2026-06-22).
    # Discovery's categorize (F2) and refine (F3) passes send the tenant's data
    # SHAPE (type/property names, article summaries — never raw exhaust) to a
    # model. WHICH model is a sovereignty choice, not a code constant:
    #   * sovereign-local (default, True): the model is hard-pinned to the
    #     tenant's on-box Ollama. ``api_key is None``; no cloud key ever rides
    #     into the request path. Nothing leaves the box. This is the safe
    #     default and matches the original shipped behavior.
    #   * configured-provider (False): discovery uses the workspace's CONFIGURED
    #     provider via ``resolve_llm_client(settings)`` — anthropic / openai /
    #     openai_compatible / gemini / litellm, a CLOUD model is allowed. This is
    #     the tenant's EXPLICIT opt-in to send discovery exhaust to their own
    #     configured model. Most businesses are fine here (faster, better
    #     categories); regulated / sovereign tenants keep the default.
    # Independent of the kb-ingest tripwire: ``kb ingest`` / ``kb build`` (which
    # POST raw tenant text to Anthropic's KB API) are NEVER called regardless of
    # this setting — that path is never correct and stays hard-blocked.
    discovery_sovereign_model: bool = Field(
        default=True,
        description=(
            "Sovereignty posture for discovery's categorize/refine model call. "
            "True (default): hard-pin the model to the tenant's on-box Ollama — "
            "data never leaves the box, no cloud key in the request path "
            "(sovereign-local, the safe default for regulated tenants). False: "
            "use the workspace's configured provider via resolve_llm_client — a "
            "CLOUD model is allowed; this is the tenant's explicit opt-in to send "
            "discovery's inferred data shape to their configured model (faster, "
            "richer categories — fine for most businesses). The kb ingest/build "
            "tripwire holds regardless of this setting. Set via "
            "POCKETPAW_DISCOVERY_SOVEREIGN_MODEL."
        ),
    )

    # Pocket data-source refresh — cost controls (RFC 04 M3).
    # A pocket source binding may declare an `interval` or `webhook` refresh
    # trigger. Both are AUTO-refresh: they re-run a source without a human in
    # the loop, so they cost real backend calls. These two settings cap that
    # cost. The interval floor clamps a too-frequent (or hallucinated)
    # `refresh_interval_seconds` up to a sane minimum; the per-hour cap is a
    # separate budget — counted PER POCKET, distinct from the manual
    # `run_source` per-(pocket, user) limiter — so an interval storm or a
    # webhook flood cannot run up unbounded backend cost.
    source_refresh_min_interval_seconds: int = Field(
        default=60,
        description=(
            "Minimum seconds between automatic interval refreshes of a pocket "
            "data source. A source binding's `refresh_interval_seconds` is "
            "clamped UP to this floor — a hallucinated `refresh_interval_seconds: "
            "1` is never honored. Set via POCKETPAW_SOURCE_REFRESH_MIN_INTERVAL_SECONDS."
        ),
    )
    source_refresh_max_per_hour: int = Field(
        default=60,
        description=(
            "Maximum automatic (interval + webhook) source refreshes per pocket "
            "per rolling hour. Once the budget is spent, further auto-refreshes "
            "are skipped (and logged) rather than queued. This counter is "
            "SEPARATE from the manual run_source rate limiter. Set via "
            "POCKETPAW_SOURCE_REFRESH_MAX_PER_HOUR."
        ),
    )

    # File extraction chain (Phase 1, "Files as Knowledge")
    extraction_chain: list[str] = Field(
        default_factory=lambda: ["local"],
        description=(
            "Ordered list of extraction adapter names "
            "(e.g. ['gemini-flash', 'local']). The chain runs first-match-wins "
            "per MIME, with offline fallback to 'local'. Set via "
            "POCKETPAW_EXTRACTION_CHAIN as a JSON array."
        ),
    )
    extraction_per_mime: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-MIME adapter override map (e.g. {'image/png': 'gemini-flash'}). "
            "Wins over the chain order. Set via POCKETPAW_EXTRACTION_PER_MIME "
            "as a JSON object."
        ),
    )
    extraction_offline_fallback: str = Field(
        default="local",
        description=(
            "Adapter name used when the chosen adapter requires network and "
            "the host is offline. Today the chain hardcodes LocalExtractor as "
            "fallback; this setting reserves the env key for future overrides."
        ),
    )
    gemini_api_key: str | None = Field(
        default=None,
        description=(
            "Google Gemini API key for the gemini-flash extraction adapter. "
            "Read from POCKETPAW_GEMINI_API_KEY. When unset, the gemini-flash "
            "adapter is silently skipped during chain construction."
        ),
    )

    # Embedding adapter (Phase 2, "Files as Knowledge" Stage 2.D)
    kb_vectors_enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the vector-embedding pipeline. When False the "
            "FileReady listener stops after text-ingest and the chat path "
            "skips interleaved-image queries. Set via POCKETPAW_KB_VECTORS_ENABLED."
        ),
    )
    embedding_adapter: str = Field(
        default="",
        description=(
            "Embedding adapter name. Empty disables embeddings even when "
            "kb_vectors_enabled is True. Supported: "
            "'vertex-gemini-embedding-2' (preview, 3072-dim, multimodal), "
            "'vertex-mm-001' (GA, 1408-dim, text+image). Set via "
            "POCKETPAW_EMBEDDING_ADAPTER."
        ),
    )
    embedding_dim: int = Field(
        default=1024,
        gt=0,
        description=(
            "Target output dim. vertex-gemini-embedding-2 uses Matryoshka "
            "truncation; vertex-mm-001 snaps to the closest valid native "
            "dim (128/256/512/1408). All vectors in a single kb-go scope "
            "must agree on this value. Set via POCKETPAW_EMBEDDING_DIM."
        ),
    )
    embedding_monthly_cap_usd: float = Field(
        default=10.0,
        ge=0,
        description=(
            "Soft monthly USD cap for embedding spend. When the running "
            "total would exceed this, the listener falls back to "
            "extraction-only (text still ingests, vector skipped). 0 "
            "disables the cap. Persisted at ~/.pocketpaw/embedding_cost.json. "
            "NOT a billing source — real billing comes from the provider's "
            "dashboard. Set via POCKETPAW_EMBEDDING_MONTHLY_CAP_USD."
        ),
    )
    vertex_project_id: str | None = Field(
        default=None,
        description=(
            "GCP project id for vertex-mm-001 (the multimodalembedding@001 "
            "adapter). When unset, the adapter is silently skipped during "
            "factory construction. Set via POCKETPAW_VERTEX_PROJECT_ID."
        ),
    )
    vertex_location: str | None = Field(
        default=None,
        description=(
            "GCP region for vertex-mm-001 (default: us-central1 when "
            "unset). Set via POCKETPAW_VERTEX_LOCATION."
        ),
    )

    soul_cognitive_model: str = Field(
        default="",
        description=(
            "Model to use for soul cognitive processing (sentiment, significance, "
            "fact/entity extraction). Empty = use main agent backend. Set to a cheaper "
            "model like 'claude-haiku-4-5-20251001' to reduce cost. Requires anthropic SDK."
        ),
    )

    notification_channels: list[str] = Field(
        default_factory=list,
        description="Targets for autonomous messages, e.g. ['telegram:12345', 'discord:98765']",
    )

    # Status API
    status_api_key: str = Field(
        default="",
        description="Optional API key for the agent status endpoint. Leave empty to skip auth.",
    )

    # Media Downloads
    media_download_dir: str = Field(
        default="", description="Custom media download dir (default: ~/.pocketpaw/media/)"
    )
    media_max_file_size_mb: int = Field(
        default=50, ge=0, description="Max media file size in MB (0 = unlimited)"
    )

    # UX
    welcome_hint_enabled: bool = Field(
        default=True,
        description="Send a one-time welcome hint on first interaction in non-web channels",
    )

    # Channel Autostart
    channel_autostart: dict[str, bool] = Field(
        default_factory=dict,
        description="Per-channel autostart on dashboard launch (missing keys default to True)",
    )

    # Concurrency
    #
    # Three different ceilings live near each other and are easy to confuse, so:
    #   * ``max_concurrent_conversations`` — the OSS AgentLoop semaphore. Gates the
    #     CHANNEL adapters (Telegram / Discord / Slack / WhatsApp) only. The cloud
    #     chat path (``ee.cloud.chat.runs.run_core``) does NOT import AgentLoop, so
    #     raising this does nothing for a cloud deploy.
    #   * ``agent_pool_max_instances`` / ``session_warm_*`` — PER-PROCESS agent-tier
    #     ceilings, in force on both the web process and each arq worker.
    #   * ``POCKETPAW_ARQ_MAX_JOBS`` (in the arq WorkerSettings, not here) — the
    #     CLUSTER-WIDE concurrent-job ceiling. That is the one that bounds how many
    #     agent runs execute at once in a cloud deploy.
    max_concurrent_conversations: int = Field(
        default=5,
        gt=0,
        description=(
            "Max parallel conversations processed simultaneously by the OSS "
            "AgentLoop — the channel adapters (Telegram/Discord/Slack/WhatsApp). "
            "Does NOT gate cloud chat runs, which bypass AgentLoop entirely; use "
            "POCKETPAW_ARQ_MAX_JOBS for those."
        ),
    )
    agent_pool_max_instances: int = Field(
        default=20,
        gt=0,
        description=(
            "Max live agent instances held by one process's AgentPool before it "
            "refuses to build another. PER-PROCESS, not cluster-wide. Set via "
            "POCKETPAW_AGENT_POOL_MAX_INSTANCES."
        ),
    )
    session_warm_max_per_tenant: int = Field(
        default=8,
        gt=0,
        description=(
            "Max WARM session slots one workspace may hold in a single process. "
            "Over the limit the SessionSupervisor LRU-evicts the oldest IDLE slot "
            "(never a busy one). This is the per-tenant fairness knob: it stops one "
            "workspace holding every warm slot. Set via "
            "POCKETPAW_SESSION_WARM_MAX_PER_TENANT."
        ),
    )
    session_warm_max_global: int = Field(
        default=64,
        gt=0,
        description=(
            "Max WARM session slots across ALL workspaces in a single process. Each "
            "warm slot pins a live agent client (a subprocess under the default "
            "claude_agent_sdk backend), so raise this with the container's memory "
            "limit. Set via POCKETPAW_SESSION_WARM_MAX_GLOBAL."
        ),
    )

    # Composio — MCP-direct tool provider for the parent cloud chat agent.
    # Wired into src/pocketpaw/agents/claude_sdk.py::_get_mcp_servers; the
    # pocket specialist does NOT receive Composio MCP. When api_key is set,
    # composio_enterprise_id is required to namespace the per-user Composio
    # user_id (avoids collisions across PocketPaw enterprise deployments
    # that share one Composio org).
    composio_api_key: str | None = Field(
        default=None, description="Composio API key (enables Composio MCP for the parent agent)"
    )
    composio_base_url: str | None = Field(
        default=None,
        description="Composio base URL. None = Composio cloud; set for self-hosted runtime.",
    )
    # ``NoDecode`` keeps pydantic-settings from trying to JSON-parse the raw
    # env string; the ``field_validator`` below handles CSV → list[str].
    composio_toolkits: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "Allow-list of Composio toolkit slugs (e.g. 'gmail,slack,github'). "
            "Comma-separated when set via env. Empty = fail closed (no toolkits exposed)."
        ),
    )
    composio_enterprise_id: str | None = Field(
        default=None,
        description=(
            "Namespace prefix for Composio user_id (f'{enterprise_id}:{user_id}'). "
            "Required when composio_api_key is set."
        ),
    )
    composio_mcp_url_ttl_seconds: int = Field(
        default=3600,
        gt=0,
        description=(
            "How long per-user Composio tools are cached in-process before "
            "re-fetching via the provider's ``composio.create(user_id=...)`` + "
            "``session.tools()``. The Composio call is a network round-trip "
            "and the per-user toolset rarely changes mid-session, so caching "
            "covers the common case. Default: 1h."
        ),
    )
    composio_connect_link_inline: bool = Field(
        default=True,
        description=(
            "When True, Composio 'needs auth' responses render as an inline Ripple button "
            "instead of a raw URL in the chat. Set False to disable if detection is brittle."
        ),
    )

    @field_validator("dodo_site_products", mode="before")
    @classmethod
    def _parse_dodo_site_products(cls, v: object) -> object:
        """Same degrade as the workspace-plan map: a malformed env string becomes
        an empty mapping rather than crashing settings load at boot.

        The consequence of an empty mapping is milder here and worth knowing: no
        per-site tier is purchasable, so a paid selection publishes live and
        records the tier without a charge. It does not raise.
        """
        if v is None or v == "":
            return {}
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return v

    @field_validator("dodo_site_addons", mode="before")
    @classmethod
    def _parse_dodo_site_addons(cls, v: object) -> object:
        """Same degrade as the two product maps: a malformed env string becomes an
        empty mapping rather than crashing settings load at boot.

        Needs ``NoDecode`` on the field for this to run at all — ``EnvSettingsSource``
        JSON-decodes a complex field at SOURCE time and raises ``SettingsError``
        before any field validator sees the value. The field carries it.

        An empty mapping means no site tier is purchasable as an add-on: a paid
        publish records the tier with no charge. It does not raise.
        """
        if v is None or v == "":
            return {}
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return v

    @field_validator("dodo_plan_products", mode="before")
    @classmethod
    def _parse_dodo_plan_products(cls, v: object) -> object:
        """Accept a JSON-object env string for the plan->product map.

        A hand-set ``POCKETPAW_DODO_PLAN_PRODUCTS`` that isn't valid JSON (or
        isn't an object) degrades to an empty mapping — subscriptions then fail
        loudly at ``subscribe`` time with a clear ``plan_unconfigured`` error
        rather than crashing the entire settings load at boot. A dict passes
        straight through.

        THE FIELD NEEDS ``NoDecode`` FOR ANY OF THAT TO HAPPEN, and it did not
        carry it until 2026-08-21. ``EnvSettingsSource`` JSON-decodes a complex
        field's raw value at SOURCE time, before a single field validator runs,
        and raises ``SettingsError`` on failure — so this validator was
        unreachable for exactly the malformed input it was written to absorb, and
        the docstring claiming otherwise was wrong for as long as it existed. A
        typo in the env var took the server down at boot. Measured, not inferred:
        see tests/cloud/billing/test_site_plan_purchasable.py.
        """
        if v is None or v == "":
            return {}
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return v

    @field_validator("composio_toolkits", mode="before")
    @classmethod
    def _parse_composio_toolkits_csv(cls, v: object) -> object:
        """Accept comma-separated env values (e.g. 'gmail, slack ,github').

        pydantic-settings normally requires JSON for list fields; this
        before-validator lets ops set the allow-list as plain CSV in
        ``POCKETPAW_COMPOSIO_TOOLKITS`` without quoting brackets.
        """
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @model_validator(mode="after")
    def _validate_composio_invariants(self) -> Settings:
        """Enforce composio_api_key → composio_enterprise_id required.

        Without the enterprise_id namespace, two PocketPaw deployments
        sharing one Composio org would collide on user_id space. Fail at
        startup rather than at first tool call.
        """
        if self.composio_api_key and not self.composio_enterprise_id:
            raise ValueError(
                "composio_enterprise_id is required when composio_api_key is set "
                "(POCKETPAW_COMPOSIO_ENTERPRISE_ID). Prevents user_id collisions "
                "across enterprise deployments sharing one Composio org."
            )
        return self

    @model_validator(mode="after")
    def _migrate_kb_scope(self) -> Settings:
        """Copy deprecated single ``kb_scope`` into ``kb_scopes`` once.

        When a host has only the legacy ``POCKETPAW_KB_SCOPE`` set we keep
        their KB injection working: the string is appended to ``kb_scopes``
        and a :class:`DeprecationWarning` nudges them to switch. If both
        keys are populated the new list wins and the legacy string is
        ignored (no surprise merging).
        """
        if not self.kb_scopes and self.kb_scope:
            warnings.warn(
                "POCKETPAW_KB_SCOPE is deprecated; use POCKETPAW_KB_SCOPES "
                "(list, e.g. POCKETPAW_KB_SCOPES='[\"workspace:w1\"]')",
                DeprecationWarning,
                stacklevel=2,
            )
            self.kb_scopes = [self.kb_scope]
        return self

    def effective_spend_mode(self) -> Literal["off", "shadow", "live"]:
        """Resolve the LiteLLM billing-cutover mode, honouring the legacy bool.

        WU-F replaced the ``litellm_spend_ingest_enabled`` bool with the
        three-position ``litellm_spend_mode`` switch. Resolution, in order:

          1. An EXPLICIT ``POCKETPAW_LITELLM_SPEND_MODE`` (any non-'off' value)
             wins outright — ``shadow`` and ``live`` are taken as set.
          2. Mode unset / 'off' + the legacy bool True → ``shadow`` (NOT ``live``).
          3. Mode unset / 'off' + the legacy bool False/unset → ``off``.

        WHY the legacy bool maps to ``shadow`` and NEVER ``live`` (money safety):
        WU-F adds the FIRST periodic ingestion caller (the cutover sweep on the
        heartbeat + worker boot). Under WU-C the bool toggled an ingestion path
        that had NO periodic caller, so a deployment could carry
        ``POCKETPAW_LITELLM_SPEND_INGEST_ENABLED=true`` harmlessly. If WU-F resolved
        that bool to ``live``, merely DEPLOYING WU-F would start LiteLLM debiting AND
        gate BC-3 off — a billing-meter flip with no operator decision. So ``live``
        requires an EXPLICIT ``POCKETPAW_LITELLM_SPEND_MODE=live`` and is never
        inferred. The legacy bool resolves to ``shadow`` — it reads + compares but
        debits nothing — so an old deployment gets the (safe) reconciliation signal
        and the operator must consciously set the mode to ``live`` to bill. See
        ``warn_legacy_spend_bool_once`` for the one-time startup notice.
        """
        if self.litellm_spend_mode != "off":
            return self.litellm_spend_mode
        if self.litellm_spend_ingest_enabled:
            return "shadow"
        return "off"

    def effective_deep_work_verify_mode(self) -> Literal["off", "shadow", "enforce"]:
        """Resolve the deep_work verify-loop mode, honouring the legacy bool.

        Mirrors the ``effective_spend_mode()`` shape: the three-position
        ``deep_work_verify_mode`` switch supersedes the
        ``deep_work_verify_loop_enabled`` bool. Resolution, in order:

          1. An EXPLICIT ``POCKETPAW_DEEP_WORK_VERIFY_MODE`` (any non-'off'
             value) wins outright — ``shadow`` and ``enforce`` are taken as
             set.
          2. Mode unset / 'off' + the legacy bool True → ``enforce``.
          3. Mode unset / 'off' + the legacy bool False/unset → ``off``.

        WHY the legacy bool maps to ``enforce`` and never ``shadow`` (the
        DELIBERATE opposite of the spend-mode precedent): the verify bool's
        SHIPPED meaning is the full acting loop — requeue on PARTIAL /
        NOT_SOLVED, escalate to BLOCKED on budget / no-progress. A
        deployment that set the bool opted into that enforcement; resolving
        it to 'shadow' would silently strip requeue/escalate from that
        deployment on upgrade — a behaviour downgrade with no operator
        decision. (The spend bool mapped to the safe MIDDLE position
        because 'live' moves money and the bool's legacy path had no
        periodic caller; here the bool's legacy behaviour IS the strong
        position, so back-compat preserves it.)
        """
        if self.deep_work_verify_mode != "off":
            return self.deep_work_verify_mode
        if self.deep_work_verify_loop_enabled:
            return "enforce"
        return "off"

    def effective_cloud_plan_verify_mode(self) -> Literal["off", "shadow", "enforce"]:
        """Resolve the CLOUD planner verify-loop mode, honouring the legacy bool.

        Identical shape to ``effective_deep_work_verify_mode()`` at the
        ee/cloud planner terminal: an explicit non-'off'
        ``POCKETPAW_CLOUD_PLAN_VERIFY_MODE`` wins outright; mode 'off' +
        legacy ``cloud_plan_verify_loop_enabled`` True → ``enforce`` (NOT
        shadow — the bool's shipped meaning is the acting loop, see the
        deep_work resolver's docstring for the full rationale); otherwise
        ``off``.
        """
        if self.cloud_plan_verify_mode != "off":
            return self.cloud_plan_verify_mode
        if self.cloud_plan_verify_loop_enabled:
            return "enforce"
        return "off"

    def save(self) -> None:
        """Save settings to config file.

        Non-secret fields go to config.json. Secret fields (API keys, tokens)
        go to the encrypted credential store.

        Uses model_dump() to automatically include all fields — no need to
        manually list every field when new settings are added.

        Runs format validation on API keys before saving; logs warnings but
        never blocks or raises.
        """
        # TODO: When adding new sensitive fields, ensure they are included in SECRET_FIELDS in
        # pocketpaw/credentials.py to prevent plaintext storage.
        from pocketpaw.credentials import SECRET_FIELDS, get_credential_store

        config_path = get_config_path()

        # Load existing config to preserve secret values if current is empty
        existing: dict = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except (json.JSONDecodeError, Exception):
                pass

        # Dump all fields with JSON-mode serialization (converts Path→str, etc.)
        all_fields = self.model_dump(mode="json")

        # For secret fields, preserve existing value if current is empty/None
        for key in SECRET_FIELDS:
            if key in all_fields and not all_fields[key] and existing.get(key):
                all_fields[key] = existing[key]

        # Store secrets in the encrypted credential store, then strip
        # them from the dict before writing config.json to prevent
        # plaintext secret leakage.
        store = get_credential_store()
        for key, value in all_fields.items():
            if key in SECRET_FIELDS and value:
                store.set(key, value)

        safe_fields = {k: v for k, v in all_fields.items() if k not in SECRET_FIELDS}
        config_path.write_text(json.dumps(safe_fields, indent=2))
        _chmod_safe(config_path, 0o600)

    @classmethod
    def load(cls) -> Settings:
        """Load settings from config file + encrypted credential store.

        Set ``POCKETPAW_IGNORE_CONFIG_JSON=true`` to skip config.json
        entirely. Useful when ``.env`` is the source of truth and you
        don't want unset fields to silently inherit stale dashboard
        values. Secrets from the encrypted credential store still load.
        """
        from pocketpaw.credentials import SECRET_FIELDS, get_credential_store

        _migrate_plaintext_keys()

        ignore_json = os.environ.get("POCKETPAW_IGNORE_CONFIG_JSON", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

        config_path = get_config_path()
        data: dict = {}
        if config_path.exists() and not ignore_json:
            try:
                data = json.loads(config_path.read_text())
            except (json.JSONDecodeError, Exception):
                pass

        store = get_credential_store()
        secrets = store.get_all()
        for field in SECRET_FIELDS:
            if field in secrets and secrets[field]:
                data[field] = secrets[field]

        env_prefix = cls.model_config.get("env_prefix", "")
        for field in list(data.keys()):
            if os.environ.get(f"{env_prefix}{field.upper()}") is not None:
                data.pop(field, None)

        if data:
            try:
                return cls(**data)
            except Exception:
                pass
        return cls()


@lru_cache
def get_settings(force_reload: bool = False) -> Settings:
    """Get cached settings instance."""
    if force_reload:
        get_settings.cache_clear()
    return Settings.load()


def get_access_token() -> str:
    """
    Get the current access token.
    If it doesn't exist, generate a new one.
    """
    token_path = get_token_path()
    if token_path.exists():
        token = token_path.read_text().strip()
        if token:
            return token

    return regenerate_token()


def regenerate_token() -> str:
    """
    Generate a new secure access token and save it.
    Invalidates previous tokens.
    """
    import uuid

    token = str(uuid.uuid4())
    token_path = get_token_path()
    token_path.write_text(token)
    _chmod_safe(token_path, 0o600)
    return token


# Flag file to avoid re-running migration on every load
_MIGRATION_DONE_PATH: Path | None = None


def _migrate_plaintext_keys() -> None:
    """One-time migration: move plaintext API keys from config.json to encrypted store."""
    from pocketpaw.credentials import SECRET_FIELDS, get_credential_store

    global _MIGRATION_DONE_PATH  # noqa: PLW0603
    if _MIGRATION_DONE_PATH is None:
        _MIGRATION_DONE_PATH = get_config_dir() / ".secrets_migrated"

    if _MIGRATION_DONE_PATH.exists():
        return

    config_path = get_config_path()
    if not config_path.exists():
        # No config yet — nothing to migrate
        _MIGRATION_DONE_PATH.write_text("1")
        return

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, Exception):
        return

    store = get_credential_store()
    migrated_count = 0

    for field in SECRET_FIELDS:
        value = data.get(field)
        if value and isinstance(value, str):
            store.set(field, value)
            migrated_count += 1
            # Remove plaintext secret from config to prevent leakage
            del data[field]

    if migrated_count:
        logger.info("Copied %d secret(s) from config to encrypted store.", migrated_count)
        # Save the cleaned config back to remove plaintext secrets
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        _chmod_safe(config_path, 0o600)

    _MIGRATION_DONE_PATH.write_text("1")
    _chmod_safe(_MIGRATION_DONE_PATH, 0o600)
