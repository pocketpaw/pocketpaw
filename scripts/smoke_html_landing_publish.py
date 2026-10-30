"""Smoke test — the DEFAULT html landing-site flow, driven for real end to end.

Created 2026-08-11 (chore/e2e-html-landing-smoke). ``tests/e2e/`` has no site
tests at all, and ``tests/ee/sites/test_dentist_e2e.py`` — the one called
end-to-end — fakes the generator AND Cloudflare in its own header. So the two
steps most likely to break in production (the real generator subprocess, the real
deploy + serve) had never been executed for a landing site. This script executes
them.

WHAT IT PROVES
    1. CREATE   ``_create_html_site_handler`` (the real MCP front door) persists a
                {path: contents} map as a pocket stamped type="site",
                pattern="landing", engine="html", and mints a Site doc with
                deployed=False — a draft. No build, no deploy.
    2. DRAFT    ``sites.service.preview_pocket`` returns that draft's content, and
                ``draft_markup.build_draft_markup`` assembles ONE self-contained
                document: local CSS folded in as a ``<style>``, ``url()`` refs
                inside that CSS inlined as data: URIs, relative stylesheet links
                gone, absolute http(s) refs untouched. That matters because Browser
                Rendering renders an ``html`` body at ``about:blank``, where no
                relative reference resolves. It also probes the two ways an IMAGE
                can fail to fold, which is where this script found a real
                divergence — see ``_check_divergence`` and step ``2-drop``.
    3. PUBLISH  ``sites.service.publish_pocket`` in local deploy mode — the REAL
                generator subprocess (``paw-sites-gen build``), the real html
                static smoke gate, then ``local_server.deploy_local`` /
                ``persist_site`` onto the local static server.
    4. SERVE    A real HTTP GET of the deployed URL: status 200, the marketing copy
                actually present, AND every stylesheet the served page links
                resolves 200 with real CSS in it. An unstyled page that returns 200
                is the failure mode a status-only check misses, so the CSS fetch is
                the assertion that matters most here.

WHAT IT DOES NOT PROVE
    * NOTHING about Cloudflare. Deploy mode is pinned to ``local`` and the script
      REFUSES to run otherwise (see ``_guard_local_only``). The Workers and
      Workers-for-Platforms deploy branches, custom hostnames, and the real
      Browser Rendering screenshot are all unexercised. ``deploy_local`` copying a
      tree and ``cf.put_worker`` uploading a bundle are different code.
    * NOTHING about the Node build. The html engine runs no build
      (``needs_node_build("html")`` is False) and publishes inline
      (``build_runs_async("html")`` is False), which is exactly why this flow needs
      no sandbox. ripple / svelte / react publishes — bun install, Vite, the
      workerd SSR gate, the ephemeral build lane — are a different path.
    * NOTHING about concurrency. One site, one publish, one process.
    * Mongo semantics are only as real as the database it found. With mongomock
      (the fallback when no Mongo is reachable) writes are in-process and
      single-threaded, so upsert races, index uniqueness, and write concern are NOT
      exercised — mongomock's write semantics have already hidden a production race
      in this codebase. The script prints which backend it used; believe that line
      over this one.
    * The realtime bus is an in-process recording stub (``emit`` asserts without
      one), and the draft/live screenshot schedulers are left alone — they fail
      soft without Cloudflare credentials, which is itself part of what step 3
      shows.

RUNNING IT
    PAW_CF_DEPLOY_MODE=local uv run python scripts/smoke_html_landing_publish.py

    Requires the generator CLI. Either put ``paw-sites-gen`` on PATH or set
    PAW_SITES_GEN_CMD; with neither, the script looks for the sibling
    ``paw-sites/dist/cli.js`` checkout and uses ``node`` on it, saying so when it
    does. Everything it writes goes to a temp dir that is removed on exit — it
    never touches the operator's ``~/.pocketpaw``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

# The one marker string the served page must contain. Distinctive on purpose: a
# generic word could match a 404 body or an error page and pass a check that
# proves nothing.
HEADLINE = "Bright Harbor Dental"
CSS_MARKER = "--paw-smoke-accent"

failures: list[str] = []
notes: list[str] = []
divergences: list[str] = []


def _ok(step: str, msg: str) -> None:
    print(f"  PASS  {step}: {msg}")


def _fail(step: str, msg: str) -> None:
    failures.append(f"{step}: {msg}")
    print(f"  FAIL  {step}: {msg}")


def _note(msg: str) -> None:
    notes.append(msg)
    print(f"  NOTE  {msg}")


def _check(step: str, cond: bool, msg: str) -> bool:
    if cond:
        _ok(step, msg)
    else:
        _fail(step, msg)
    return cond


def _check_divergence(step: str, cond: bool, msg: str) -> bool:
    """A check for behaviour that is KNOWN to contradict its own documentation.

    Reported loudly but does NOT fail the run, because a permanently-red smoke
    script is one nobody runs. Each call site must name the code that diverges and
    the claim it diverges from, so the entry either gets fixed or gets deleted
    rather than quietly becoming the expected output.
    """
    if cond:
        _ok(step, msg)
        return True
    divergences.append(f"{step}: {msg}")
    print(f"  DIVERGENCE  {step}: {msg}")
    return False


# --------------------------------------------------------------------------- #
# Safety guards. This script must never be able to deploy to the real edge or
# spend a sandbox, regardless of what the ambient environment holds.
# --------------------------------------------------------------------------- #

# Credential-shaped vars whose PRESENCE means the process is one env flip away
# from a real deploy. We refuse rather than trust the mode flag alone.
_CF_CREDENTIAL_VARS = (
    "PAW_CF_API_TOKEN",
    "PAW_CF_ACCOUNT_ID",
    "PAW_CF_SITES_DOMAIN",
    "PAW_CF_DISPATCH_NAMESPACE",
)


def _guard_local_only() -> None:
    """Refuse to run unless this process can only deploy locally.

    Three conditions, all required. The mode flag is the one the service actually
    reads (``_deploy_mode()``); the other two are belt — a credential in the
    environment or a sandbox key means someone can flip the mode and this script
    becomes a real deploy.
    """
    mode = (os.environ.get("PAW_CF_DEPLOY_MODE") or "").strip().lower()
    if mode != "local":
        sys.exit(
            "REFUSING TO RUN: PAW_CF_DEPLOY_MODE must be exactly 'local' "
            f"(got {mode or '<unset>'}). This script publishes a site for real and "
            "will not do it against the Cloudflare edge."
        )
    present = [v for v in _CF_CREDENTIAL_VARS if (os.environ.get(v) or "").strip()]
    if present:
        sys.exit(
            "REFUSING TO RUN: Cloudflare settings are in the environment "
            f"({', '.join(present)}). Run this from a checkout with no .env, or "
            "unset them — a local smoke must not be one flag away from a real deploy."
        )
    if (os.environ.get("DAYTONA_API_KEY") or "").strip():
        sys.exit(
            "REFUSING TO RUN: DAYTONA_API_KEY is set. The html engine runs no build "
            "and needs no sandbox; a key in scope means this run could spend one."
        )


def _guard_engine_needs_nothing() -> None:
    """Assert from the code itself that html publishes inline with no build.

    This is why the run needs neither Node nor a sandbox. If either predicate ever
    flips, the safety argument in this file's header is void and the script should
    stop rather than quietly start building or queueing.
    """
    from pocketpaw_ee.sites.engines import needs_node_build
    from pocketpaw_ee.sites.service import build_runs_async

    if needs_node_build("html"):
        sys.exit(
            "REFUSING TO RUN: needs_node_build('html') is now True — this script's "
            "no-toolchain assumption no longer holds."
        )
    if build_runs_async("html"):
        sys.exit(
            "REFUSING TO RUN: build_runs_async('html') is now True — an html publish "
            "would enqueue a sandbox build, which this script must not spend."
        )


# --------------------------------------------------------------------------- #
# Environment + fixtures
# --------------------------------------------------------------------------- #


def _resolve_generator() -> list[str] | None:
    """The generator invocation, or None when there is none to be had.

    Mirrors ``generator_client._gen_cmd_argv``: PAW_SITES_GEN_CMD wins, else the
    ``paw-sites-gen`` bin on PATH. The third rung is this script's own
    convenience — the sibling ``paw-sites`` checkout's built CLI, which is what a
    workspace dev actually has — and it says so out loud, because a run that
    silently picked a different generator than production uses is a misleading
    pass.
    """
    raw = (os.environ.get("PAW_SITES_GEN_CMD") or "").strip()
    if raw:
        import shlex

        return shlex.split(raw)
    if shutil.which("paw-sites-gen"):
        return ["paw-sites-gen"]
    node = shutil.which("node")
    if not node:
        return None
    # Candidate 1 is this checkout's own parent. Candidate 2 is the MAIN worktree's
    # parent, resolved through git: run from a linked worktree (which is how this
    # script gets used) the two are different directories, and only the second one
    # is the workspace that holds the paw-sites checkout.
    candidates = [REPO_ROOT.parent]
    try:
        common = subprocess.run(
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if common.returncode == 0 and common.stdout.strip():
            candidates.append(Path(common.stdout.strip()).parent.parent)
    except (OSError, subprocess.SubprocessError):
        pass
    for base in candidates:
        sibling = base / "paw-sites" / "dist" / "cli.js"
        if sibling.is_file():
            import shlex

            # FORWARD SLASHES, deliberately. generator_client._gen_cmd_argv parses
            # PAW_SITES_GEN_CMD with shlex.split() in POSIX mode, where a backslash
            # is an ESCAPE character — so a native Windows path in that variable is
            # silently flattened ("D:\a\b.js" -> "D:ab.js") and the generator dies
            # with MODULE_NOT_FOUND on a path the operator never typed. as_posix()
            # sidesteps it; node accepts forward slashes on Windows.
            cmd = [Path(node).as_posix(), sibling.as_posix()]
            os.environ["PAW_SITES_GEN_CMD"] = shlex.join(cmd)
            _note(
                "paw-sites-gen is not on PATH; using the sibling checkout's built CLI "
                f"({sibling.as_posix()}) via PAW_SITES_GEN_CMD. Production runs the "
                "packaged bin."
            )
            return cmd
    return None


def _isolate_paths(tmp: Path) -> None:
    """Point every on-disk root this flow writes at the throwaway dir.

    Without these three the run pollutes the operator's real ``~/.pocketpaw``:
    the deploy tree, the persistent per-pocket build dir, and the native-artifact
    cache all default to it.
    """
    os.environ["PAW_SITES_LOCAL_DIR"] = str(tmp / "sites")
    os.environ["PAW_SITES_BUILD_DIR"] = str(tmp / "site-builds")
    os.environ["PAW_SITES_ARTIFACT_DIR"] = str(tmp / "site-artifacts")


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


async def _init_db() -> str:
    """Initialise Beanie against real Mongo when one is reachable, else mongomock.

    Returns a human label naming which one, because the whole value of the run's
    persistence claims depends on it. PAW_SMOKE_MONGO_URL forces a URL.
    """
    from beanie import init_beanie
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

    db_name = f"paw_smoke_html_{uuid.uuid4().hex[:8]}"
    url = (os.environ.get("PAW_SMOKE_MONGO_URL") or "").strip()
    if not url and _port_open("127.0.0.1", 27017):
        url = "mongodb://127.0.0.1:27017"

    if url:
        from motor.motor_asyncio import AsyncIOMotorClient

        client = AsyncIOMotorClient(url, serverSelectionTimeoutMS=3000)
        await client.admin.command("ping")
        db = client[db_name]
        label = f"REAL Mongo at {url} (db {db_name})"
    else:
        from mongomock_motor import AsyncMongoMockClient

        client = AsyncMongoMockClient()
        db = client[db_name]
        # Beanie >=1.26 passes authorizedCollections / nameOnly, which
        # mongomock-motor's stub rejects. Same shim tests/ee/conftest.py uses.
        original = db.list_collection_names

        async def _safe_list_collection_names(*_a: Any, **_kw: Any) -> list[str]:
            return await original()

        db.list_collection_names = _safe_list_collection_names  # type: ignore[method-assign]
        label = "mongomock (NO real Mongo was reachable)"

    await init_beanie(database=db, document_models=[*ALL_DOCUMENTS, MemoryFactDoc])
    return label


def _install_recording_bus() -> Any:
    """Install an in-process recording bus.

    ``emit()`` asserts when no bus is initialised (a deliberate "forgot
    init_realtime" guard), and a live publish emits ``SitePublished``. The real bus
    needs infrastructure this script has no business standing up, so it records
    instead — the same substitution ``tests/ee/sites/conftest.py`` makes.
    """
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def publish(self, event: Any) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler: Any) -> None:  # noqa: ARG002
            return

    rec = _RecordingBus()
    bus_mod._bus = rec  # type: ignore[attr-defined]
    return rec


async def _seed_workspace(user_id: str) -> str:
    """Insert a REAL workspace doc on a plan that unlocks Sites.

    The sites write paths gate on ``require_sites_plan`` →
    ``workspace_service.get_workspace_plan``, which reads this doc. The test suite
    patches that function; seeding the document instead keeps the gate itself in
    the run rather than stubbing the thing being verified.
    """
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(
        name="Paw HTML Smoke",
        slug=f"paw-html-smoke-{uuid.uuid4().hex[:8]}",
        owner=user_id,
        plan="go",  # the cheapest tier carrying the "sites" feature
    )
    await ws.insert()
    return str(ws.id)


# --------------------------------------------------------------------------- #
# The authored source maps — what an agent would hand create_html_site.
# --------------------------------------------------------------------------- #

# A publishable landing page: an entry document, a real stylesheet, and an inlinable
# asset. Every local ref RESOLVES, because the html static smoke gate fails a
# publish whose page links a file that isn't there (which is correct, and is why the
# drop-behaviour case below is a separate pocket that is never published).
LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<circle cx="12" cy="12" r="10" fill="#0e7c86"/></svg>'
)

SITE_A_SOURCE: dict[str, str] = {
    "index.html": f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{HEADLINE} — gentle dentistry on the waterfront</title>
<link rel="stylesheet" href="./styles.css">
</head>
<body>
<header class="nav">
  <img class="logo" src="./logo.svg" alt="{HEADLINE}" width="32" height="32">
  <a href="#book" class="cta">Book a visit</a>
</header>
<main>
  <section class="hero">
    <h1>{HEADLINE}</h1>
    <p class="sub">Cleanings, whitening, and same-week emergency care, two blocks
    from the ferry terminal.</p>
    <a class="cta" href="#book">Book a visit</a>
  </section>
  <section class="proof">
    <p>Open Saturdays. Most insurance accepted.</p>
    <p><a href="https://example.com/reviews">Read patient reviews</a></p>
  </section>
</main>
<footer id="book"><p>call 775-555-0100</p></footer>
</body>
</html>
""",
    "styles.css": f""":root {{
  {CSS_MARKER}: #0e7c86;
}}
body {{
  margin: 0;
  font-family: Georgia, serif;
  color: #16232b;
}}
.hero {{
  padding: 6rem 1.5rem;
  background-image: url("./logo.svg");
  background-repeat: no-repeat;
}}
.hero h1 {{
  font-size: clamp(2rem, 6vw, 4rem);
  color: var({CSS_MARKER});
}}
.cta {{
  background: var({CSS_MARKER});
  color: #fff;
  padding: 0.75rem 1.25rem;
  border-radius: 999px;
  text-decoration: none;
}}
""",
    "logo.svg": LOGO_SVG,
}

# Preview-only. Probes the two ways a local reference can fail to fold:
#   * ``nowhere.png`` — no file behind it at all. For a real html site this is the
#     COMMON case, not a contrived one: a Pocket stores TEXT, so an imported zip's
#     binary images are absent from the source map until a first publish leaves them
#     in the build dir (see ``build_draft_markup`` rung 2).
#   * ``photo.bmp`` — present and readable, but its extension is not in
#     ``_MIME_BY_EXT``, so ``_data_uri`` refuses to guess a mime for it.
# Publishing this would (correctly) fail the html smoke gate on the missing file, so
# it is never published — the gate is doing its job, which is itself worth knowing.
SITE_B_SOURCE: dict[str, str] = {
    "index.html": """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><link rel="stylesheet" href="./styles.css"></head>
<body>
<h1>Unfoldable ref probe</h1>
<img src="./nowhere.png" alt="missing on purpose">
<img src="./photo.bmp" alt="present but unsupported mime">
<img src="https://example.com/remote.png" alt="absolute, must survive">
</body>
</html>
""",
    "styles.css": "h1 { color: rebeccapurple; }\n",
    "photo.bmp": "not really a bitmap, but readable bytes under an unmapped extension",
}


# --------------------------------------------------------------------------- #
# Step 1 — CREATE
# --------------------------------------------------------------------------- #


async def step1_create(
    workspace_id: str,
    user_id: str,
    source: dict[str, str],
    name: str,
    *,
    step: str = "1-create",
) -> str:
    """Drive the real MCP create handler and assert the pocket is a DRAFT."""
    from pocketpaw_ee.agent.mcp_servers.sites_create import _create_html_site_handler

    result = await _create_html_site_handler({"source": source, "name": name})
    if result.get("is_error"):
        _fail(step, f"handler returned an error: {result}")
        raise SystemExit(1)

    payload = json.loads(result["content"][0]["text"])
    pocket_id = payload["pocket_id"]
    pocket = payload["pocket"]

    _check(
        step, pocket.get("type") == "site", f'pocket type == "site" (got {pocket.get("type")!r})'
    )
    _check(
        step,
        pocket.get("pattern") == "landing",
        f'pocket pattern == "landing" (got {pocket.get("pattern")!r})',
    )
    _check(
        step,
        pocket.get("engine") == "html",
        f'pocket engine == "html" (got {pocket.get("engine")!r})',
    )

    # The persisted source map round-trips verbatim — the html track has no
    # assemble step, so what the agent wrote is what publish will serve.
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    stored = await pockets_service.get(pocket_id, user_id)
    _check(
        step,
        stored.get("source") == source,
        "the persisted source map is byte-identical to the authored one",
    )

    # A draft: a Site doc exists (so the gallery lists it) but is not deployed.
    from pocketpaw_ee.sites.service import _SiteDoc

    doc = await _SiteDoc.find_one({"pocket_id": pocket_id, "workspace": workspace_id})
    if doc is None:
        _fail(step, "no Site doc was minted, so the site would not list in the gallery")
    else:
        _check(step, doc.deployed is False, "Site doc deployed == False (a draft)")
        _check(step, not (doc.url or ""), f"draft has no url (got {doc.url!r})")

    # Nothing was built: the per-pocket build dir must not exist yet.
    from pocketpaw_ee.sites.generator_client import build_home

    built = build_home() / pocket_id
    _check(step, not built.exists(), f"no build dir was created ({built.name} absent)")
    return pocket_id


# --------------------------------------------------------------------------- #
# Step 2 — DRAFT PREVIEW + self-contained document assembly
# --------------------------------------------------------------------------- #


async def step2_preview(
    workspace_id: str, user_id: str, pocket_id: str, source: dict[str, str]
) -> None:
    from pocketpaw_ee.sites import service as sites_service

    resp = await sites_service.preview_pocket(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
    )
    _check("2-preview", resp.engine == "html", f'preview engine == "html" (got {resp.engine!r})')
    _check(
        "2-preview",
        resp.content == source,
        "preview_pocket returns the draft's own source map",
    )


async def step2_draft_markup(workspace_id: str, user_id: str, pocket_id: str) -> None:
    """The document that goes to Browser Rendering must stand alone at about:blank."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.sites import draft_markup
    from pocketpaw_ee.sites.service import _SiteDoc

    site = await _SiteDoc.find_one({"pocket_id": pocket_id, "workspace": workspace_id})
    pocket = await pockets_service.get(pocket_id, user_id)
    doc = await draft_markup.build_draft_markup(site, pocket=pocket)

    if not doc:
        _fail("2-markup", "build_draft_markup returned empty — the card gets a placeholder")
        return

    _check("2-markup", HEADLINE in doc, "the assembled document carries the marketing copy")
    _check(
        "2-markup",
        CSS_MARKER in doc,
        "the local stylesheet was FOLDED IN (its custom property is in the document)",
    )
    _check(
        "2-markup",
        'href="./styles.css"' not in doc and "href='./styles.css'" not in doc,
        "no relative stylesheet <link> survives (it would never resolve at about:blank)",
    )
    # The CSS itself referenced ./logo.svg — that url() has to become a data: URI
    # or the background silently fails to paint in the screenshot.
    _check(
        "2-markup",
        'url("data:image/svg+xml' in doc
        or "url(data:image/svg+xml" in doc
        or "url('data:image/svg+xml" in doc,
        "the url() inside the inlined CSS became a data: URI",
    )
    _check(
        "2-markup",
        "./logo.svg" not in doc,
        "no relative asset path is left anywhere in the document",
    )
    _check(
        "2-markup",
        "https://example.com/reviews" in doc,
        "absolute http(s) references are left alone (the render browser can fetch them)",
    )


async def step2_drop_unfoldable(workspace_id: str, user_id: str, pocket_id: str) -> None:
    """A local ref with no file behind it is dropped, not left to 404."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.sites import draft_markup
    from pocketpaw_ee.sites.service import _SiteDoc

    site = await _SiteDoc.find_one({"pocket_id": pocket_id, "workspace": workspace_id})
    pocket = await pockets_service.get(pocket_id, user_id)
    doc = await draft_markup.build_draft_markup(site, pocket=pocket)
    if not doc:
        _fail("2-drop", "build_draft_markup returned empty for the unfoldable-ref probe")
        return
    _check(
        "2-drop",
        "rebeccapurple" in doc,
        "the foldable stylesheet still got inlined alongside the bad refs",
    )
    _check(
        "2-drop",
        "https://example.com/remote.png" in doc,
        "the absolute ref alongside them was preserved",
    )
    # DIVERGENCE, not FAIL. draft_markup's module header states that "anything local
    # that cannot be folded in is dropped rather than left to 404", and
    # ``_MIME_BY_EXT``'s comment says rendering a broken image is "the one outcome
    # this slice promises never to produce". ``inline_document.img_repl`` keeps that
    # promise for stylesheets (``return ""``) but NOT for images: all three of its
    # failure branches ``return tag``, leaving the relative src in a document that is
    # about to be rendered at about:blank, where it cannot resolve.
    _check_divergence(
        "2-drop",
        "nowhere.png" not in doc,
        "an UNREADABLE local img src is left in the document (img_repl returns the "
        "tag unchanged when read() gives None) — the header promises it is dropped",
    )
    _check_divergence(
        "2-drop",
        "photo.bmp" not in doc,
        "a readable img whose extension is not in _MIME_BY_EXT is left in the "
        "document (img_repl returns the tag when _data_uri declines) — same promise",
    )


# --------------------------------------------------------------------------- #
# Step 3 — PUBLISH (real generator, local deploy)
# --------------------------------------------------------------------------- #


async def step3_publish(workspace_id: str, user_id: str, pocket_id: str) -> str:
    from pocketpaw_ee.sites import service as sites_service

    doc = await sites_service.publish_pocket(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
    )
    _check("3-publish", doc.deployed is True, "Site doc flipped deployed == True")
    _check("3-publish", bool(doc.deployed_at), "deployed_at was stamped")
    url = doc.url or ""
    _check("3-publish", bool(url), f"a url was stamped ({url!r})")
    _check(
        "3-publish",
        "127.0.0.1" in url or "localhost" in url,
        f"the url is a LOCAL one, so nothing reached the edge ({url!r})",
    )

    # The generator subprocess really ran: step 1 asserted this dir did NOT exist,
    # and the generate step is the only thing that creates it. Without this check a
    # faked generator would pass every other assertion in this step.
    from pocketpaw_ee.sites.generator_client import build_home

    built = build_home() / pocket_id
    _check(
        "3-publish",
        built.is_dir() and (built / "index.html").is_file(),
        f"the real generator materialized the source map into {built.name}/",
    )

    # The deploy tree is the persisted copy the local server hands out.
    from pocketpaw_ee.sites.local_server import sites_home

    served = sites_home() / str(doc.id)
    _check(
        "3-publish",
        (served / "index.html").is_file(),
        f"persist_site copied a tree with index.html into {served.name}/",
    )
    _check(
        "3-publish",
        (served / "styles.css").is_file(),
        "the stylesheet was copied alongside it (an html site's tree is its source)",
    )
    # ``engines.py`` says an html site's served artifact is "byte-identical to the
    # authored source", which is the whole basis for draft_markup treating the source
    # map AS the static tree (rung 2). It holds for the GENERATOR's emission, but a
    # LIVE publish then injects the concierge bar (``_embed_concierge_bar``, between
    # build and deploy) — so the deployed bytes are the authored ones PLUS one script
    # tag. Strip exactly that tag and the rest must match byte for byte; anything
    # else the publish tail rewrote shows up as a diff.
    import re as _re

    served_index = (served / "index.html").read_text(encoding="utf-8")
    # The snippet is an HTML comment followed by the marker-stamped script tag; both
    # are the injection, so both come out before the comparison.
    stripped = _re.sub(
        r"\s*<!--\s*Paw Bar concierge[^>]*-->\s*<script[^>]*data-paw-bar-embed[^>]*>\s*</script>",
        "",
        served_index,
    )
    stripped = _re.sub(r"\s*<!--\s*Paw Bar concierge[^>]*-->", "", stripped)
    stripped = _re.sub(r"\s*<script[^>]*data-paw-bar-embed[^>]*>\s*</script>", "", stripped)
    injected = stripped != served_index
    if _check(
        "3-publish",
        stripped == SITE_A_SOURCE["index.html"],
        "the served index.html is the authored source, modulo the injected "
        f"concierge bar (bar injected: {injected})",
    ):
        if injected:
            _note(
                "a live publish rewrites the deployed index.html to add the concierge "
                "bar script. engines.py's 'byte-identical to the authored source' is "
                "true of the generator's output, not of the deployed file."
            )
    else:
        import difflib

        diff = list(
            difflib.unified_diff(
                SITE_A_SOURCE["index.html"].splitlines(),
                stripped.splitlines(),
                "authored",
                "served (concierge tag stripped)",
                lineterm="",
                n=1,
            )
        )
        for line in diff[:40]:
            print(f"        {line}")
    return url


# --------------------------------------------------------------------------- #
# Step 4 — SERVE over real HTTP
# --------------------------------------------------------------------------- #


def _get(url: str) -> tuple[int, bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "paw-html-smoke"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — localhost only
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def _stylesheet_hrefs(markup: str) -> list[str]:
    """Every stylesheet href the served page links, in document order."""
    import re

    hrefs: list[str] = []
    for tag in re.findall(r"<link\b[^>]*>", markup, flags=re.I):
        if not re.search(r'rel\s*=\s*["\']?stylesheet', tag, flags=re.I):
            continue
        m = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, flags=re.I)
        if m:
            hrefs.append(m.group(1))
    return hrefs


def step4_serve(url: str) -> None:
    status, body, _ctype = _get(url)
    if not _check("4-serve", status == 200, f"GET {url} -> {status}"):
        return
    markup = body.decode("utf-8", "replace")
    _check("4-serve", HEADLINE in markup, "the served page contains the marketing copy")

    hrefs = _stylesheet_hrefs(markup)
    if not _check("4-serve", bool(hrefs), f"the served page links a stylesheet ({hrefs})"):
        return

    # THE ASSERTION THAT MATTERS. A 200 on the document proves nothing about
    # whether the page is styled; a stylesheet that 404s renders naked text and
    # still gives you a 200 on the page.
    for href in hrefs:
        if href.startswith(("http://", "https://", "//", "data:")):
            continue
        css_url = urllib.parse.urljoin(url, href)
        css_status, css_body, css_ctype = _get(css_url)
        _check("4-css", css_status == 200, f"GET {css_url} -> {css_status} (must not 404)")
        if css_status != 200:
            continue
        text = css_body.decode("utf-8", "replace")
        _check(
            "4-css",
            CSS_MARKER in text,
            "the stylesheet body is the authored CSS, not an error page",
        )
        _check(
            "4-css",
            "text/css" in css_ctype.lower(),
            f"served as text/css (got {css_ctype!r})",
        )

    # The inlinable asset the page and the CSS both point at.
    logo_url = urllib.parse.urljoin(url, "./logo.svg")
    logo_status, _logo_body, _ = _get(logo_url)
    _check("4-css", logo_status == 200, f"GET {logo_url} -> {logo_status}")


# --------------------------------------------------------------------------- #


async def run() -> int:
    user_id = f"smoke-user-{uuid.uuid4().hex[:8]}"

    print("== fixtures ==")
    db_label = await _init_db()
    print(f"  database: {db_label}")
    if db_label.startswith("mongomock"):
        _note(
            "mongomock: writes are in-process and single-threaded, so upsert races, "
            "index uniqueness and write concern are NOT exercised by this run."
        )
    _install_recording_bus()
    workspace_id = await _seed_workspace(user_id)
    print(f"  workspace: {workspace_id} (plan=go)")

    from pocketpaw_ee.cloud.chat.agent_service import attach_agent_identity, detach_agent_identity

    tokens = attach_agent_identity(workspace_id=workspace_id, user_id=user_id)
    try:
        print("\n== step 1: create (draft, no build, no deploy) ==")
        pocket_a = await step1_create(workspace_id, user_id, SITE_A_SOURCE, "Bright Harbor Dental")

        print("\n== step 2: draft preview + self-contained document ==")
        await step2_preview(workspace_id, user_id, pocket_a, SITE_A_SOURCE)
        await step2_draft_markup(workspace_id, user_id, pocket_a)
        pocket_b = await step1_create(
            workspace_id, user_id, SITE_B_SOURCE, "Unfoldable probe", step="2-drop-setup"
        )
        await step2_drop_unfoldable(workspace_id, user_id, pocket_b)

        print("\n== step 3: publish (real generator, local deploy) ==")
        url = await step3_publish(workspace_id, user_id, pocket_a)

        print("\n== step 4: serve over real HTTP ==")
        step4_serve(url)
    finally:
        detach_agent_identity(tokens)

    print("\n== summary ==")
    print(f"  database: {db_label}")
    for n in notes:
        print(f"  note: {n}")
    if divergences:
        print(f"\n  {len(divergences)} KNOWN DIVERGENCE(S) — documented behaviour vs actual:")
        for d in divergences:
            print(f"    - {d}")
    if failures:
        print(f"\n  {len(failures)} FAILURE(S):")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("  all steps passed")
    return 0


def main() -> int:
    _guard_local_only()
    sys.path.insert(0, str(REPO_ROOT / "ee"))
    _guard_engine_needs_nothing()

    if _resolve_generator() is None:
        sys.exit(
            "REFUSING TO RUN: no generator. Put 'paw-sites-gen' on PATH, set "
            "PAW_SITES_GEN_CMD, or check out the sibling paw-sites repo and build "
            "its dist/cli.js. Publishing an html site shells out to it."
        )

    tmp = Path(tempfile.mkdtemp(prefix="paw-html-smoke-"))
    _isolate_paths(tmp)
    print(f"scratch: {tmp}")
    try:
        return asyncio.run(run())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
