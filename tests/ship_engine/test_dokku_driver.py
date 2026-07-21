# tests/ship_engine/test_dokku_driver.py — DokkuDriver specifics (SHIP-1):
# the command surface it issues, its Dokku-CLI parsing, and the redaction
# chokepoint. Everything engine-AGNOSTIC lives in test_contract.py — this
# module is allowed to know Dokku commands and transcript contents.
#
# The redaction tests are the security gate: the transcripts deliberately
# carry secrets (config:set echoes env values; dokku-mongo prints the DSN
# password), each test first PROVES the secret entered the chokepoint, then
# asserts it never reached a log record, an exception, or a DTO.
#
# Created 2026-07-21 (feat/ship-1-engine-contract): new module.

from __future__ import annotations

import importlib
import logging

import pytest
from pocketpaw_ee.ship_engine import CommandFailed, VerbNotSupported
from pocketpaw_ee.ship_engine.dokku import DokkuDriver
from pocketpaw_ee.ship_engine.transcripts import FakeSSHTransport, load_transcript

from tests.ship_engine import contract as c
from tests.ship_engine.dokku_wiring import (
    HAPPY_REPLIES,
    SECRET_MARKERS,
    make_happy_transport,
)

_DOKKU_LOGGER = "pocketpaw_ee.ship_engine.dokku"


# --------------------------------------------------------------------- #
# Verb ownership + command surface
# --------------------------------------------------------------------- #


async def test_provision_box_raises_verb_not_supported() -> None:
    driver = DokkuDriver(make_happy_transport())
    with pytest.raises(VerbNotSupported) as exc_info:
        await driver.provision_box(c.BOX_SPEC)
    assert exc_info.value.verb == "provision_box"
    assert exc_info.value.engine == "dokku"


async def test_deploy_creates_missing_app_then_deploys() -> None:
    transport = make_happy_transport()
    result = await DokkuDriver(transport).deploy_app(c.DEPLOY_REQUEST)
    assert transport.calls == [
        f"dokku apps:exists {c.APP}",
        f"dokku apps:create {c.APP}",
        f"dokku config:set --no-restart {c.APP} "
        "API_KEY=hunter2-super-secret-value MONGO_PASSWORD=passw0rd-abc",
        f"dokku git:from-image {c.APP} {c.IMAGE}",
    ]
    assert result.app_url == "http://demo.paw.example"


async def test_deploy_skips_create_when_app_exists() -> None:
    replies = {**HAPPY_REPLIES, f"dokku apps:exists {c.APP}": "apps_exists_ok.txt"}
    transport = FakeSSHTransport(replies)
    await DokkuDriver(transport).deploy_app(c.DEPLOY_REQUEST)
    assert f"dokku apps:create {c.APP}" not in transport.calls


async def test_backup_exports_then_sizes_the_dump() -> None:
    transport = make_happy_transport()
    result = await DokkuDriver(transport).backup(c.SERVICE, c.BACKUP_PATH)
    assert transport.calls == [
        f"dokku mongo:export {c.SERVICE} > {c.BACKUP_PATH}",
        f"stat -c%s {c.BACKUP_PATH}",
    ]
    assert result.size_bytes == 4194304


async def test_metrics_parses_ps_report_and_df() -> None:
    result = await DokkuDriver(make_happy_transport()).metrics(c.APP)
    assert result.deployed is True
    assert result.running is True
    assert result.processes == 1
    assert result.disk_used_pct == 38.0


async def test_db_create_reports_env_var_name_not_dsn() -> None:
    result = await DokkuDriver(make_happy_transport()).db_create(c.APP, c.SERVICE)
    assert result.exposed_env_var == "MONGO_URL"


# --------------------------------------------------------------------- #
# Redaction — the security gate
# --------------------------------------------------------------------- #


async def test_secrets_never_reach_log_output(caplog: pytest.LogCaptureFixture) -> None:
    # Sanity first: the secrets really do flow through the chokepoint — the
    # config:set COMMAND carries the env values, and the mongo transcripts
    # echo both them and the DSN password. Otherwise this test proves nothing.
    assert SECRET_MARKERS[0] in c.ENV["API_KEY"]
    assert SECRET_MARKERS[2] in load_transcript("mongo_create.txt").stdout
    assert SECRET_MARKERS[0] in load_transcript("config_set.txt").stdout

    caplog.set_level(logging.DEBUG, logger=_DOKKU_LOGGER)
    transport = make_happy_transport()
    driver = DokkuDriver(transport)
    await driver.deploy_app(c.DEPLOY_REQUEST)
    await driver.db_create(c.APP, c.SERVICE)

    assert any(SECRET_MARKERS[0] in call for call in transport.calls)  # entered
    for marker in SECRET_MARKERS:
        assert marker not in caplog.text, f"secret {marker!r} leaked into logs"
    assert "[redacted]" in caplog.text  # redaction actually ran


async def test_command_failed_redacts_the_command() -> None:
    config_cmd = next(cmd for cmd in HAPPY_REPLIES if "config:set" in cmd)
    replies = {**HAPPY_REPLIES, config_cmd: "config_set_fail.txt"}
    with pytest.raises(CommandFailed) as exc_info:
        await DokkuDriver(FakeSSHTransport(replies)).deploy_app(c.DEPLOY_REQUEST)
    exc = exc_info.value
    assert "API_KEY=[redacted]" in exc.command
    for marker in SECRET_MARKERS:
        assert marker not in str(exc)


async def test_command_failed_redacts_the_stderr_tail() -> None:
    replies = {**HAPPY_REPLIES, f"dokku mongo:link {c.SERVICE} {c.APP}": "mongo_link_fail.txt"}
    # Sanity: the failure stderr really contains the DSN password.
    assert SECRET_MARKERS[2] in load_transcript("mongo_link_fail.txt").stderr
    with pytest.raises(CommandFailed) as exc_info:
        await DokkuDriver(FakeSSHTransport(replies)).db_create(c.APP, c.SERVICE)
    exc = exc_info.value
    assert exc.exit_code == 1
    assert "[redacted]@" in exc.stderr_tail
    assert SECRET_MARKERS[2] not in str(exc)


# --------------------------------------------------------------------- #
# Chokepoint mechanics
# --------------------------------------------------------------------- #


async def test_timeout_maps_to_command_failed() -> None:
    transport = FakeSSHTransport(HAPPY_REPLIES, delay=0.05)
    driver = DokkuDriver(transport, timeouts={"logs": 0.01})
    with pytest.raises(CommandFailed) as exc_info:
        await driver.logs(c.APP)
    assert exc_info.value.exit_code == -1
    assert "timed out" in exc_info.value.stderr_tail


async def test_fake_transport_rejects_unmapped_commands() -> None:
    with pytest.raises(AssertionError, match="no transcript"):
        await FakeSSHTransport({}).run("dokku apps:list")


def test_asyncssh_dependency_is_installed() -> None:
    # SHIP-1 adds asyncssh to ee/pyproject.toml; the driver lazy-imports it,
    # so this is the one place the environment claim is actually proven.
    assert importlib.import_module("asyncssh") is not None
