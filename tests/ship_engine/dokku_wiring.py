# tests/ship_engine/dokku_wiring.py — the DokkuDriver's registration with the
# contract suite (SHIP-1): exact-command → transcript maps for the standard
# scenario, replayed by ``FakeSSHTransport`` with zero network.
#
# The command strings here ARE the driver's expected command surface — if the
# driver changes a command, the fake rejects it loudly and the map is updated
# deliberately. ``SECRET_MARKERS`` are the secrets that exist inside the
# transcripts (env values echoed by config:set, the DSN password printed by
# dokku-mongo) and must never surface anywhere observable.
#
# Created 2026-07-21 (feat/ship-1-engine-contract): new module.

from __future__ import annotations

from pocketpaw_ee.ship_engine.dokku import DokkuDriver
from pocketpaw_ee.ship_engine.transcripts import FakeSSHTransport

from tests.ship_engine import contract as c

_CONFIG_SET_CMD = (
    f"dokku config:set --no-restart {c.APP} "
    "API_KEY=hunter2-super-secret-value MONGO_PASSWORD=passw0rd-abc"
)

# The happy path: app missing on first deploy (exercises apps:create), then
# every verb of the standard scenario.
HAPPY_REPLIES: dict[str, str] = {
    f"dokku apps:exists {c.APP}": "apps_exists_missing.txt",
    f"dokku apps:create {c.APP}": "apps_create.txt",
    _CONFIG_SET_CMD: "config_set.txt",
    f"dokku git:from-image {c.APP} {c.IMAGE}": "git_from_image.txt",
    f"dokku git:from-image {c.APP} {c.ROLLBACK_IMAGE}": "git_from_image.txt",
    f"dokku domains:add {c.APP} {c.DOMAIN}": "domains_add.txt",
    f"dokku letsencrypt:enable {c.APP}": "letsencrypt_enable.txt",
    f"dokku mongo:create {c.SERVICE}": "mongo_create.txt",
    f"dokku mongo:link {c.SERVICE} {c.APP}": "mongo_link.txt",
    f"dokku mongo:export {c.SERVICE} > {c.BACKUP_PATH}": "mongo_export.txt",
    f"stat -c%s {c.BACKUP_PATH}": "stat_size.txt",
    f"dokku logs {c.APP} --num 100": "logs.txt",
    f"dokku ps:report {c.APP}": "ps_report.txt",
    "df -Pk /": "df_root.txt",
    (
        f"docker stats --no-stream --no-trunc "
        f"--format '{{{{.CPUPerc}}}} {{{{.MemPerc}}}}' --filter name={c.APP}."
    ): "docker_stats.txt",
    f"dokku --force apps:destroy {c.APP}": "apps_destroy.txt",
}

# The failing path: the app exists, config applies, the image deploy fails.
FAILING_REPLIES: dict[str, str] = {
    f"dokku apps:exists {c.APP}": "apps_exists_ok.txt",
    _CONFIG_SET_CMD: "config_set.txt",
    f"dokku git:from-image {c.APP} {c.IMAGE}": "git_from_image_fail.txt",
}

SECRET_MARKERS: tuple[str, ...] = (
    "hunter2-super-secret-value",  # ENV["API_KEY"], echoed by config_set.txt
    "passw0rd-abc",  # ENV["MONGO_PASSWORD"], echoed by config_set.txt
    "s3cr3tpass8f2a",  # the DSN password in mongo_create/link transcripts
)


def make_happy_transport() -> FakeSSHTransport:
    return FakeSSHTransport(HAPPY_REPLIES)


DOKKU_CASE = c.EngineCase(
    name="dokku",
    make_happy=lambda: DokkuDriver(make_happy_transport()),
    make_failing=lambda: DokkuDriver(FakeSSHTransport(FAILING_REPLIES)),
    unsupported_verbs=frozenset({"provision_box"}),
    secret_markers=SECRET_MARKERS,
)
