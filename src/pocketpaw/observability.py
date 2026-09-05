# src/pocketpaw/observability.py — Logfire configuration for the whole process.
#
# Created 2026-09-05. Two jobs, both of them process-wide:
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
#      configure_observability() also instruments pydantic-ai globally, via
#      logfire.instrument_pydantic_ai(), so an agent run emits spans wherever it is
#      built rather than only where a capability was wired. That is safe alongside
#      the per-agent Instrumentation capability the pydantic-ai backend can add:
#      pydantic-ai 2.18 injects the global one only when the run's capability list
#      does not already carry an InstrumentationCap (agent/__init__.py:1368), so
#      the two cannot double up.
#
#   2. install_logfire_bridge() — add Logfire's stdlib logging handler to the root
#      ADDITIVELY. Not via basicConfig(handlers=[...]), which the Logfire docs
#      suggest and which would delete the console handler — and with it the
#      handler that install_scrubbing_filters() attaches the scrubbers to.
#
# BOTH ARE INERT UNLESS POCKETPAW_LOGFIRE_ENABLED IS TRUTHY. Turning it on with no
# LOGFIRE_TOKEN and no OTEL_EXPORTER_OTLP_ENDPOINT is not free and not useful:
# logfire builds a real TracerProvider with zero span processors, so spans are
# constructed, serialized and dropped. There is no short-circuit for traces the way
# there is for metrics.
"""Process-wide observability wiring (Pydantic Logfire)."""

from __future__ import annotations

import importlib.util
import logging
import os

logger = logging.getLogger(__name__)

#: Values that count as "on" for POCKETPAW_LOGFIRE_ENABLED. Spelled out because
#: ``bool(os.environ.get(...))`` makes the string "false" truthy, which is the
#: classic way an off switch turns out to be a decoration.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

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

    instrument_pydantic_ai()
    return True


def instrument_pydantic_ai() -> bool:
    """Instrument every pydantic-ai agent in this process. True when it happened.

    ``Agent.instrument_all`` under the hood, so an agent built anywhere emits run,
    model-request and tool-call spans — not only one built through a code path that
    remembered to pass the capability.

    No double-instrumentation: pydantic-ai resolves the global settings into an
    ``InstrumentationCap`` at run time and inserts it only when the run's capability
    list does not already have one, so an agent carrying the backend's explicit
    ``Instrumentation()`` capability is instrumented once, by its own.

    Content is excluded unless ``POCKETPAW_LOGFIRE_INCLUDE_CONTENT`` says otherwise
    — see ``logfire_include_content``.
    """
    try:
        import logfire
    except ImportError:
        return False
    # find_spec rather than ``import pydantic_ai``: an existence check should not
    # execute a heavy package, and pydantic-ai is an extra too — a deployment on
    # another agent backend has no agents to instrument.
    if importlib.util.find_spec("pydantic_ai") is None:
        return False
    include_content = logfire_include_content()
    try:
        logfire.instrument_pydantic_ai(
            include_content=include_content,
            include_binary_content=include_content,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not instrument pydantic-ai: %s", exc)
        return False
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
