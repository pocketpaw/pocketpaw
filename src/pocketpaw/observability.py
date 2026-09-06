# src/pocketpaw/observability.py — Logfire configuration for the whole process.
#
# Created 2026-09-05. Three jobs, all of them process-wide:
#
#   1. configure_observability() — call ``logfire.configure`` ONCE, at startup.
#      It used to live inside PydanticAIBackend._build_instrumentation_capability,
#      which is wrong in four ways: it is gated on a pydantic-ai feature flag, so
#      the logging bridge could not be turned on independently; it only fires in a
#      process that actually constructs a pydantic-ai agent, and with
#      POCKETPAW_CLOUD_RUN_EXECUTOR=arq the web process enqueues and never builds
#      one, so on the deployed stack it ran in the worker and never in the backend;
#      its service_name was the literal "pocketpaw-pydantic-ai", and both
#      containers run the SAME image, so the two would be one indistinguishable
#      service; and it ran late, as a side effect of construction, after the
#      process was already serving. Here it is called from setup_logging(), which
#      is the one function both entrypoints already run first.
#
#   2. instrument_everything() — attach every integration in _instrumentations():
#      the agent backends, the HTTP clients, Redis, Mongo, MCP, and host metrics.
#      FastAPI is the exception, because it needs the app object, so it has its own
#      entry point in instrument_fastapi_app().
#
#      Global pydantic-ai instrumentation is safe alongside the per-agent
#      Instrumentation capability the backend can add: pydantic-ai 2.18 injects the
#      global one only when the run's capability list does not already carry an
#      InstrumentationCap (agent/__init__.py:1368), so the two cannot double up.
#
#   3. install_logfire_bridge() — add Logfire's stdlib logging handler to the root
#      ADDITIVELY. Not via basicConfig(handlers=[...]), which the Logfire docs
#      suggest and which would delete the console handler — and with it the
#      handler that install_scrubbing_filters() attaches the scrubbers to.
#
# ALL OF IT IS INERT UNLESS POCKETPAW_LOGFIRE_ENABLED IS TRUTHY. Turning it on with
# no LOGFIRE_TOKEN and no OTEL_EXPORTER_OTLP_ENDPOINT is not free and not useful:
# logfire builds a real TracerProvider with zero span processors, so spans are
# constructed, serialized and dropped. There is no short-circuit for traces the way
# there is for metrics.
"""Process-wide observability wiring (Pydantic Logfire)."""

from __future__ import annotations

import importlib.util
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: Values that count as "on" for POCKETPAW_LOGFIRE_ENABLED. Spelled out because
#: ``bool(os.environ.get(...))`` makes the string "false" truthy, which is the
#: classic way an off switch turns out to be a decoration.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Routes excluded from FastAPI request spans. The compose healthcheck curls
#: ``/api/v1/version`` every 30 seconds in both containers, which is thousands of
#: metered spans a day saying nothing a container status does not already say.
_UNTRACED_ROUTES = r"/api/v1/version,/api/v1/health"

# ``logfire.configure`` is process-global. The guard is module state rather than a
# parameter because two independent callers reach it — setup_logging() at startup,
# and the pydantic-ai backend as a fallback for a process that never ran
# setup_logging (tests, library use, anything embedding the backend).
_CONFIGURED = False


def _env_flag(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in _TRUTHY


def logfire_enabled() -> bool:
    """The master switch, read straight from the environment.

    Not a pydantic setting. ``setup_logging`` runs before settings are reliably
    loadable, and this follows the same precedent as ``POCKETPAW_LOG_LEVEL``,
    which both entrypoints read from ``os.environ`` for exactly that reason.
    """
    return _env_flag("POCKETPAW_LOGFIRE_ENABLED")


def logfire_include_content() -> bool:
    """Whether agent PROMPTS AND COMPLETIONS are sent to Logfire. Off by default.

    Logfire's own default is on, and this deliberately inverts it. Logfire does
    not scrub message content at all — every ``gen_ai.*`` message key is in its
    SAFE_KEYS allowlist, on the reasoning that an LLM prompt is the thing you most
    want to read when a run goes wrong. That is right for a debugging session and
    it is a data-residency decision for a product: with it on, customer chat text
    leaves the network and lands in a hosted store.

    Off, the spans still carry model, token counts, latency and tool names, which
    is what makes a cost or a slowdown diagnosable. On, they also carry what the
    customer typed.
    """
    return _env_flag("POCKETPAW_LOGFIRE_INCLUDE_CONTENT")


def logfire_extra_scrub_patterns() -> list[str]:
    """Our credential regexes, in the form ``ScrubbingOptions`` wants.

    Logfire's own ``DEFAULT_PATTERNS`` are KEYWORD patterns — "password", "secret",
    "api[._ -]?key" — so they match an attribute *named* like a credential and
    match none of the credential FORMATS this codebase actually handles.
    ``sk-ant-``, ``xoxb-`` and ``pp_`` hit nothing in that list.

    This closes the attribute side. The message side is closed by ``SecretFilter``
    running as a logging filter before emit, because the formatted message lands in
    ``logfire.msg``, which is the first entry in Logfire's SAFE_KEYS allowlist and
    is never scrubbed by Logfire itself.
    """
    from pocketpaw.logging_setup import secret_pattern_strings

    return secret_pattern_strings()


def _pydantic_ai_instrumentation() -> None:
    """Instrument every pydantic-ai agent in this process.

    ``Agent.instrument_all`` under the hood, so an agent built anywhere emits run,
    model-request and tool-call spans — not only one built through a code path that
    remembered to pass the capability.

    No double-instrumentation: pydantic-ai resolves the global settings into an
    ``InstrumentationCap`` at run time and inserts it only when the run's capability
    list does not already have one, so an agent carrying the backend's explicit
    ``Instrumentation()`` capability is instrumented once, by its own.

    Content is excluded unless ``POCKETPAW_LOGFIRE_INCLUDE_CONTENT`` says otherwise
    — see ``logfire_include_content``.

    Raises rather than returning a flag; ``_instrument`` is what swallows it.
    """
    import logfire

    # find_spec rather than ``import pydantic_ai``: an existence check should not
    # execute a heavy package, and pydantic-ai is an extra too — a deployment on
    # another agent backend has no agents to instrument.
    if importlib.util.find_spec("pydantic_ai") is None:
        raise RuntimeError("pydantic-ai is not installed")
    include_content = logfire_include_content()
    logfire.instrument_pydantic_ai(
        include_content=include_content,
        include_binary_content=include_content,
    )


def _instrumentations() -> dict[str, Any]:
    """Everything instrumented at startup, by name.

    Built lazily because every value closes over ``logfire``, and importing it at
    module scope would put a ``pydantic-ai``-extra dependency in the import path of
    a base install.

    The keyword arguments are data decisions, not taste ones:

    * ``capture_headers=False`` everywhere. Headers carry ``Authorization``, our
      own API keys and session cookies, and Logfire's scrubber matches attribute
      NAMES against a keyword list, so a bearer token under a header name it does
      not recognise would go straight through.
    * ``capture_request_body`` / ``capture_response_body`` stay off on httpx.
      Those bodies are the customer's prompts and the model's completions.
    * ``capture_statement=False`` on Redis. The statement carries the job payload,
      which for the chat lane is the conversation.

    Not here: sqlite3 and sqlalchemy. Fabric, Instinct, the audit log and the
    ledger all run on SQLite and would emit a span per statement, which adds little
    over the operation span already wrapping them and would dominate the bill.
    ``POCKETPAW_LOGFIRE_INSTRUMENT`` can name them back in.
    """
    import logfire

    return {
        # --- Agent backends. The reason any of this exists. ---
        "pydantic_ai": _pydantic_ai_instrumentation,
        "claude_agent_sdk": logfire.instrument_claude_agent_sdk,
        "anthropic": logfire.instrument_anthropic,
        "openai": logfire.instrument_openai,
        # Tool calls out to MCP servers, which is where an agent's side effects
        # actually happen.
        "mcp": logfire.instrument_mcp,
        # --- Everything the process talks to. ---
        # httpx is the whole outbound surface: the LiteLLM proxy, provider APIs,
        # Daytona, webhooks. A slow model request and a slow sandbox read
        # identically in a log line and are one attribute apart in a span.
        "httpx": lambda: logfire.instrument_httpx(capture_headers=False),
        "aiohttp_client": logfire.instrument_aiohttp_client,
        # The arq queues and the run stream transport.
        "redis": lambda: logfire.instrument_redis(capture_statement=False),
        # Mongo, under both motor and beanie.
        "pymongo": logfire.instrument_pymongo,
        # --- The host. Metrics rather than spans, so close to free. ---
        # CPU, memory, disk and network for the container itself, which answers
        # "was it the code or was it the box" without an SSH session.
        "system_metrics": logfire.instrument_system_metrics,
    }


def _instrument(name: str, call: Any) -> bool:
    """Run one instrumentation, and never let it take the process down.

    A missing optional package is the expected case, not a fault: the OTel
    instrumentors ride on the ``pydantic-ai`` extra's logfire spec, and an install
    without them should lose spans, never fail to boot. So a
    per-integration failure is DEBUG while the summary of what attached is INFO.
    One line naming real coverage beats eight naming absence.
    """
    try:
        call()
    except Exception as exc:  # noqa: BLE001
        logger.debug("logfire: %s not instrumented (%s)", name, exc)
        return False
    return True


def _selected_instrumentations() -> list[str]:
    """Which integrations to attach. Everything, unless told otherwise.

    ``POCKETPAW_LOGFIRE_INSTRUMENT`` replaces the default set with an explicit
    comma-separated list, and ``none`` disables all of them. It exists because
    spans are metered per unit: if one integration turns out to dominate the bill,
    dropping it should not need a deploy of new code.
    """
    raw = os.environ.get("POCKETPAW_LOGFIRE_INSTRUMENT", "").strip()
    known = _instrumentations()
    if not raw:
        return list(known)
    if raw.lower() == "none":
        return []
    wanted = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [name for name in wanted if name not in known]
    if unknown:
        logger.warning(
            "POCKETPAW_LOGFIRE_INSTRUMENT names unknown integrations %s; known are %s",
            unknown,
            sorted(known),
        )
    return [name for name in wanted if name in known]


def instrument_everything() -> list[str]:
    """Attach every selected instrumentation. Returns the ones that took.

    Called by ``configure_observability``, and separate from it so a caller can
    re-run it after importing a library that was not present at startup.
    """
    try:
        registry = _instrumentations()
    except Exception as exc:  # noqa: BLE001
        # Not just ImportError. Building the registry touches every
        # ``logfire.instrument_*`` attribute, so a name that a future logfire
        # renames raises AttributeError HERE, before the per-integration guard in
        # ``_instrument`` can contain it — and this runs inside setup_logging, at
        # module scope in __main__, where an exception stops the process booting.
        logger.debug("logfire: no instrumentation (%s)", exc)
        return []
    attached = [name for name in _selected_instrumentations() if _instrument(name, registry[name])]
    if attached:
        logger.info("logfire: instrumented %s", ", ".join(attached))
    return attached


def instrument_fastapi_app(app: Any) -> bool:
    """Instrument one FastAPI app: a span per request, with route and status.

    Separate from the rest because it needs the app object, so it cannot run at
    configure time. Call it once the routers are mounted — the instrumentor reads
    the route table to label spans by route TEMPLATE rather than by path, which is
    what keeps ``/api/v1/pockets/{id}`` one row instead of one row per pocket.

    THE TRY/EXCEPT BELOW IS NOT A SAFETY NET FOR THE REQUEST PATH. It catches a
    failure to *install* the instrumentation. What this call installs is ASGI
    middleware that then runs on every request, outside this function and outside
    any guard here, so a bug in it takes the whole API down while this function
    reports success. That is not theoretical: reading the route table is exactly
    where it broke. Under fastapi>=0.137 ``app.routes`` holds ``_IncludedRouter``
    objects with no ``.path``, the instrumentor read it anyway on every CORS
    preflight, and production served 163 preflight 500s that the browser reported
    as CORS failures. Held off by the fastapi ceiling in pyproject; see
    tests/test_fastapi_version_cap.py. Treat anything this installs as
    load-bearing, whatever the docstring on observability says about it not being.
    """
    if not logfire_enabled():
        return False
    try:
        import logfire
    except ImportError:
        return False
    try:
        logfire.instrument_fastapi(
            app,
            # Headers carry Authorization and session cookies.
            capture_headers=False,
            excluded_urls=_UNTRACED_ROUTES,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("logfire: fastapi not instrumented (%s)", exc)
        return False
    return True


def configure_observability() -> bool:
    """Configure Logfire once per process. True when it is configured.

    Never raises. Observability is not load-bearing: a bad token or an unreachable
    exporter must not stop a tenant's run or a container's boot.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return True
    try:
        import logfire
    except ImportError:
        # logfire lives in the ``pydantic-ai`` EXTRA, not the base dependency
        # list, so a bare ``pip install pocketpaw`` does not have it.
        return False

    _CONFIGURED = True
    try:
        logfire.configure(
            # From the environment because the backend and the worker run the
            # SAME image. A literal here makes them one service in every query.
            service_name=os.environ.get("LOGFIRE_SERVICE_NAME", "pocketpaw"),
            environment=os.environ.get("LOGFIRE_ENVIRONMENT", "development"),
            # Safe with no account: with no token and no OTEL_EXPORTER_OTLP_*
            # endpoint, nothing is exported.
            send_to_logfire="if-token-present",
            # Logfire's OWN console span exporter, which would print an indented
            # span tree to stderr next to every log line. Nothing to do with the
            # Rich handler, which is a stdlib logging handler Logfire never touches.
            console=False,
            # The default EXTRACTS an incoming traceparent header. On an
            # internet-facing API that lets any caller graft spans onto our traces.
            distributed_tracing=False,
            scrubbing=logfire.ScrubbingOptions(extra_patterns=logfire_extra_scrub_patterns()),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not configure logfire: %s", exc)
        return False

    instrument_everything()
    return True


def install_logfire_bridge(level: str = "INFO") -> bool:
    """Add Logfire's logging handler to the root logger, alongside the existing ones.

    The caller must run ``install_scrubbing_filters()`` AFTER this. That function
    walks the root's handler list, so a handler added later carries no scrubbers —
    and this is the handler where an unscrubbed line stops being a local stdout
    leak and becomes an exfiltrated one.

    ``fallback=NullHandler()`` because the default is a ``StreamHandler`` on
    stderr, used while instrumentation is suppressed — which is exactly during
    Logfire's own export request. With the console handler already on stderr that
    shows up as occasional duplicated lines.
    """
    try:
        from logfire.integrations.logging import LogfireLoggingHandler
    except ImportError:
        return False

    root = logging.getLogger()
    if any(isinstance(handler, LogfireLoggingHandler) for handler in root.handlers):
        return True
    try:
        root.addHandler(
            LogfireLoggingHandler(
                level=getattr(logging, level.upper(), logging.INFO),
                fallback=logging.NullHandler(),
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not install the logfire logging bridge: %s", exc)
        return False
    return True
