# tests/ship_engine/contract.py — the engine-agnostic contract kit for the
# ShipEngine port (SHIP-1).
#
# ``test_contract.py`` runs one identical suite against EVERY registered
# ``EngineCase`` — DokkuDriver today, DokployDriver or an own-Go engine
# later. A new driver joins the suite by registering a case in
# ``conftest.py``; the tests themselves never name a driver.
#
# The STANDARD SCENARIO every case must wire its fake to serve (the constants
# below): one app ``demo`` deployed from ``IMAGE`` with the two ``ENV`` vars,
# a domain with TLS, a linked ``demo-db`` database, a backup at
# ``BACKUP_PATH``, a rollback to ``ROLLBACK_IMAGE``, healthy metrics (one
# running process, disk under 100%), and recent logs. ``make_failing`` wires
# an engine whose ``deploy_app`` fails so the typed-error path is provable.
# ``secret_markers`` are strings that DO exist inside the case's fixtures
# (env values, generated DB passwords) and must NEVER surface in a result
# DTO, an exception, or a log line.
#
# Created 2026-07-21 (feat/ship-1-engine-contract): new module.
# Updated 2026-07-21 (review fixes): leak-scanner regex widened to flag ANY
#   ``scheme://userinfo@`` (a password containing ``@`` previously slipped
#   the narrower user:pass shape).

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, fields, is_dataclass
from typing import Any

from pocketpaw_ee.ship_engine import (
    AppSpec,
    BoxSpec,
    DeployRequest,
    GitSource,
    ShipEngine,
)

# --------------------------------------------------------------------------- #
# The standard scenario — every engine case wires its fake around these.
# --------------------------------------------------------------------------- #

APP = "demo"
IMAGE = "registry.paw.example/demo:9f3c2e1"
ROLLBACK_IMAGE = "registry.paw.example/demo:1a2b3c4"
DOMAIN = "demo.paw.example"
SERVICE = "demo-db"
BACKUP_PATH = "/var/backups/demo-db.dump"
ENV: Mapping[str, str] = {
    "API_KEY": "hunter2-super-secret-value",
    "MONGO_PASSWORD": "passw0rd-abc",
}

BOX_SPEC = BoxSpec(name="paw-box-1", region="fsn1", size="cx22")
APP_SPEC = AppSpec(name=APP, env=ENV)
DEPLOY_REQUEST = DeployRequest(app=APP_SPEC, image=IMAGE)

# The v1 deploy_source scenario: a private git repo built from ``main`` with an
# access token. The token is a secret marker (registered in the case) — it must
# NEVER surface in a result DTO, an exception, or a log line. Redaction is
# marker-based (exact string), so the literal is deliberately NOT a valid
# GitHub-PAT shape: the underscores break the CI secret-scanner's
# ``ghp_[A-Za-z0-9]{36}`` pattern. Keep it that way — a real-looking token here
# only trips the scanner without making the redaction test any stronger.
GIT_REPO = "https://github.com/paw-demo/app.git"
GIT_REF = "main"
GIT_TOKEN = "ghp_EXAMPLE_do_not_leak_contract_marker"
GIT_SOURCE = GitSource(repo_url=GIT_REPO, ref=GIT_REF, token=GIT_TOKEN)


@dataclass(frozen=True)
class EngineCase:
    """One ShipEngine implementation registered with the contract suite.

    ``make_happy`` builds an engine wired for the standard scenario above;
    ``make_failing`` builds one whose ``deploy_app`` fails with a non-zero
    exit; ``unsupported_verbs`` are the verbs the engine refuses by design
    (``VerbNotSupported``); ``secret_markers`` are secrets present in the
    case's fixtures that must never leak.
    """

    name: str
    make_happy: Callable[[], ShipEngine]
    make_failing: Callable[[], ShipEngine]
    unsupported_verbs: frozenset[str]
    secret_markers: tuple[str, ...]


# How the suite invokes each verb with the standard scenario's inputs —
# keyed by contract verb name so ``unsupported_verbs`` tests stay generic.
VERB_CALLS: dict[str, Callable[[ShipEngine], Awaitable[Any]]] = {
    "provision_box": lambda engine: engine.provision_box(BOX_SPEC),
    "deploy_app": lambda engine: engine.deploy_app(DEPLOY_REQUEST),
    "deploy_source": lambda engine: engine.deploy_source(APP_SPEC, GIT_SOURCE),
    "add_domain": lambda engine: engine.add_domain(APP, DOMAIN),
    "db_create": lambda engine: engine.db_create(APP, SERVICE),
    "backup": lambda engine: engine.backup(SERVICE, BACKUP_PATH),
    "rollback": lambda engine: engine.rollback(APP, ROLLBACK_IMAGE),
    "logs": lambda engine: engine.logs(APP),
    "metrics": lambda engine: engine.metrics(APP),
    "destroy": lambda engine: engine.destroy(APP),
}

# ``scheme://userinfo@`` — credential material embedded in a URL/DSN. Any
# userinfo before a host is treated as a leak (results should never carry
# even a username), and matching to the LAST ``@`` catches passwords that
# themselves contain ``@``.
_URL_CREDS_RE = re.compile(r"\w+://[^\s/]+@")


def iter_string_values(obj: Any) -> Iterator[str]:
    """Yield every string reachable from a result DTO (fields, tuples)."""
    if isinstance(obj, str):
        yield obj
    elif is_dataclass(obj) and not isinstance(obj, type):
        for f in fields(obj):
            yield from iter_string_values(getattr(obj, f.name))
    elif isinstance(obj, (tuple, list)):
        for item in obj:
            yield from iter_string_values(item)


def assert_clean_text(text: str, markers: tuple[str, ...]) -> None:
    """Assert ``text`` carries no URL credentials and no known secret."""
    assert not _URL_CREDS_RE.search(text), f"URL credentials leaked: {text!r}"
    for marker in markers:
        assert marker not in text, f"secret marker {marker!r} leaked: {text!r}"


def assert_no_secret_material(dto: Any, markers: tuple[str, ...]) -> None:
    """Assert no string field of a result DTO carries secret material."""
    for value in iter_string_values(dto):
        assert_clean_text(value, markers)
