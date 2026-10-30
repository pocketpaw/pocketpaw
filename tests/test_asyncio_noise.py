"""Tests for the asyncio accept-noise filter.

Created: 2026-08-01
Changes:
  - 2026-08-01: initial — reproduces the Windows proactor accept storm
    (``Accept failed on a socket`` + the orphaned ``accept_coro`` task) and
    proves the filter swallows exactly those two while every other error
    still reaches the previous handler.
"""

import asyncio

from pocketpaw.asyncio_noise import _is_benign_accept_error, install_accept_noise_filter


def _accept_oserror(winerror: int | None = 64) -> OSError:
    """Build the OSError a Windows proactor raises when a peer aborts an accept."""
    exc = OSError(22, "The specified network name is no longer available")
    if winerror is not None:
        exc.winerror = winerror
    return exc


class _FakeAcceptTask:
    """Stands in for the orphaned ``IocpProactor.accept.<locals>.accept_coro`` task."""

    def __repr__(self) -> str:
        return (
            "<Task finished name='Task-521' "
            "coro=<IocpProactor.accept.<locals>.accept_coro() done> "
            "exception=OSError(22, 'The specified network name is no longer available')>"
        )


class _FakeAppTask:
    """An ordinary application task — its unretrieved exceptions must survive."""

    def __repr__(self) -> str:
        return "<Task finished name='Task-77' coro=<sync_pockets() done>>"


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


def test_accept_failed_on_socket_is_benign():
    context = {"message": "Accept failed on a socket", "exception": _accept_oserror(64)}
    assert _is_benign_accept_error(context) is True


def test_orphaned_accept_task_is_benign():
    context = {
        "message": "Task exception was never retrieved",
        "future": _FakeAcceptTask(),
        "exception": _accept_oserror(64),
    }
    assert _is_benign_accept_error(context) is True


def test_connection_aborted_during_accept_is_benign():
    context = {"message": "Accept failed on a socket", "exception": _accept_oserror(1236)}
    assert _is_benign_accept_error(context) is True


def test_unrelated_task_exception_is_not_benign():
    """Same message, ordinary task — must not be swallowed even with a matching winerror."""
    context = {
        "message": "Task exception was never retrieved",
        "future": _FakeAppTask(),
        "exception": _accept_oserror(64),
    }
    assert _is_benign_accept_error(context) is False


def test_other_winerror_is_not_benign():
    """WSAECONNREFUSED on the accept path is a real problem — keep it visible."""
    context = {"message": "Accept failed on a socket", "exception": _accept_oserror(10061)}
    assert _is_benign_accept_error(context) is False


def test_posix_oserror_without_winerror_is_not_benign():
    context = {"message": "Accept failed on a socket", "exception": _accept_oserror(None)}
    assert _is_benign_accept_error(context) is False


def test_non_oserror_is_not_benign():
    context = {"message": "Accept failed on a socket", "exception": ValueError("nope")}
    assert _is_benign_accept_error(context) is False


def test_context_without_exception_is_not_benign():
    assert _is_benign_accept_error({"message": "Accept failed on a socket"}) is False


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


async def test_installed_filter_swallows_accept_noise():
    loop = asyncio.get_running_loop()
    seen: list[dict] = []
    loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx))
    try:
        assert install_accept_noise_filter() is True
        loop.call_exception_handler(
            {"message": "Accept failed on a socket", "exception": _accept_oserror(64)}
        )
        loop.call_exception_handler(
            {
                "message": "Task exception was never retrieved",
                "future": _FakeAcceptTask(),
                "exception": _accept_oserror(64),
            }
        )
        assert seen == []
    finally:
        loop.set_exception_handler(None)


async def test_installed_filter_delegates_real_errors():
    loop = asyncio.get_running_loop()
    seen: list[dict] = []
    loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx))
    try:
        install_accept_noise_filter()
        context = {"message": "Something actually broke", "exception": RuntimeError("boom")}
        loop.call_exception_handler(context)
        assert seen == [context]
    finally:
        loop.set_exception_handler(None)


async def test_installed_filter_falls_back_to_default_handler(monkeypatch):
    """With no prior handler the filter must defer to asyncio's own default."""
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(None)
    seen: list[dict] = []
    monkeypatch.setattr(loop, "default_exception_handler", lambda ctx: seen.append(ctx))
    try:
        install_accept_noise_filter()
        context = {"message": "Something actually broke", "exception": RuntimeError("boom")}
        loop.call_exception_handler(context)
        assert seen == [context]
    finally:
        loop.set_exception_handler(None)


async def test_install_is_idempotent():
    loop = asyncio.get_running_loop()
    try:
        assert install_accept_noise_filter() is True
        installed = loop.get_exception_handler()
        assert install_accept_noise_filter() is True
        assert loop.get_exception_handler() is installed
    finally:
        loop.set_exception_handler(None)


def test_install_outside_a_running_loop_is_a_noop():
    assert install_accept_noise_filter() is False
