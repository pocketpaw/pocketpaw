"""Keep benign Windows accept-aborts out of the asyncio error log.

Created: 2026-08-01
Changes:
  - 2026-08-01: initial — ``install_accept_noise_filter()`` swallows the
    ``Accept failed on a socket`` / orphaned ``accept_coro`` pair that the
    Windows ProactorEventLoop logs (with a full traceback) every time a
    client aborts a TCP connection between ``AcceptEx`` and its completion.

Why a loop exception handler and not a log filter: both messages are emitted
by ``loop.call_exception_handler()``, which formats and logs them itself, so
they arrive at the ``asyncio`` logger already rendered at ERROR. Intercepting
at the handler is the only place the pair can be dropped as a unit.

Why not switch event loops: ``SelectorEventLoop`` does not log this, but it
cannot spawn subprocesses on Windows, which would break every stdio MCP
server. The noise is the cheaper problem.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# Windows error codes a pending AcceptEx reports when the peer goes away
# before the overlapped operation completes. Operationally identical: the
# client vanished, the listening socket is fine, asyncio re-arms the accept.
_BENIGN_ACCEPT_WINERRORS = frozenset(
    {
        64,  # ERROR_NETNAME_DELETED — "The specified network name is no longer available"
        121,  # ERROR_SEM_TIMEOUT — half-open connection timed out mid-accept
        995,  # ERROR_OPERATION_ABORTED — pending accept cancelled during shutdown
        1236,  # ERROR_CONNECTION_ABORTED — peer sent RST
    }
)

# Substrings that identify the proactor's own accept coroutine. Used to scope
# the very generic "Task exception was never retrieved" message so an
# application task's unretrieved error is never mistaken for accept noise.
_ACCEPT_COROUTINE_MARKERS = ("accept_coro", "IocpProactor.accept")


def _is_benign_accept_error(context: dict) -> bool:
    """True when ``context`` is the Windows aborted-accept noise, nothing else.

    ``context`` is an asyncio exception-handler context dict. Two distinct
    messages arrive per aborted connection: the proactor's own
    ``Accept failed on a socket``, and a ``Task exception was never
    retrieved`` when the orphaned accept coroutine is finalized.
    """
    exc = context.get("exception")
    if not isinstance(exc, OSError):
        return False

    # ``winerror`` only exists on Windows, so this is inert on POSIX — which
    # is correct: the selector loop handles ECONNABORTED itself and never
    # reaches the exception handler.
    if getattr(exc, "winerror", None) not in _BENIGN_ACCEPT_WINERRORS:
        return False

    message = context.get("message") or ""
    if message == "Accept failed on a socket":
        return True

    if message.startswith("Task exception was never retrieved"):
        target = repr(context.get("future") or context.get("task"))
        return any(marker in target for marker in _ACCEPT_COROUTINE_MARKERS)

    return False


def install_accept_noise_filter(loop: asyncio.AbstractEventLoop | None = None) -> bool:
    """Install the filter on ``loop`` (default: the running loop).

    Anything that is not an aborted accept is passed to whatever handler was
    already installed, falling back to asyncio's default — so this suppresses
    noise without ever hiding a real error.

    Returns ``True`` once a filter is in place, ``False`` if there was no loop
    to install it on. Safe to call more than once; the second call is a no-op.
    """
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False

    previous = loop.get_exception_handler()
    if getattr(previous, "_pocketpaw_accept_filter", False):
        return True

    def handler(loop_: asyncio.AbstractEventLoop, context: dict) -> None:
        if _is_benign_accept_error(context):
            logger.debug(
                "Dropped aborted-accept noise (client disconnected before AcceptEx completed): %s",
                context.get("exception"),
            )
            return
        if previous is not None:
            previous(loop_, context)
        else:
            loop_.default_exception_handler(context)

    handler._pocketpaw_accept_filter = True  # type: ignore[attr-defined]
    loop.set_exception_handler(handler)
    return True
