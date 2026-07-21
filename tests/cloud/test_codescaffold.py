# test_codescaffold.py — prompt -> plan -> composed source map (CS-1).
#
# Created 2026-07-22 (feat/codescaffold).
#
# Two layers, deliberately separated:
#
#   * The PLAN tests are pure. No node, no subprocess — matching is a function of
#     a prompt and the catalog, and its failures should read as "the matcher is
#     wrong", not "the box has no node".
#   * The COMPOSE tests really shell the vendored engine. That is the point of
#     them: the whole slice rests on the claim that the template ships and runs
#     with nothing installed, and a mocked subprocess would prove neither. They
#     skip (loudly) rather than fail when node is absent or too old.
from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.codescaffold import domain, engine
from pocketpaw_ee.cloud.codescaffold import service as scaffold_service

WS = "ws-1"
USER = "user-1"

BOOKING_PROMPT = "a booking app with sign-in"


# ── The catalog itself ──────────────────────────────────────────────────────


def test_catalog_matches_the_engines_manifest_list() -> None:
    """The Python catalog and the runner's static MANIFESTS are two lists of the
    same recipes, maintained by hand on purpose. If they drift, a prompt can
    select a recipe the engine cannot apply — so read the runner and compare."""
    runner = (engine.TEMPLATE_DIR / "_runner" / "compose.mjs").read_text(encoding="utf-8")
    for recipe in domain.CATALOG:
        assert f"recipes/{recipe.id}/recipe.ts" in runner, f"{recipe.id} missing from compose.mjs"


def test_every_catalog_recipe_has_its_payload_on_disk() -> None:
    """Catches the vendoring failure this slice is most exposed to: a recipe that
    exists in the catalog but whose files were dropped by an ignore rule."""
    for recipe in domain.CATALOG:
        manifest = engine.TEMPLATE_DIR / "recipes" / recipe.id / "recipe.ts"
        assert manifest.is_file(), f"{recipe.id}: {manifest} did not ship"


def test_the_runner_and_base_shipped() -> None:
    assert engine.RUNNER.is_file()
    assert (engine.TEMPLATE_DIR / "base" / "package.json").is_file()


# ── Matching ────────────────────────────────────────────────────────────────


def test_the_done_when_prompt_selects_db_and_auth() -> None:
    """CS-1's stated acceptance: "a booking app with sign-in" -> db + auth."""
    matches = domain.match_recipes(BOOKING_PROMPT)

    assert [m.id for m in matches] == ["db", "auth"]


def test_an_implicit_dependency_says_it_is_implicit() -> None:
    """`db` was never asked for by name. Showing "needed by auth" is the whole
    reason `why` exists — a user is about to approve this list."""
    by_id = {m.id: m.reason for m in domain.match_recipes(BOOKING_PROMPT)}

    assert by_id["auth"] == 'you said "sign-in"'
    assert by_id["db"] == "needed by auth"


def test_a_direct_mention_beats_the_implicit_reason() -> None:
    by_id = {m.id: m.reason for m in domain.match_recipes("a store with a database and login")}

    assert by_id["db"] == 'you said "database"'


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("a blog", []),
        ("a shop that takes payments", ["db", "stripe"]),
        ("somewhere users can sign up and pay", ["db", "auth", "stripe"]),
        ("SIGN-IN PAGE", ["db", "auth"]),
        ("a signin page", ["db", "auth"]),
        ("a sign in page", ["db", "auth"]),
    ],
)
def test_matching_across_phrasings(prompt: str, expected: list[str]) -> None:
    assert [m.id for m in domain.match_recipes(prompt)] == expected


def test_recipes_come_back_in_dependency_order() -> None:
    """The order the engine will apply them in, so the confirmation UI reads the
    same way the build runs."""
    ids = [m.id for m in domain.match_recipes("payments and accounts")]

    assert ids.index("db") < ids.index("auth")
    assert ids.index("db") < ids.index("stripe")


def test_a_keyword_inside_another_word_does_not_match() -> None:
    """Word boundaries. Substring matching would put "db" in "adblock" and
    "pay" in "paycheck", silently adding a database to a project."""
    assert domain.match_recipes("an adblock dashboard for paycheck stubs") == []


# ── Naming ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        (BOOKING_PROMPT, "booking"),
        ("Build me an app", "new-project"),
        ("", "new-project"),
        ("🎉🎉🎉", "new-project"),
        ("2048 game clone", "p-2048-game-clone"),
    ],
)
def test_derive_project_name(prompt: str, expected: str) -> None:
    """This becomes a directory name and a worker name, so it has to be a safe
    slug for ANY input — including one with no usable words in it."""
    assert domain.derive_project_name(prompt) == expected


def test_a_derived_name_is_always_a_safe_slug() -> None:
    for prompt in ["../../etc/passwd", "a" * 500, "!!!", "My App: The Sequel!"]:
        name = domain.derive_project_name(prompt)
        assert name
        assert name.replace("-", "").isalnum(), name
        assert len(name) <= 40


# ── Requirements ────────────────────────────────────────────────────────────


def test_a_composed_project_needs_a_native_toolchain() -> None:
    """Every composed project targets Cloudflare Workers, so `wrangler dev` runs
    workerd — a native binary. This is the flag that will route these projects to
    a VM rather than an in-tab runtime, which is Decision 3 working as designed."""
    req = domain.requirements_for(["db", "auth"])

    assert req.nativeToolchain is True
    assert req.install is True


def test_it_does_not_claim_to_need_raw_sockets() -> None:
    """D1 is reached through `platform.env.DB`, a binding — not a TCP connection
    string. Over-declaring here would rule out runtimes for no reason."""
    assert domain.requirements_for(["db", "auth", "stripe"]).rawSockets is False


def test_every_raised_flag_carries_a_reason() -> None:
    """The `reasons` discipline websandbox/requirements.py established: a routing
    decision the user can see but cannot have explained is undebuggable."""
    req = domain.requirements_for(["db"])

    assert any("install" in r for r in req.reasons)
    assert any("nativeToolchain" in r for r in req.reasons)


def test_requirements_never_name_a_runtime() -> None:
    """The plan emits REQUIREMENTS; the client's registry picks the runtime. A
    runtime name leaking in here would move a product decision into the backend."""
    blob = " ".join(domain.requirements_for(["db", "auth", "stripe"]).reasons).lower()

    assert "daytona" not in blob
    assert "webcontainer" not in blob


# ── plan() ──────────────────────────────────────────────────────────────────


async def test_plan_answers_the_done_when() -> None:
    result = await scaffold_service.plan(WS, USER, {"prompt": BOOKING_PROMPT})

    assert [r.id for r in result.recipes] == ["db", "auth"]
    assert result.projectName == "booking"
    assert result.starter == domain.STARTER
    assert result.requires.nativeToolchain is True


async def test_plan_reports_secret_names_only() -> None:
    """Names, never values — the template's contract, and what lets a plan be
    logged and shown without carrying anything sensitive."""
    result = await scaffold_service.plan(WS, USER, {"prompt": "sign-in and payments"})

    assert result.secrets == ["AUTH_SECRET", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"]


async def test_plan_rejects_an_empty_prompt() -> None:
    with pytest.raises(Exception):
        await scaffold_service.plan(WS, USER, {"prompt": ""})


async def test_plan_on_an_unmatched_prompt_still_plans() -> None:
    """A prompt matching no recipe is not an error — the base template alone is a
    real project."""
    result = await scaffold_service.plan(WS, USER, {"prompt": "a blog"})

    assert result.recipes == []
    assert result.requires.install is True


# ── compose(): the real engine ──────────────────────────────────────────────

_NODE = shutil.which("node")


def _node_supports_strip_types() -> bool:
    if not _NODE:
        return False
    try:
        out = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [_NODE, "--version"], capture_output=True, text=True, timeout=15
        ).stdout.strip()
        major, minor = (int(p) for p in out.lstrip("v").split(".")[:2])
    except Exception:  # noqa: BLE001 — any failure means "cannot rely on it"
        return False
    # --experimental-strip-types landed in 22.6.
    return (major, minor) >= (22, 6)


requires_node = pytest.mark.skipif(
    not _node_supports_strip_types(),
    reason="needs node >= 22.6 for --experimental-strip-types",
)


@requires_node
async def test_compose_produces_the_auth_routes() -> None:
    """The other half of CS-1's done-when: the source map contains the auth
    routes. Really shells the vendored engine — the claim under test is that it
    runs with nothing installed."""
    result = await scaffold_service.compose(WS, USER, {"recipes": ["auth"], "projectName": "demo"})

    assert result.order == ["db", "auth"]
    assert "src/routes/sign-in/+page.svelte" in result.files
    assert "src/routes/dashboard/+page.server.ts" in result.files
    assert result.fileCount == len(result.files) > 40


@requires_node
async def test_compose_stacks_migrations() -> None:
    result = await scaffold_service.compose(WS, USER, {"recipes": ["auth"]})

    migrations = sorted(p for p in result.files if p.startswith("migrations/"))
    assert migrations == ["migrations/0001_init.sql", "migrations/0002_auth.sql"]


@requires_node
async def test_compose_resolves_dependencies_without_being_asked() -> None:
    """`auth` requires `db`. Asking for one composes both, and passing the whole
    catalog to the engine does NOT compose the whole catalog."""
    result = await scaffold_service.compose(WS, USER, {"recipes": ["auth"]})

    assert result.order == ["db", "auth"]
    assert "stripe" not in result.order
    assert not any("stripe" in p for p in result.files)


@requires_node
async def test_compose_with_no_recipes_still_yields_a_project() -> None:
    result = await scaffold_service.compose(WS, USER, {"recipes": []})

    assert result.files
    assert "package.json" in result.files
    assert result.order == []


@requires_node
async def test_the_engines_secrets_agree_with_the_catalog() -> None:
    """Two hand-maintained lists of the same fact. The engine's is derived from
    the manifests it actually applied, so this is the one that would be right if
    they drifted — which is exactly why it is worth comparing."""
    result = await scaffold_service.compose(WS, USER, {"recipes": ["auth", "stripe"]})

    assert sorted(result.secrets) == sorted(domain.secrets_for(["auth", "stripe", "db"]))


@requires_node
async def test_no_secret_value_is_ever_in_the_composed_source() -> None:
    """The invariant the whole secrets-by-name-only contract exists for. Recipes
    declare names; nothing that looks like a provisioned value may appear."""
    result = await scaffold_service.compose(WS, USER, {"recipes": ["auth", "stripe"]})

    blob = "\n".join(result.files.values())
    assert "sk_live_" not in blob
    assert "sk_test_" not in blob
    # A declared name may appear (as `platform.env.AUTH_SECRET`); an assignment
    # to it must not.
    assert "AUTH_SECRET=" not in blob


@requires_node
async def test_compose_does_not_leak_build_or_dependency_directories() -> None:
    """A stray `.svelte-kit` or `node_modules` in the vendored base would be
    composed into every generated project. It happened once during vendoring."""
    result = await scaffold_service.compose(WS, USER, {"recipes": ["auth"]})

    assert not [p for p in result.files if ".svelte-kit" in p or "node_modules" in p]


@requires_node
async def test_composing_twice_is_byte_identical() -> None:
    """Recipes are idempotent one-shot codemods, so composition is a pure
    function of the recipe list. A scaffolder that is not reproducible cannot be
    debugged from a bug report."""
    a = await scaffold_service.compose(WS, USER, {"recipes": ["auth"]})
    b = await scaffold_service.compose(WS, USER, {"recipes": ["auth"]})

    assert a.files == b.files


@requires_node
async def test_paths_are_posix_relative() -> None:
    """Both runtimes consume this map by path — tar entries for Daytona, mount
    keys for a WebContainer. A Windows separator or a leading slash breaks both,
    and this backend runs on Windows in development."""
    result = await scaffold_service.compose(WS, USER, {"recipes": ["auth"]})

    for path in result.files:
        assert "\\" not in path, path
        assert not path.startswith("/"), path
        assert ".." not in path.split("/"), path


# ── compose(): refusals and failures ────────────────────────────────────────


def _stub_binary(tmp_path, body: str) -> str:
    """A stand-in for node: a Python script plus a launcher that runs it.

    Written to disk rather than passed as an inline command because the override
    is resolved with `Path(raw).is_file()` — a real path is exactly what an
    operator would set, so the stub exercises the same branch production does.
    """
    import sys
    import textwrap

    script = tmp_path / "stub_engine.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


async def test_an_unknown_recipe_is_rejected_before_the_subprocess() -> None:
    """User-supplied strings reach a subprocess argv. Checking them against the
    catalog first turns a round trip into a 400 with a useful message."""
    with pytest.raises(CloudError) as exc:
        await scaffold_service.compose(WS, USER, {"recipes": ["auth", "rm -rf /"]})

    assert exc.value.status_code == 400
    assert "rm -rf /" in str(exc.value.message)


async def test_a_missing_node_is_a_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE paw-sites failure. Without this branch the operator gets a bare 500
    and no clue that the box simply has no node."""
    monkeypatch.setenv("PAW_CODESCAFFOLD_NODE", "definitely-not-a-real-binary-xyz")

    with pytest.raises(CloudError) as exc:
        await scaffold_service.compose(WS, USER, {"recipes": ["auth"]})

    assert exc.value.status_code == 503
    assert exc.value.code == "codescaffold.node_missing"


async def test_a_broken_engine_is_a_clean_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """An engine that runs but prints something other than the envelope must not
    surface as a half-composed project."""
    stub = _stub_binary(tmp_path, "print('not json at all')")
    monkeypatch.setenv("PAW_CODESCAFFOLD_NODE", stub)

    with pytest.raises(CloudError) as exc:
        await scaffold_service.compose(WS, USER, {"recipes": ["auth"]})

    assert exc.value.status_code == 500
    assert exc.value.code == "codescaffold.engine_failed"


async def test_a_missing_template_is_a_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The .dockerignore trap, made loud. A vendored tree dropped by either
    ignore layer produces a 5xx with nothing to point at; this names it."""
    monkeypatch.setattr(engine, "RUNNER", engine.TEMPLATE_DIR / "_runner" / "gone.mjs")

    with pytest.raises(CloudError) as exc:
        await scaffold_service.compose(WS, USER, {"recipes": ["auth"]})

    assert exc.value.status_code == 500
    assert exc.value.code == "codescaffold.template_missing"


async def test_a_hung_engine_times_out(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    stub = _stub_binary(tmp_path, "import time; time.sleep(30)")
    monkeypatch.setenv("PAW_CODESCAFFOLD_NODE", stub)
    monkeypatch.setattr(engine, "COMPOSE_TIMEOUT_SECONDS", 1)

    with pytest.raises(CloudError) as exc:
        await asyncio.wait_for(
            scaffold_service.compose(WS, USER, {"recipes": ["auth"]}), timeout=20
        )

    assert exc.value.status_code == 504
